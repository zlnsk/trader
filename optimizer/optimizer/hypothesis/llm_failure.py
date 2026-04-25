"""LLM-driven failure clustering.

Premise: a Bayesian optimizer finds the best point in a bounded
parameter box. An LLM finds the *structural* reason a cluster of
trades lost money — e.g. "all -3% losers this week happened in
risk_off at 13:30 UTC on stocks without VWAP support". That kind of
observation motivates a different class of hypothesis than what TPE
can surface.

Input: a window of recent losing closed trades + their denormalised
features.

Output (strict JSON, enforced): a list of 0-3 proposals, each naming
keys in config_managed_keys with `from`/`to` floats + rationale.

Every proposal still passes through the adversary. The LLM cannot
apply anything.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from .. import safety
from ..config_store import active_global_version
from ..config_store.versions import _values_of, get_managed_keys
from ..llm import chat, LLMError

log = logging.getLogger("optimizer.hypothesis.llm_failure")


_SYSTEM_PROMPT = """You are the failure-cluster analyst for a trading optimizer.
You receive a JSON object with:
  - losing_trades: list of recent losing trades with features
  - current_config: dict of the active thresholds
  - managed_keys: dict of {key: {min, max}} bounds

Your job: spot a structural pattern in the losers and propose at most 3
parameter changes that would have rejected that cluster. Every proposal
must cite which cluster feature motivated it.

Respond with a single JSON object shaped EXACTLY like:
{
  "cluster_summary": "string, one sentence",
  "proposals": [
    {"key": "QUANT_SCORE_MIN", "from": 50, "to": 55, "reason": "..."}
  ]
}

Rules you MUST follow:
  - Each proposal's `key` must be one of the managed_keys
  - Each `to` must be within that key's [min, max]
  - Each `to` must be within 15% of the current value
  - If you see no structural pattern, return proposals: []
  - Do NOT propose anything speculative — cite specific numbers
"""


async def _fetch_losing_trades(pool: asyncpg.Pool,
                                 window_days: int = 14,
                                 limit: int = 80) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT symbol, slot_id, strategy, net_pnl_pct, fees_eur,
                      hold_seconds, entry_rsi, entry_ibs, entry_sigma,
                      entry_score, entry_regime, entry_day_of_week,
                      entry_minute_of_day, exit_reason, closed_at
                 FROM trade_outcomes
                WHERE closed_at >= $1 AND net_pnl_pct < 0
                ORDER BY closed_at DESC
                LIMIT $2""",
            since, limit,
        )
    return [dict(r) for r in rows]


async def propose(pool: asyncpg.Pool) -> list[int]:
    """Run the LLM failure-cluster step. Returns list of tuning_proposals
    ids created. Returns [] if insufficient data or LLM bounced."""
    losers = await _fetch_losing_trades(pool)
    if len(losers) < 10:
        log.info("llm_failure: too few losers (%d)", len(losers))
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
            pool, purpose="failure_cluster",
            system=_SYSTEM_PROMPT,
            user=json.dumps({
                "losing_trades": [
                    {**t, "closed_at": str(t["closed_at"])} for t in losers
                ],
                "current_config": current,
                "managed_keys": managed,
            }, default=str),
        )
    except LLMError as exc:
        log.warning("llm_failure_llm_error: %s", exc)
        return []

    cluster_summary = parsed.get("cluster_summary") or "(no summary)"
    candidates = parsed.get("proposals") or []
    if not isinstance(candidates, list):
        return []
    ids: list[int] = []
    async with pool.acquire() as c:
        for p in candidates[:3]:
            if not isinstance(p, dict):
                continue
            key = p.get("key")
            to = p.get("to")
            frm = p.get("from")
            reason = p.get("reason") or ""
            if key not in mk:
                continue
            if not isinstance(to, (int, float)):
                continue
            # Bounds sanity (adversary will re-check, but fail-fast saves a row)
            mk_ = mk[key]
            if mk_.min_value is not None and to < mk_.min_value:
                continue
            if mk_.max_value is not None and to > mk_.max_value:
                continue
            row = await c.fetchrow(
                """INSERT INTO tuning_proposals
                   (proposal, rationale, source, status)
                   VALUES ($1::jsonb, $2, 'llm_failure', 'pending')
                   RETURNING id""",
                {
                    "generator": "llm_failure",
                    "proposals": [{"key": key, "from": frm, "to": float(to)}],
                    "cluster_summary": cluster_summary,
                },
                f"llm_failure: {cluster_summary} :: {reason}",
            )
            ids.append(int(row["id"]))
    log.info("llm_failure_proposals", extra={"ids": ids})
    return ids
