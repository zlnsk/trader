"""Canary: run a proposal on a bounded slot subset.

Flow:
  1. start_canary(): given a validated proposal, activate a slot-scoped
     config_version covering at most MAX_CANARY_SLOTS_ABSOLUTE slots.
     Insert canary_assignments row. Trader's `resolved_for_slot` now
     returns the canary config for those slots, baseline for others.

  2. evaluate_canary(): check real fills accumulated since start. Pass
     requires MIN_CANARY_TRADES in the canary arm AND the canary's mean
     net_pnl_pct CI lower bound > baseline mean net_pnl_pct. Fail on
     drawdown breach or CI lower bound below baseline. Else still
     running.

  3. On pass/fail, update canary_assignments.status + end time, flip the
     proposal's status.
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from .. import safety
from ..config_store.versions import (
    _values_of, propose_version, activate_version, deactivate_version,
    active_global_version,
)
from ..validator.bootstrap import two_sample_delta_ci

log = logging.getLogger("optimizer.canary")


PASS = "passed"
FAIL = "failed"
RUNNING = "running"
ABORTED = "aborted"


@dataclass
class CanaryConfig:
    min_trades_required: int = safety.MIN_CANARY_TRADES
    required_ci_bps: float = 5.0   # lower CI bound must beat baseline by this
    max_duration_days: int = 14


@dataclass
class CanaryVerdict:
    status: str              # running|passed|failed|aborted
    reason: str
    n_baseline: int
    n_canary: int
    observed_delta_bps: float | None = None
    ci_lo_bps: float | None = None
    ci_hi_bps: float | None = None


async def slots_for_canary(
    pool: asyncpg.Pool, *, strategy: str | None = None,
) -> list[int]:
    """Pick slot_ids for a canary. Rule:
    - ceil(MAX_CANARY_SLOT_FRACTION * N_total) slots, capped at
      MAX_CANARY_SLOTS_ABSOLUTE.
    - exclude any slot currently in an active canary (disjoint assignment).
    - prefer slots with active recent trading (n_samples>0 in last 30d).
    """
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT slot, strategy FROM slot_profiles"""
        )
    all_slots = [r["slot"] for r in rows if strategy is None or r["strategy"] == strategy]
    async with pool.acquire() as c:
        active = await c.fetch(
            """SELECT scope FROM config_versions
               WHERE scope->>'kind'='slots'
                 AND activated_at IS NOT NULL
                 AND deactivated_at IS NULL"""
        )
    blocked: set[int] = set()
    for a in active:
        sc = a["scope"] if isinstance(a["scope"], dict) else json.loads(a["scope"])
        blocked.update(sc.get("slot_ids") or [])
    candidates = [s for s in all_slots if s not in blocked]
    if not candidates:
        return []
    limit = min(
        safety.MAX_CANARY_SLOTS_ABSOLUTE,
        max(1, int(len(all_slots) * safety.MAX_CANARY_SLOT_FRACTION)),
    )
    # Deterministic: sort then pick first N. Keeps tests stable.
    return sorted(candidates)[:limit]


