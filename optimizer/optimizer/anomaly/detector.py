"""Anomaly + degradation detection.

Writes `optimizer_findings` rows. Each detector is pure — it reads
metrics + recent trade_outcomes and emits findings. Findings are then
consumed by the hypothesis sources OR surfaced directly in the
dashboard when severity=critical.

Detectors here are STATISTICAL rules, not LLM-backed. The "LLM-can-
reason-about-context" angle lives in `hypothesis/llm_failure.py` +
`hypothesis/llm_strategic.py`.

Deliberately small + readable. Bloat is the enemy — a detector that
false-positives once a day becomes a detector the operator ignores.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from .. import safety

log = logging.getLogger("optimizer.anomaly")


async def _emit(pool: asyncpg.Pool, *, detector: str, severity: str,
                 subject: str, body: str, evidence: dict,
                 dedup_hours: int = 6) -> int | None:
    """Insert a finding if one with same (detector, subject) isn't
    already open within the last `dedup_hours`. Returns new id or None."""
    async with pool.acquire() as c:
        existing = await c.fetchrow(
            """SELECT id FROM optimizer_findings
                WHERE detector=$1 AND subject=$2
                  AND resolved_at IS NULL
                  AND ts > NOW() - ($3::text || ' hours')::interval""",
            detector, subject, str(dedup_hours),
        )
        if existing:
            return None
        row = await c.fetchrow(
            """INSERT INTO optimizer_findings
               (detector, severity, subject, body, evidence)
               VALUES ($1,$2,$3,$4,$5::jsonb) RETURNING id""",
            detector, severity, subject, body, json.dumps(evidence),
        )
    log.info("finding_emitted", extra={"id": int(row["id"]), "detector": detector})
    return int(row["id"])


async def _detect_drawdown_breach(pool: asyncpg.Pool) -> None:
    """If any slot's 7-day rolling max_dd_pct exceeds ROLLBACK_DD_BREACH_PCT,
    emit a critical finding. This also seeds the rollback trigger path."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT slot_id, max_dd_pct, n_samples
                 FROM metrics_slot_rolling
                WHERE window_days=7 AND config_version_id=0
                  AND as_of_date >= CURRENT_DATE - INTERVAL '1 day'
                  AND n_samples >= 5
                  AND max_dd_pct >= $1""",
            safety.ROLLBACK_DD_BREACH_PCT,
        )
    for r in rows:
        await _emit(
            pool, detector="drawdown_breach", severity="critical",
            subject=f"slot_{r['slot_id']}_dd_{float(r['max_dd_pct']):.1f}pct",
            body=(f"Slot {r['slot_id']} 7-day drawdown "
                   f"{float(r['max_dd_pct']):.2f}% breached the "
                   f"{safety.ROLLBACK_DD_BREACH_PCT}% safety limit."),
            evidence={"slot_id": r["slot_id"],
                       "window_days": 7,
                       "max_dd_pct": float(r["max_dd_pct"]),
                       "n_samples": int(r["n_samples"])},
        )


async def _detect_pf_regression(pool: asyncpg.Pool) -> None:
    """For each slot, compare current 7d PF to 30d PF. If current <
    30d * (1 - ROLLBACK_PF_DROP) and both have ≥ MIN_N_SAMPLES, emit."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            """WITH recent AS (
                 SELECT slot_id, profit_factor, n_samples
                   FROM metrics_slot_rolling
                  WHERE window_days=7 AND config_version_id=0
                    AND as_of_date >= CURRENT_DATE - INTERVAL '1 day'
               ), baseline AS (
                 SELECT slot_id, profit_factor, n_samples
                   FROM metrics_slot_rolling
                  WHERE window_days=30 AND config_version_id=0
                    AND as_of_date >= CURRENT_DATE - INTERVAL '1 day'
               )
               SELECT r.slot_id,
                      r.profit_factor AS pf_7d,  r.n_samples AS n_7d,
                      b.profit_factor AS pf_30d, b.n_samples AS n_30d
                 FROM recent r JOIN baseline b USING (slot_id)
                WHERE r.n_samples >= $1 AND b.n_samples >= $1""",
            safety.MIN_N_SAMPLES // 2,  # relaxed vs absolute MIN_N for detection
        )
    for r in rows:
        pf7 = float(r["pf_7d"] or 0)
        pf30 = float(r["pf_30d"] or 0)
        if pf30 <= 0:
            continue
        drop = 1 - (pf7 / pf30)
        if drop >= safety.ROLLBACK_PF_DROP:
            await _emit(
                pool, detector="pf_regression", severity="warning",
                subject=f"slot_{r['slot_id']}_pf_drop_{int(drop*100)}pct",
                body=(f"Slot {r['slot_id']} profit-factor dropped "
                       f"{drop*100:.1f}% (7d={pf7:.2f} vs 30d={pf30:.2f})."),
                evidence={"slot_id": r["slot_id"], "pf_7d": pf7,
                           "pf_30d": pf30, "drop_pct": drop * 100},
            )


async def _detect_frequency_collapse(pool: asyncpg.Pool) -> None:
    """Per-slot 7d trade count vs 30d expectation."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            """WITH recent AS (
                 SELECT slot_id, n_samples AS n_7d FROM metrics_slot_rolling
                  WHERE window_days=7 AND config_version_id=0
                    AND as_of_date >= CURRENT_DATE - INTERVAL '1 day'
               ), baseline AS (
                 SELECT slot_id, n_samples AS n_30d FROM metrics_slot_rolling
                  WHERE window_days=30 AND config_version_id=0
                    AND as_of_date >= CURRENT_DATE - INTERVAL '1 day'
               )
               SELECT r.slot_id, r.n_7d, b.n_30d
                 FROM recent r JOIN baseline b USING (slot_id)
                WHERE b.n_30d >= 20"""
        )
    for r in rows:
        n_7d = int(r["n_7d"])
        n_30d = int(r["n_30d"])
        expected_7d = n_30d / 30.0 * 7
        if expected_7d <= 0:
            continue
        drop_pct = (1 - n_7d / expected_7d) * 100
        if drop_pct >= safety.ROLLBACK_FREQ_COLLAPSE_PCT:
            await _emit(
                pool, detector="frequency_collapse", severity="warning",
                subject=f"slot_{r['slot_id']}_trades_drop_{int(drop_pct)}pct",
                body=(f"Slot {r['slot_id']} trade count collapsed "
                       f"(7d={n_7d} vs expected {expected_7d:.1f} "
                       f"from 30d pace)."),
                evidence={"slot_id": r["slot_id"], "n_7d": n_7d,
                           "n_30d": n_30d,
                           "expected_7d": expected_7d,
                           "drop_pct": drop_pct},
            )


async def _detect_data_quality(pool: asyncpg.Pool) -> None:
    """Basic sanity rules over signal_snapshots freshness + fields."""
    async with pool.acquire() as c:
        # Freshness: no new snapshot in last 2 hours (when bot is supposed
        # to be scanning) -> warning.
        latest = await c.fetchrow(
            "SELECT MAX(snapshot_ts) AS ts FROM signal_snapshots"
        )
    if latest is None or latest["ts"] is None:
        return
    age_hours = (datetime.now(timezone.utc) - latest["ts"]).total_seconds() / 3600
    if age_hours > 4:
        await _emit(
            pool, detector="data_quality", severity="warning",
            subject="signal_snapshots_stale",
            body=f"Latest signal_snapshot is {age_hours:.1f}h old.",
            evidence={"age_hours": age_hours, "latest_ts": str(latest["ts"])},
        )


async def _detect_drift(pool):
    from .drift import detect_drift
    await detect_drift(pool)


DETECTORS = (
    _detect_drawdown_breach,
    _detect_pf_regression,
    _detect_frequency_collapse,
    _detect_data_quality,
    _detect_drift,
)


async def scan(pool: asyncpg.Pool) -> None:
    for d in DETECTORS:
        try:
            await d(pool)
        except Exception:  # noqa: BLE001
            log.exception("detector_failed: %s", d.__name__)
