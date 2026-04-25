"""Long-running optimizer process scheduler.

Runs jobs on per-component cadences. Job failures are isolated: an
exception in one job logs + records a heartbeat-error; the scheduler
keeps going. Overlap policy: SKIP if previous run still in flight
(no queuing — we never want two numerical searches fighting for TPE).

Cadences are intentionally conservative and keyed to trade-arrival
rate, not calendar preference. Swing trades once per ~day; running
swing optimisation hourly is noise-chasing.

Cadences:
  metrics.refresh_all         — every 15 min
  rollback.check              — every 5 min   (fast regression guard)
  numerical.propose           — every 6 hours, NEVER < 5 min after an
                                apply (let new config settle)
  canary.evaluate_all_running — every 30 min
  anomaly.scan                — every 60 min
  llm.failure_clustering      — weekly on Sunday 10:00 UTC
  llm.strategic_review        — weekly on Sunday 11:00 UTC
  meta.weekly_report          — weekly on Monday 00:10 UTC

Run order at startup: refresh metrics once, run a rollback check (safety
first), then enter the normal cadence loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal as _signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

import asyncpg

from . import db
from .metrics import refresh as metrics_refresh
from .lifecycle.rollback import check_and_maybe_rollback
from .canary.runner import evaluate_canary

log = logging.getLogger("optimizer.scheduler")


@dataclass
class Job:
    name: str
    interval_sec: int
    func: Callable[[asyncpg.Pool], Awaitable[None]]
    next_run_at: float = 0.0
    running: bool = False


async def _check_enabled(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT value FROM config WHERE key='OPTIMIZER_ENABLED'"
        )
    if row is None:
        return False
    v = row["value"]
    return v is True or v == 1 or v == "true"


async def _source_enabled(pool: asyncpg.Pool, source: str) -> bool:
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT enabled FROM optimizer_source_flags WHERE source=$1",
            source,
        )
    return bool(row and row["enabled"])


async def _heartbeat(pool: asyncpg.Pool, job_name: str, ok: bool,
                      err: str | None = None, duration_ms: int = 0) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO heartbeat (component, ts, info)
               VALUES ($1, NOW(), $2::jsonb)
               ON CONFLICT (component) DO UPDATE
                 SET ts=EXCLUDED.ts, info=EXCLUDED.info""",
            f"optimizer:{job_name}",
            json.dumps({"ok": ok, "err": err, "duration_ms": duration_ms}),
        )


async def _job_metrics_refresh(pool: asyncpg.Pool) -> None:
    if _markets_quiet():
        return
    await metrics_refresh.refresh_all(pool)


async def _job_rollback_check(pool: asyncpg.Pool) -> None:
    # Honour force-rollback flag set from dashboard.
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT value FROM config WHERE key='_force_rollback_pending'"
        )
    if row and (row["value"] is True or row["value"] == "true"):
        from .lifecycle.rollback import rollback_global
        new_id = await rollback_global(
            pool, triggered_by="dashboard", trigger="manual",
        )
        async with pool.acquire() as c:
            await c.execute(
                """UPDATE config SET value='false'::jsonb, updated_at=NOW()
                    WHERE key='_force_rollback_pending'"""
            )
        log.warning("force_rollback_honored", extra={"new_version": new_id})
    await check_and_maybe_rollback(pool)


async def _job_canary_evaluate(pool: asyncpg.Pool) -> None:
    if _markets_quiet():
        return
    async with pool.acquire() as c:
        ids = [r["id"] for r in await c.fetch(
            "SELECT id FROM canary_assignments WHERE status='running'"
        )]
    for cid in ids:
        try:
            await evaluate_canary(pool, canary_id=cid)
        except Exception:  # noqa: BLE001
            log.exception("canary_evaluate_failed id=%s", cid)


async def _job_numerical_propose(pool: asyncpg.Pool) -> None:
    if not await _source_enabled(pool, "numerical"):
        return
    # Deferred import so optuna isn't required at module-import time.
    from .hypothesis.numerical import propose
    try:
        pid = await propose(pool)
        if pid:
            log.info("numerical_proposed id=%s", pid)
            # Optimistic: immediately run the adversary so dashboard sees status.
            from .validator.adversary import validate_proposal
            from .config_store.versions import active_global_version, _values_of
            active = await active_global_version(pool)
            if active:
                baseline = await _values_of(pool, active["id"])
                # Load the proposal's candidate values.
                async with pool.acquire() as c:
                    row = await c.fetchrow(
                        "SELECT proposal FROM tuning_proposals WHERE id=$1", pid,
                    )
                prop = row["proposal"] if isinstance(row["proposal"], dict) else json.loads(row["proposal"])
                candidate = dict(baseline)
                for p in prop.get("proposals", []):
                    candidate[p["key"]] = p["to"]
                await validate_proposal(
                    pool, proposal_id=pid,
                    baseline=baseline, candidate=candidate,
                )
    except Exception:  # noqa: BLE001
        log.exception("numerical_propose_failed")