async def start_canary(
    pool: asyncpg.Pool,
    *,
    proposal_id: int,
    baseline_version_id: int,
    candidate_values: dict[str, Any],
    slot_ids: list[int],
    canary_cfg: CanaryConfig | None = None,
) -> int:
    """Activate a slot-scoped config_version for `slot_ids` and record a
    canary_assignments row. Returns canary id."""
    if not slot_ids:
        raise ValueError("slot_ids must be non-empty")
    if len(slot_ids) > safety.MAX_CANARY_SLOTS_ABSOLUTE:
        raise ValueError(
            f"canary slot count {len(slot_ids)} > "
            f"MAX_CANARY_SLOTS_ABSOLUTE={safety.MAX_CANARY_SLOTS_ABSOLUTE}"
        )
    canary_cfg = canary_cfg or CanaryConfig()
    canary_version_id = await propose_version(
        pool,
        created_by="canary",
        source="canary",
        rationale=f"Canary for proposal #{proposal_id} on slots {sorted(slot_ids)}",
        values=candidate_values,
        parent_id=baseline_version_id,
        proposal_id=proposal_id,
        scope_kind="slots",
        slot_ids=slot_ids,
    )
    await activate_version(pool, canary_version_id, activated_by="canary")
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """INSERT INTO canary_assignments
               (proposal_id, canary_version_id, baseline_version_id,
                slot_ids, status, min_trades_required, required_ci_bps)
               VALUES ($1,$2,$3,$4,'running',$5,$6)
               RETURNING id""",
            proposal_id, canary_version_id, baseline_version_id,
            slot_ids, canary_cfg.min_trades_required,
            canary_cfg.required_ci_bps,
        )
        await c.execute(
            "UPDATE tuning_proposals SET status='canary_running', canary_id=$1 WHERE id=$2",
            row["id"], proposal_id,
        )
    log.info("canary_started", extra={"id": row["id"], "slots": slot_ids})
    return int(row["id"])


async def _fetch_canary_trades(
    pool: asyncpg.Pool, *, canary_id: int,
) -> dict[str, list[dict]]:
    """Return {'canary': [...], 'baseline': [...]} of trade_outcomes rows
    for trades that opened after canary start, split by slot_ids."""
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """SELECT slot_ids, started_at FROM canary_assignments WHERE id=$1""",
            canary_id,
        )
        if row is None:
            return {"canary": [], "baseline": []}
        canary_slots = list(row["slot_ids"])
        rows = await c.fetch(
            """SELECT slot_id, net_pnl_pct, net_pnl_eur
               FROM trade_outcomes
               WHERE opened_at >= $1""",
            row["started_at"],
        )
    canary_rows = []
    baseline_rows = []
    for r in rows:
        d = dict(r)
        if r["slot_id"] in canary_slots:
            canary_rows.append(d)
        else:
            baseline_rows.append(d)
    return {"canary": canary_rows, "baseline": baseline_rows}


