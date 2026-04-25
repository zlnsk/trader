"""Cumulative-drift detector.

Individual proposals are capped at 15%. Over many cycles, the optimizer
could walk a parameter arbitrarily far. This detector flags when a
managed-key's active value differs from its earliest-bootstrap value
by more than DRIFT_WARN_PCT.

Runs as part of anomaly.scan.
"""
from __future__ import annotations

import json
import logging

import asyncpg

from ..config_store.versions import _values_of, active_global_version

log = logging.getLogger("optimizer.anomaly.drift")

DRIFT_WARN_PCT = 30.0     # warn when active value differs by > 30%
DRIFT_CRIT_PCT = 60.0     # critical when > 60%


async def detect_drift(pool: asyncpg.Pool) -> list[int]:
    """Emits an optimizer_findings row per drifted key. Returns the list
    of finding ids created."""
    active = await active_global_version(pool)
    if active is None:
        return []
    async with pool.acquire() as c:
        oldest = await c.fetchrow(
            """SELECT id FROM config_versions
                WHERE source='bootstrap'
             ORDER BY id ASC LIMIT 1"""
        )
    if oldest is None:
        return []
    bootstrap_vals = await _values_of(pool, oldest["id"])
    active_vals = await _values_of(pool, active["id"])

    ids: list[int] = []
    for key, old in bootstrap_vals.items():
        new = active_vals.get(key)
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            continue
        if old == 0:
            continue
        drift_pct = abs((float(new) - float(old)) / float(old)) * 100.0
        if drift_pct < DRIFT_WARN_PCT:
            continue
        severity = "critical" if drift_pct >= DRIFT_CRIT_PCT else "warning"
        async with pool.acquire() as c:
            existing = await c.fetchrow(
                """SELECT id FROM optimizer_findings
                    WHERE detector='cumulative_drift' AND subject=$1
                      AND resolved_at IS NULL""",
                f"drift:{key}",
            )
            if existing:
                continue
            row = await c.fetchrow(
                """INSERT INTO optimizer_findings
                   (detector, severity, subject, body, evidence)
                   VALUES ('cumulative_drift',$1,$2,$3,$4::jsonb)
                   RETURNING id""",
                severity, f"drift:{key}",
                f"{key}: bootstrap={old}, active={new}, drift={drift_pct:.1f}%",
                json.dumps({"key": key, "bootstrap": old,
                             "active": new, "drift_pct": drift_pct}),
            )
        ids.append(int(row["id"]))
        log.warning("drift_finding_emitted", extra={
            "key": key, "drift_pct": drift_pct, "severity": severity,
        })
    return ids
