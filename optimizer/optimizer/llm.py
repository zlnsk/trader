"""OpenRouter client for the optimizer.

Separate from bot/llm.py because:
  1. Optimizer has its own daily USD budget cap (safety.OPTIMIZER_DAILY_LLM_USD_BUDGET)
  2. No :online suffix — we don't want the LLM consulting the internet
     when reasoning about our own private trade clusters
  3. Schema validation is simpler (each callsite defines its own output shape)

Never calls into bot/ code. Stays fully self-contained.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from . import safety

log = logging.getLogger("optimizer.llm")


URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.getenv(
    "OPTIMIZER_LLM_MODEL", "anthropic/claude-opus-4.7",
)


# Per-1M-token prices in USD. Mirrors bot/cost.py PRICES; kept inline because
# this module is intentionally self-contained (no bot/ imports). Update both
# tables in lockstep when OpenRouter pricing shifts.
_PRICES: dict[str, tuple[float, float]] = {
    "anthropic/claude-opus-4.7":        (15.0, 75.0),
    "anthropic/claude-opus-4.7:online": (15.0, 75.0),
    "anthropic/claude-sonnet-4.6":      (3.0, 15.0),
    "anthropic/claude-haiku-4.5":       (1.0, 5.0),
}
_DEFAULT_PRICE = (5.0, 25.0)


def _estimate_cost_usd(model: str, tok_in: int, tok_out: int) -> float:
    p_in, p_out = _PRICES.get(model, _DEFAULT_PRICE)
    return (tok_in * p_in + tok_out * p_out) / 1_000_000.0


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


def _key() -> str | None:
    k = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    return k if k and k != "TOFILL" else None


async def _current_spend_today(pool) -> float:
    import datetime as _dt
    today = _dt.date.today()
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """SELECT COALESCE(SUM(cost_usd), 0) AS total
                 FROM llm_spend
                WHERE ts::date = $1 AND touchpoint LIKE 'optimizer:%'""",
            today,
        )
    return float(row["total"] or 0)


async def chat(
    pool,
    *,
    purpose: str,
    system: str,
    user: str,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 800,
) -> dict[str, Any]:
    """Send a message, return parsed JSON response. Raises LLMError on
    budget / auth / parse issues. `purpose` is stored in llm_spend for
    cost attribution."""
    spend = await _current_spend_today(pool)
    if spend >= safety.OPTIMIZER_DAILY_LLM_USD_BUDGET:
        raise BudgetExceeded(
            f"optimizer daily budget ${safety.OPTIMIZER_DAILY_LLM_USD_BUDGET} "
            f"exhausted (spent ${spend:.2f})"
        )
    key = _key()
    if key is None:
        raise LLMError("no OPENROUTER_API_KEY")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://trader.example.com",
        "X-Title": "Trader Optimizer",
    }
    payload = {
        "model": model or DEFAULT_MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "usage": {"include": True},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(URL, headers=headers, json=payload)
    if r.status_code >= 400:
        raise LLMError(f"openrouter http {r.status_code}: {r.text[:300]}")
    body = r.json()
    try:
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, json.JSONDecodeError) as exc:
        raise LLMError(f"bad response shape: {exc}") from exc

    # Cost capture: OpenRouter does not surface a `total_cost` field in usage,
    # so compute from token counts × the local price table.
    usage = body.get("usage") or {}
    tok_in = int(usage.get("prompt_tokens") or 0)
    tok_out = int(usage.get("completion_tokens") or 0)
    cost = _estimate_cost_usd(payload["model"], tok_in, tok_out)
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO llm_spend
               (ts, touchpoint, model, input_tokens, output_tokens, cost_usd)
               VALUES (NOW(),$1,$2,$3,$4,$5)""",
            f"optimizer:{purpose}",
            payload["model"],
            tok_in,
            tok_out,
            cost,
        )
    return parsed