async def evaluate_canary(pool: asyncpg.Pool, *, canary_id: int,
                            canary_cfg: CanaryConfig | None = None,
                            persist: bool = True) -> CanaryVerdict:
    canary_cfg = canary_cfg or CanaryConfig()
    trades = await _fetch_canary_trades(pool, canary_id=canary_id)
    c_pct = [float(r["net_pnl_pct"]) for r in trades["canary"]]
    b_pct = [float(r["net_pnl_pct"]) for r in trades["baseline"]]

    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT started_at, slot_ids FROM canary_assignments WHERE id=$1",
            canary_id,
        )
    if row is None:
        return CanaryVerdict(status=ABORTED, reason="missing",
                              n_baseline=0, n_canary=0)
    started_at = row["started_at"]
    age_days = (datetime.now(timezone.utc) - started_at).days

    if len(c_pct) < canary_cfg.min_trades_required:
        if age_days >= canary_cfg.max_duration_days:
            verdict = CanaryVerdict(
                status=ABORTED,
                reason=f"did_not_reach_min_trades_in_{canary_cfg.max_duration_days}d",
                n_baseline=len(b_pct), n_canary=len(c_pct),
            )
            if persist:
                await _persist_verdict(pool, canary_id, verdict)
            return verdict
        return CanaryVerdict(
            status=RUNNING, reason="accumulating_trades",
            n_baseline=len(b_pct), n_canary=len(c_pct),
        )

    # DD trigger on canary arm (code-level safety constant).
    dd_trigger = _max_running_drawdown_pct(
        [float(r["net_pnl_eur"]) for r in trades["canary"]]
    )
    if dd_trigger >= safety.ROLLBACK_DD_BREACH_PCT:
        verdict = CanaryVerdict(
            status=FAIL, reason=f"dd_breach_{dd_trigger:.2f}%",
            n_baseline=len(b_pct), n_canary=len(c_pct),
        )
        if persist:
            await _persist_verdict(pool, canary_id, verdict)
        return verdict

    if not b_pct:
        # No baseline trades in the window — can't compare. Defer.
        return CanaryVerdict(
            status=RUNNING, reason="no_baseline_comparand",
            n_baseline=0, n_canary=len(c_pct),
        )

    observed, lo, hi = two_sample_delta_ci(
        b_pct, c_pct, n_samples=safety.BOOTSTRAP_SAMPLES, rng_seed=42,
    )
    observed_bps = observed * 100
    lo_bps = lo * 100
    hi_bps = hi * 100
    if lo_bps >= canary_cfg.required_ci_bps:
        verdict = CanaryVerdict(
            status=PASS, reason="ci_lo_beats_baseline",
            n_baseline=len(b_pct), n_canary=len(c_pct),
            observed_delta_bps=observed_bps,
            ci_lo_bps=lo_bps, ci_hi_bps=hi_bps,
        )
    elif hi_bps <= -canary_cfg.required_ci_bps:
        verdict = CanaryVerdict(
            status=FAIL, reason="ci_hi_below_baseline",
            n_baseline=len(b_pct), n_canary=len(c_pct),
            observed_delta_bps=observed_bps,
            ci_lo_bps=lo_bps, ci_hi_bps=hi_bps,
        )
    else:
        if age_days >= canary_cfg.max_duration_days:
            verdict = CanaryVerdict(
                status=ABORTED, reason="inconclusive_after_max_duration",
                n_baseline=len(b_pct), n_canary=len(c_pct),
                observed_delta_bps=observed_bps,
                ci_lo_bps=lo_bps, ci_hi_bps=hi_bps,
            )
        else:
            return CanaryVerdict(
                status=RUNNING, reason="ci_straddles_baseline",
                n_baseline=len(b_pct), n_canary=len(c_pct),
                observed_delta_bps=observed_bps,
                ci_lo_bps=lo_bps, ci_hi_bps=hi_bps,
            )
    if persist:
        await _persist_verdict(pool, canary_id, verdict)
    return verdict


def _max_running_drawdown_pct(per_trade_eur: list[float]) -> float:
    cum, peak, dd = 0.0, 0.0, 0.0
    for v in per_trade_eur:
        cum += v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return (dd / peak * 100.0) if peak > 0 else 0.0


async def _persist_verdict(
    pool: asyncpg.Pool, canary_id: int, verdict: CanaryVerdict,
) -> None:
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT proposal_id, canary_version_id FROM canary_assignments WHERE id=$1",
            canary_id,
        )
        await c.execute(
            """UPDATE canary_assignments
               SET status=$1, ended_at=NOW(), result=$2::jsonb
               WHERE id=$3""",
            verdict.status,
            json.dumps({
                "reason": verdict.reason,
                "n_baseline": verdict.n_baseline,
                "n_canary": verdict.n_canary,
                "observed_delta_bps": verdict.observed_delta_bps,
                "ci_lo_bps": verdict.ci_lo_bps,
                "ci_hi_bps": verdict.ci_hi_bps,
            }),
            canary_id,
        )
        # On fail/abort, retire the canary version so the trader stops
        # using it immediately.
        if verdict.status in (FAIL, ABORTED):
            await c.execute(
                """UPDATE config_versions SET deactivated_at=NOW(),
                     deactivated_by='canary', deactivated_reason=$2
                   WHERE id=$1""",
                row["canary_version_id"], f"canary_{verdict.status}",
            )
        new_proposal_status = {
            PASS: "canary_passed", FAIL: "canary_failed",
            ABORTED: "canary_failed",
        }[verdict.status]
        await c.execute(
            "UPDATE tuning_proposals SET status=$1 WHERE id=$2",
            new_proposal_status, row["proposal_id"],
        )
