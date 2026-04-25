"""LLM spend tracking + per-day budget gate.

Every call in llm.py routes its usage numbers through `record_usage`. Before a
call, `budget_allows` checks whether today's cumulative spend exceeds
LLM_DAILY_BUDGET_USD in config. Budget exhaustion returns a safe fallback
(abstain / bypassed) rather than halting the bot.

Cost is computed from input/output tokens via a small per-model table; update
as pricing shifts. Cached tokens count at 10% of input price (OpenRouter /
Anthropic standard).
"""
from __future__ import annotations

import logging
from datetime import date

import asyncpg

log = logging.getLogger("bot.cost")

# Per-1M-token prices, USD. Update as OpenRouter pricing shifts.
# Values tracked from OpenRouter listing 2026-04. Numbers are defensive — err
# on the side of over-counting so budget caps bite early rather than late.
PRICES: dict[str, dict[str, float]] = {
    "anthropic/claude-opus-4.7":          {"input": 15.0, "output": 75.0, "cached": 1.5},
    "anthropic/claude-opus-4.7:online":   {"input": 15.0, "output": 75.0, "cached": 1.5},
    "anthropic/claude-sonnet-4.6":        {"input": 3.0,  "output": 15.0, "cached": 0.3},
    "anthropic/claude-haiku-4.5":         {"input": 1.0,  "output": 5.0,  "cached": 0.1},
    "perplexity/sonar":                   {"input": 1.0,  "output": 1.0,  "cached": 0.1},
    "google/gemini-2.5-flash":            {"input": 0.30, "output": 2.50, "cached": 0.075},
    "google/gemini-2.5-flash:online":     {"input": 0.30, "output": 2.50, "cached": 0.075},
    "_default":                           {"input": 5.0,  "output": 25.0, "cached": 0.5},
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int,
                      cached_tokens: int = 0) -> float:
    p = PRICES.get(model) or PRICES["_default"]
    uncached_in = max(0, input_tokens - cached_tokens)
    return (uncached_in * p["input"] + cached_tokens * p["cached"]
            + output_tokens * p["output"]) / 1_000_000.0


async def record_usage(pool: asyncpg.Pool, touchpoint: str, model: str,
                        input_tokens: int, output_tokens: int,
                        cached_tokens: int = 0, meta: dict | None = None,
                        strategy: str = "mean_rev") -> float:
    cost = estimate_cost_usd(model, input_tokens, output_tokens, cached_tokens)
    try:
        async with pool.acquire() as c:
            await c.execute(
                """INSERT INTO llm_spend (touchpoint, model, input_tokens, output_tokens,
                    cached_tokens, cost_usd, meta, strategy)
                   VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8)""",
                touchpoint, model, input_tokens, output_tokens, cached_tokens, cost,
                meta or {}, strategy,
            )
    except Exception as exc:
        log.warning("llm_spend_write_failed err=%s", exc)
    return cost


async def spent_today_usd(pool: asyncpg.Pool, strategy: str | None = None) -> float:
    """Today's LLM spend in USD, globally or scoped to a single strategy."""
    try:
        async with pool.acquire() as c:
            if strategy is None:
                row = await c.fetchrow(
                    """SELECT COALESCE(SUM(cost_usd), 0) AS spent
                       FROM llm_spend WHERE ts::date = $1""",
                    date.today(),
                )
            else:
                row = await c.fetchrow(
                    """SELECT COALESCE(SUM(cost_usd), 0) AS spent
                       FROM llm_spend WHERE ts::date = $1 AND strategy = $2""",
                    date.today(), strategy,
                )
        return float(row["spent"] or 0)
    except Exception as exc:
        log.warning("llm_spend_read_failed err=%s", exc)
        return 0.0


async def _per_strategy_cap(pool: asyncpg.Pool, strategy: str) -> float | None:
    """Per-strategy daily USD cap from llm_budget_per_strategy. None means
    'no cap row' (fall through to global); 0 also means no cap."""
    try:
        async with pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT daily_usd_cap FROM llm_budget_per_strategy WHERE strategy=$1",
                strategy,
            )
    except Exception:
        return None
    if row is None or row["daily_usd_cap"] is None:
        return None
    cap = float(row["daily_usd_cap"])
    return cap if cap > 0 else None


async def budget_allows(pool: asyncpg.Pool, cfg: dict,
                         strategy: str = "mean_rev") -> bool:
    """True if the call is still under both the per-strategy cap AND the
    global LLM_DAILY_BUDGET_USD cap. Either cap missing/zero = unlimited on
    that axis. Belt-and-braces: a runaway strategy cannot exhaust another
    strategy's budget, and the global cap still applies."""
    per_cap = await _per_strategy_cap(pool, strategy)
    if per_cap is not None:
        spent_strategy = await spent_today_usd(pool, strategy)
        if spent_strategy >= per_cap:
            return False
    budget = float(cfg.get("LLM_DAILY_BUDGET_USD", 0) or 0)
    if budget <= 0:
        return True
    spent = await spent_today_usd(pool)
    return spent < budget
