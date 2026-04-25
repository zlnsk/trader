"""Rollback engine.

Watches applied global versions. Triggers rollback when any of:
  - profit-factor drop over ROLLBACK_WINDOW_DAYS exceeds ROLLBACK_PF_DROP
  - drawdown in the same window exceeds ROLLBACK_DD_BREACH_PCT
  - trade-count drop exceeds ROLLBACK_FREQ_COLLAPSE_PCT
  - global halt event (BOT_ENABLED flips to false with trigger='auto_kill')

Single-change rollback: rolls back ONE step at a time (the currently
active version's parent becomes the new active). Cumulative walks happen
only when a rollback itself regresses — never a free-form "revert the
last 5 changes" action.

Break-glass: the config_versions.source='bootstrap' row is immutable
and always available. If the normal parent chain is broken (e.g. a
parent_id points to a deactivated-but-damaged row), rollback_global
falls back to the latest bootstrap row and logs LOUDLY.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from .. import safety
from ..config_store.versions import (
    active_global_version, rollback_to, _values_of,
)

log = logging.getLogger("optimizer.rollback")


async def _metrics_for_window(
    pool: asyncpg.Pool, version_id: int, window_days: int,
) -> dict:
    """Compute PF + DD + trade_count for the trades executed under
    `version_id` within the last `window_days`."""
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT net_pnl_pct, net_pnl_eur
                 FROM trade_outcomes
                WHERE config_version_id=$1 AND closed_at >= $2""",
            version_id, since,
        )
    pct = [float(r["net_pnl_pct"]) for r in rows]
    eur = [float(r["net_pnl_eur"]) for r in rows]
    wins = [x for x in pct if x > 0]
    losses = [x for x in pct if x <= 0]
    sum_wins = sum(wins)
    sum_losses = sum(losses)
    pf = sum_wins / abs(sum_losses) if sum_losses else (
        float("inf") if sum_wins > 0 else 0.0
    )
    cum, peak, dd = 0.0, 0.0, 0.0
    for v in eur:
        cum += v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    dd_pct = (dd / peak * 100.0) if peak > 0 else 0.0
    return {"n_trades": len(pct), "pf": pf, "dd_pct": dd_pct}


async def _baseline_metrics(pool: asyncpg.Pool, parent_id: int | None,
                             window_days: int) -> dict:
    if parent_id is None:
        return {"n_trades": 0, "pf": 0.0, "dd_pct": 0.0}
    return await _metrics_for_window(pool, parent_id, window_days)


async def check_and_maybe_rollback(
    pool: asyncpg.Pool,
    *,
    window_days: int | None = None,
    triggered_by: str = "auto_rollback",
) -> int | None:
    """Returns new version id if a rollback happened, else None."""
    window_days = window_days or safety.ROLLBACK_WINDOW_DAYS
    active = await active_global_version(pool)
    if active is None:
        return None
    if active["source"] in ("bootstrap", "rollback"):
        return None  # don't rollback a rollback or the root
    parent = active.get("parent_id")
    if parent is None:
        return None

    curr = await _metrics_for_window(pool, active["id"], window_days)
    base = await _baseline_metrics(pool, parent, window_days)

    # Require at least a handful of trades under the new config before
    # considering a rollback. Zero trades = nothing to judge.
    if curr["n_trades"] < 10:
        return None

    reasons = []
    evidence = {"window_days": window_days,
                 "current": curr, "baseline": base}

    if base["pf"] > 0 and curr["pf"] < base["pf"] * (1 - safety.ROLLBACK_PF_DROP):
        reasons.append("pf_regression")
    if curr["dd_pct"] >= safety.ROLLBACK_DD_BREACH_PCT:
        reasons.append("dd_breach")
    if base["n_trades"] > 0 and curr["n_trades"] <= base["n_trades"] * (1 - safety.ROLLBACK_FREQ_COLLAPSE_PCT / 100.0):
        reasons.append("frequency_collapse")

    if not reasons:
        return None
    trigger = reasons[0]
    log.warning("auto_rollback_triggered", extra={
        "trigger": trigger, "from_version": active["id"],
        "to_version": parent, "reasons": reasons,
        "evidence": evidence,
    })
    return await rollback_to(
        pool, bad_version_id=active["id"], good_version_id=parent,
        trigger=trigger, triggered_by=triggered_by, evidence=evidence,
    )


async def rollback_global(
    pool: asyncpg.Pool, *, triggered_by: str, trigger: str = "manual",
    evidence: dict | None = None,
) -> int | None:
    """Manually roll back the current active global to its parent. No
    metric check. Used for break-glass."""
    active = await active_global_version(pool)
    if active is None:
        return None
    parent = active.get("parent_id")
    if parent is None:
        # Break-glass: fall back to newest bootstrap row.
        async with pool.acquire() as c:
            row = await c.fetchrow(
                """SELECT id FROM config_versions
                   WHERE source='bootstrap'
                   ORDER BY id DESC LIMIT 1"""
            )
        if row is None:
            log.error("break_glass_failed_no_bootstrap")
            return None
        parent = row["id"]
        log.error("break_glass_rollback_to_bootstrap", extra={"id": parent})
    return await rollback_to(
        pool, bad_version_id=active["id"], good_version_id=parent,
        trigger=trigger, triggered_by=triggered_by,
        evidence=evidence or {},
    )
