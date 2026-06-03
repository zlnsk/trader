"""Resolve stale optimizer_findings rows.

Findings accumulate forever — multiple modules read filtered by
`resolved_at IS NULL` (anomaly/drift.py, anomaly/detector.py,
hypothesis/llm_strategic.py) but no module ever writes resolved_at.
Without this job the noticeboard's signal:noise collapses within days.

Three policies, applied in order:

  1. addressed_by_proposal: any finding whose `proposal_id` has reached a
     terminal status (applied/superseded/rolled_back). The underlying
     concern was either fixed or explicitly retired.

  2. duplicate_superseded: when (detector, severity, subject) has more
     than one open row, keep the most recent and resolve the rest.
     Stops `anomaly.scan` and `llm_strategic` from accreting a wall of
     identical "still in drawdown" rows every cycle.

  3. age_stale: open `info` older than 3 days, open `warning` older than
     7 days. Critical is never aged out — operator must ack so a
     persistent serious problem cannot self-quiet.

Safe to call repeatedly. Ordered so dedupe and age don't fire on rows the
proposal-link rule already closed (idempotent regardless).
"""
from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger("optimizer.findings.resolve")

INFO_AGE_DAYS = 3
WARNING_AGE_DAYS = 7


async def resolve_stale(pool: asyncpg.Pool) -> dict[str, int]:
    counts = {"addressed_by_proposal": 0, "duplicate_superseded": 0, "stale_age": 0}
    async with pool.acquire() as c:
        async with c.transaction():
            r = await c.execute(
                """UPDATE optimizer_findings AS f
                       SET resolved_at = NOW(), resolution = 'addressed_by_proposal'
                     FROM tuning_proposals AS p
                     WHERE f.proposal_id = p.id
                       AND f.resolved_at IS NULL
                       AND p.status IN ('applied', 'superseded', 'rolled_back')"""
            )
            counts["addressed_by_proposal"] = int(r.split()[-1])

            r = await c.execute(
                """WITH ranked AS (
                       SELECT id,
                              row_number() OVER (
                                  PARTITION BY detector, severity, subject
                                  ORDER BY ts DESC
                              ) AS rn
                         FROM optimizer_findings
                        WHERE resolved_at IS NULL
                   )
                   UPDATE optimizer_findings
                      SET resolved_at = NOW(), resolution = 'duplicate_superseded'
                    WHERE id IN (SELECT id FROM ranked WHERE rn > 1)"""
            )
            counts["duplicate_superseded"] = int(r.split()[-1])

            r = await c.execute(
                f"""UPDATE optimizer_findings
                       SET resolved_at = NOW(), resolution = 'stale_age'
                     WHERE resolved_at IS NULL
                       AND (
                           (severity = 'info'
                              AND ts < NOW() - INTERVAL '{INFO_AGE_DAYS} days')
                        OR (severity = 'warning'
                              AND ts < NOW() - INTERVAL '{WARNING_AGE_DAYS} days')
                       )"""
            )
            counts["stale_age"] = int(r.split()[-1])

    log.info("findings_resolve_stale", extra=counts)
    return counts