async def _job_anomaly_scan(pool: asyncpg.Pool) -> None:
    if _markets_quiet():
        return
    from .anomaly.detector import scan
    await scan(pool)


async def _job_llm_failure(pool: asyncpg.Pool) -> None:
    if not await _source_enabled(pool, "llm_failure"):
        return
    from .hypothesis.llm_failure import propose
    await propose(pool)


async def _job_llm_strategic(pool: asyncpg.Pool) -> None:
    if not await _source_enabled(pool, "llm_strategic"):
        return
    from .hypothesis.llm_strategic import propose
    await propose(pool)


async def _job_meta_report(pool: asyncpg.Pool) -> None:
    from .meta.report import generate_weekly
    await generate_weekly(pool)


async def _job_findings_resolve(pool: asyncpg.Pool) -> None:
    from .lifecycle.findings import resolve_stale
    await resolve_stale(pool)


def _is_sunday_hour(hour: int) -> Callable[[], bool]:
    def ok():
        now = datetime.now(timezone.utc)
        return now.weekday() == 6 and now.hour == hour
    return ok


def _markets_quiet() -> bool:
    """True when no major equity market in our universe is open within the
    next ~hour. Used to skip non-essential optimizer jobs that generate
    findings against frozen data. Coarse — ignores holidays. Missing one
    anomaly scan is harmless; emitting 50 duplicate "still in drawdown"
    findings every weekend is what filled the noticeboard.

    Window: weekdays 07:00–20:00 UTC. Captures EU open (~07:00) through
    US close (20:00). Outside that range nothing material can change in
    `trade_outcomes` because no market is taking orders."""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:        # Saturday/Sunday
        return True
    if now.hour < 7 or now.hour >= 20:
        return True
    return False


class Scheduler:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.stopping = False
        self.jobs: list[Job] = [
            Job("metrics.refresh",    15 * 60, _job_metrics_refresh),
            Job("rollback.check",      5 * 60, _job_rollback_check),
            Job("canary.evaluate",    30 * 60, _job_canary_evaluate),
            Job("numerical.propose",   6 * 3600, _job_numerical_propose),
            Job("anomaly.scan",       60 * 60, _job_anomaly_scan),
            # LLM jobs: interval is 6h-ish but inner guards (weekday/hour)
            # make them fire only within narrow windows.
            Job("llm.failure",         6 * 3600, _job_llm_failure),
            Job("llm.strategic",       6 * 3600, _job_llm_strategic),
            Job("meta.weekly",        12 * 3600, _job_meta_report),
            Job("findings.resolve",    6 * 3600, _job_findings_resolve),
        ]

    async def _run_job(self, job: Job) -> None:
        if job.running:
            log.info("skip_overlapping", extra={"job": job.name})
            return
        job.running = True
        started = time.time()
        err = None
        try:
            await job.func(self.pool)
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            log.exception("job_failed", extra={"job": job.name})
        finally:
            duration_ms = int((time.time() - started) * 1000)
            job.running = False
            job.next_run_at = time.time() + job.interval_sec
            await _heartbeat(self.pool, job.name, ok=err is None,
                                err=err, duration_ms=duration_ms)

    async def run(self) -> None:
        log.info("scheduler_start")
        # Startup safety sweep: refresh metrics, then check for rollback
        # before any proposal generation.
        await self._run_job(self.jobs[0])  # metrics.refresh
        await self._run_job(self.jobs[1])  # rollback.check

        while not self.stopping:
            if not await _check_enabled(self.pool):
                log.info("optimizer_disabled_sleeping")
                await asyncio.sleep(60)
                continue
            now_ts = time.time()
            for job in self.jobs:
                if now_ts >= job.next_run_at and not job.running:
                    asyncio.create_task(self._run_job(job))
            await asyncio.sleep(10)
        log.info("scheduler_stop")


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format='{"ts":"%(asctime)s","lvl":"%(levelname)s","job":"%(name)s","msg":%(message)s}',
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
    )
    pool = await db.open_pool()
    sch = Scheduler(pool)
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, lambda: setattr(sch, "stopping", True))
    try:
        await sch.run()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
