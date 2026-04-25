"""Opportunity discovery: symmetric to failure clustering, but for wins.

Asks the LLM: given this cluster of winning trades, what parameter
would let us take MORE of them? The LLM can propose loosening a
threshold. Same hard caps apply (param cap, managed_keys bounds,
adversary). Disabled by default (optimizer_source_flags).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from ..config_store import active_global_version
from ..config_store.versions import _values_of, get_managed_keys
from ..llm import chat, LLMError

log = logging.getLogger("optimizer.hypothesis.llm_opportunity")


_SYSTEM = """You are the opportunity analyst for a trading optimizer.
You receive a sample of winning trades and the current thresholds.
Find clusters of wins we're partly missing (we accepted some but probably
rejected similar ones at the gate). Propose AT MOST 2 parameter loosenings
that could have captured more of them.

Respond with JSON:
{
  "opportunity_summary": "...",
  "proposals": [{"key":"...", "from": 1.5, "to": 1.3, "reason": "..."}]
}

Every proposal's `to` must be within 15% of `from` and within the
managed_keys bounds.
"""


async def _fetch_winners(pool: asyncpg.Pool,
                           window_days: int = 14,
                           limit: int = 80) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT symbol, slot_id, net_pnl_pct, hold_seconds,
                      entry_rsi, entry_ibs, entry_sigma, entry_score,
                      entry_regime, entry_day_of_week, entry_minute_of_day
                 FROM trade_outcomes
                WHERE closed_at >= $1 AND net_pnl_pct > 0
                ORDER BY net_pnl_pct DESC
                LIMIT $2""",
            since, limit,
        )
    return [dict(r) for r in rows]


async def propose(pool: asyncpg.Pool) -> list[int]:
    winners = await _fetch_winners(pool)
    if len(winners) < 10:
        return []
    active = await active_global_version(pool)
    if active is None:
        return []
    current = await _values_of(pool, active["id"])
    mk = await get_managed_keys(pool)
    managed = {k: {"min": v.min_value, "max": v.max_value}
                for k, v in mk.items()}
    try:
        parsed = await chat(
            pool, purpose="opportunity",
            system=_SYSTEM,
            user=json.dumps({
                "winning_trades": winners,
                "current_config": current,
                "managed_keys": managed,
            }, default=str),
        )
    except LLMError as exc:
        log.warning("llm_opportunity_llm_error: %s", exc)
        return []
    summary = parsed.get("opportunity_summary") or "(no summary)"
    candidates = parsed.get("proposals") or []
    ids: list[int] = []
    async with pool.acquire() as c:
        for p in candidates[:2]:
            if not isinstance(p, dict):
                continue
            key = p.get("key")
            to = p.get("to")
            if key not in mk or not isinstance(to, (int, float)):
                continue
            row = await c.fetchrow(
                """INSERT INTO tuning_proposals
                   (proposal, rationale, source, status)
                   VALUES ($1::jsonb, $2, 'llm_opportunity', 'pending')
                   RETURNING id""",
                {
                    "generator": "llm_opportunity",
                    "proposals": [{"key": key, "from": p.get("from"),
                                     "to": float(to)}],
                    "opportunity_summary": summary,
                },
                f"llm_opportunity: {summary} :: {p.get('reason', '')}",
            )
            ids.append(int(row["id"]))
    return ids
