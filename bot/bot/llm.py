"""Claude via OpenRouter — all the LLM-driven decisions.

Models:
- `anthropic/claude-opus-4.7:online` for heavier calls that need news (veto, exit, regime, report, tuning).
- `anthropic/claude-haiku-4.5` for fast, frequent, no-news calls (candidate ranking, stop adjust).

Safety layers (per research rec #1 + #2):
- Daily USD budget check via cost.budget_allows — if exhausted, return None
  so the caller falls back to its safe default (abstain/bypass/no-change).
- Usage tokens logged to llm_spend after every successful call.
- Each decision function validates output through a Pydantic schema in
  pydantic_models.py — malformed output → safe default, never free-text into
  order code.
- (Removed 2026-04-25) OpenRouter cache_control hint: silently ignored because
  every system prompt here is well below Anthropic's 4096-token minimum
  cacheable prefix on Opus/Haiku 4.5. Re-add when a prompt actually exceeds
  the threshold. Verified zero cached_tokens across 30d of llm_spend.
"""
from __future__ import annotations

import json
import os
from typing import Any

import asyncpg
import httpx

from . import cost, pydantic_models as pm

URL = "https://openrouter.ai/api/v1/chat/completions"
OPUS_ONLINE = os.getenv("OPENROUTER_MODEL", "anthropic/claude-opus-4.7:online")
OPUS_OFFLINE = os.getenv("OPENROUTER_MODEL_OFFLINE", "anthropic/claude-opus-4.7")


# PR10 — per-touchpoint model routing. Each key maps a call purpose to a
# model string. When LLM_TIER_SPLIT_ENABLED=false (default) every call
# resolves to OPUS_ONLINE regardless of these keys, preserving prior
# behaviour byte-for-byte.
_MODEL_CFG_KEY_BY_TOUCHPOINT = {
    "entry_veto":     "LLM_MODEL_VETO",
    "market_regime":  "LLM_MODEL_REGIME",
    "rank":           "LLM_MODEL_RANKING",
    "stop_adjust":    "LLM_MODEL_STOP_ADJUST",
    "exit_veto":      "LLM_MODEL_EXIT_VETO",
    "news_watch":     "LLM_MODEL_NEWS",
}


def _model_for(touchpoint: str, fallback: str, cfg: dict | None) -> str:
    """Resolve touchpoint → model from cfg when LLM_TIER_SPLIT_ENABLED is on;
    else return the caller's fallback (historically OPUS_ONLINE)."""
    if not cfg:
        return fallback
    if not cfg.get("LLM_TIER_SPLIT_ENABLED"):
        return fallback
    key = _MODEL_CFG_KEY_BY_TOUCHPOINT.get(touchpoint)
    if key is None:
        return fallback
    v = cfg.get(key)
    if not isinstance(v, str) or not v:
        return fallback
    return v
HAIKU = os.getenv("OPENROUTER_MODEL_FAST", "anthropic/claude-haiku-4.5")
TITLE = "Trader"
REFERER = "https://trader.example.com"


def _key() -> str | None:
    k = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    return k if k and k != "TOFILL" else None


# Module-level context so existing call sites don't need to thread pool/cfg
# through every touchpoint. Bot.tick() calls set_context(pool, cfg) at the
# start of each loop iteration. Safe because ticks are serial within the bot.
_CTX: dict[str, Any] = {"pool": None, "cfg": None}


def set_context(pool: asyncpg.Pool | None, cfg: dict | None) -> None:
    _CTX["pool"] = pool
    _CTX["cfg"] = cfg


def _ctx_pool() -> asyncpg.Pool | None:
    return _CTX.get("pool")


def _ctx_cfg() -> dict | None:
    return _CTX.get("cfg")


async def _chat(model: str, system: str, user: str, max_tokens: int = 600,
                temperature: float = 0.2, expect_json: bool = True,
                *, touchpoint: str = "unknown",
                pool: asyncpg.Pool | None = None,
                cfg: dict | None = None,
                strategy: str = "mean_rev") -> dict | str | None:
    """Core chat wrapper. If `pool` is provided, spend is recorded to
    llm_spend and the daily budget is enforced.
    """
    key = _key()
    if not key:
        return None
    # Budget gate — returning None here means the caller falls back to its
    # safe default (abstain/bypass). We do NOT halt the bot. Per-strategy cap
    # layers on top of the global cap so a chatty strategy cannot starve the
    # others.
    if pool is not None and cfg is not None:
        if not await cost.budget_allows(pool, cfg, strategy=strategy):
            return None

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "usage": {"include": True},
    }
    if expect_json:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {key}",
        "X-Title": TITLE,
        "HTTP-Referer": REFERER,
        "Content-Type": "application/json",
    }
    import time as _time
    t0 = _time.perf_counter()
    response_valid: bool | None = None
    tokens_in = tokens_out = 0
    parsed: dict | str | None = None
    # HTTP timeout: configurable via LLM_HTTP_TIMEOUT_SEC; 45s default matches
    # historical behaviour. Promoted out of a literal so an on-call can extend
    # it for a degraded OpenRouter incident without a deploy.
    http_timeout = 45.0
    if cfg is not None:
        try:
            http_timeout = float(cfg.get("LLM_HTTP_TIMEOUT_SEC", 45) or 45)
        except (TypeError, ValueError):
            http_timeout = 45.0
    try:
        async with httpx.AsyncClient(timeout=http_timeout) as c:
            r = await c.post(URL, json=payload, headers=headers)
            r.raise_for_status()
            body = r.json()
        content = body["choices"][0]["message"]["content"]
        usage = body.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens", 0) or 0)
        tokens_out = int(usage.get("completion_tokens", 0) or 0)
        if pool is not None:
            await cost.record_usage(
                pool, touchpoint, model,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cached_tokens=int(
                    (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                ),
                meta={"id": body.get("id")},
                strategy=strategy,
            )
        if expect_json:
            try:
                parsed = json.loads(content)
                response_valid = True
            except (ValueError, TypeError):
                response_valid = False
                logging.getLogger("bot.llm").warning(
                    "llm_malformed_response touchpoint=%s model=%s raw=%s",
                    touchpoint, model, content[:500] if isinstance(content, str) else content,
                )
                parsed = None
        else:
            parsed = content
            response_valid = True
        return parsed
    except Exception:
        response_valid = False
        return None
    finally:
        latency_ms = int((_time.perf_counter() - t0) * 1000)
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO llm_calls
                           (call_purpose, model, tokens_in, tokens_out,
                            latency_ms, response_valid, strategy)
                           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                        touchpoint, model, tokens_in, tokens_out,
                        latency_ms, response_valid, strategy,
                    )
            except Exception:
                # llm_calls may not yet exist pre-migration — never crash
                # the call site on instrumentation failure.
                pass


# ── 1) Entry veto (was `check`) ────────────────────────────────────────────────

ENTRY_SYS = """You are a qualitative risk filter for a mean-reversion trading bot.
The bot detected a statistical dip (RSI low, stretched below SMA20). Do NOT predict
direction — only veto if the dip is caused by a genuine structural problem (fraud,
scandal, guidance cut, regulatory disaster, leaked earnings miss) that makes 2-week recovery unlikely.

Also return a sentiment_score (0-100) representing your confidence that this dip is a 
mean-reversion buying opportunity vs a value trap. 90-100 = strong buy signal, 50 = neutral, 
0-40 = likely trap even if not vetoed. This score is used to scale position size.

Reply STRICT JSON:
{"verdict": "allow"|"veto"|"abstain",
 "confidence": 0.0-1.0,
 "sentiment_score": 0-100,
 "dive_cause": "...",
 "recovery_likelihood": "high"|"medium"|"low",
 "red_flags": ["..."],
 "reasoning": "..."}"""


async def check(symbol: str, name: str, sector: str, metrics: dict) -> dict:
    # Include earnings proximity and divergence info for richer context
    earnings_hint = ""
    if metrics.get("earnings_blackout_reason"):
        earnings_hint = f" Earnings context: {metrics.get('earnings_blackout_reason')}."
    div_hint = ""
    if metrics.get("rsi_divergence", {}).get("detected"):
        div_hint = " Bullish RSI divergence detected (price lower low, RSI higher low)."
    prompt = (
        f"Symbol: {symbol} ({name}, {sector}). "
        f"RSI14={metrics.get('rsi')}, σ-below-SMA20={metrics.get('sigma_below_sma20')}, "
        f"last={metrics.get('last')}, SMA20={metrics.get('sma20')}."
        f"{earnings_hint}{div_hint}\n"
        "Check recent 48h news for structural red flags, leaked earnings misses, or guidance cuts."
    )
    out = await _chat(_model_for("entry_veto", OPUS_ONLINE, _ctx_cfg()),
                        ENTRY_SYS, prompt, max_tokens=600,
                       touchpoint="entry_veto", pool=_ctx_pool(), cfg=_ctx_cfg())
    if out is None:
        return {"verdict": "bypassed" if _key() is None else "abstain",
                "sentiment_score": 50,
                "reasoning": "LLM unavailable" if _key() is None else "budget/error",
                "red_flags": []}
    v = pm.parse_or_default(pm.EntryVeto, out if isinstance(out, dict) else None)
    result = v.model_dump()
    # Ensure sentiment_score always present; default 50 (neutral) if missing
    if result.get("sentiment_score") is None:
        result["sentiment_score"] = 50
    return result


# ── 2) Pre-scan market regime (cached ~60 min in DB) ──────────────────────────

REGIME_SYS = """You assess the current US/EU equity macro regime for a mean-reversion
dip-buy bot. Classify the current environment.

Reply STRICT JSON:
{"regime": "mean_reversion"|"momentum"|"risk_off"|"mixed",
 "confidence": 0.0-1.0,
 "reasoning": "..."}

Definitions:
- mean_reversion: calm or choppy tape; oversold stocks tend to bounce within 1-2 weeks.
- momentum: strong directional trend; fading dips is unprofitable.
- risk_off: crisis/selloff regime; even quality names keep falling; pause buys.
- mixed: unclear signal."""


async def market_regime() -> dict | None:
    prompt = (
        "Based on last 48h of macro news (rates, Fed, geopolitics, earnings season, VIX) "
        "what regime is US/EU large-cap equity in right now?"
    )
    out = await _chat(_model_for("market_regime", OPUS_ONLINE, _ctx_cfg()),
                        REGIME_SYS, prompt, max_tokens=400,
                       touchpoint="market_regime", pool=_ctx_pool(), cfg=_ctx_cfg())
    if out is None:
        return None
    v = pm.parse_or_default(pm.RegimeVerdict, out if isinstance(out, dict) else None)
    return v.model_dump()


# ── 3) Exit-timing veto (before selling at target) ────────────────────────────

EXIT_SYS = """You advise a dip-buy bot on whether to take profit now or hold for more.
The position is at the pre-set profit target. Bot defaults to sell; your job is to
identify cases where very recent news makes holding meaningfully better.

Reply STRICT JSON:
{"action": "sell"|"hold"|"tighten",
 "confidence": 0.0-1.0,
 "extra_target_pct": number|null,
 "reasoning": "..."}

- "sell": take the gain now (default).
- "hold": there's fresh positive news suggesting more upside; recommend higher target via extra_target_pct.
- "tighten": uncertain; move stop to entry to protect the gain."""


async def exit_veto(symbol: str, name: str, entry: float, current: float,
                    target: float, held_days: int) -> dict | None:
    prompt = (
        f"{symbol} ({name}) entry={entry}, current={current}, target={target}, held {held_days} days. "
        "The bot is about to sell at target. Fresh news that changes this?"
    )
    out = await _chat(_model_for("exit_veto", OPUS_ONLINE, _ctx_cfg()),
                        EXIT_SYS, prompt, max_tokens=400,
                       touchpoint="exit_veto", pool=_ctx_pool(), cfg=_ctx_cfg())
    if out is None:
        return None
    v = pm.parse_or_default(pm.ExitVeto, out if isinstance(out, dict) else None)
    return v.model_dump()


# ── 4) Stop-loss adjust (when position is near stop) ──────────────────────────

STOP_SYS = """You review a losing position that is approaching the stop-loss level. Was
the dive caused by sector-wide macro noise (hold) or a structural red flag
(tighten/exit_now)? Default to exit at stop unless strongly confident.

IMPORTANT: widening a stop when price moves against the position is never
permitted. The only allowed actions are hold, tighten, exit_now.

Reply STRICT JSON:
{"action": "hold"|"tighten"|"exit_now",
 "new_stop_pct": number|null,
 "confidence": 0.0-1.0,
 "reasoning": "..."}"""


async def stop_adjust(symbol: str, name: str, entry: float, current: float,
                      stop: float) -> dict | None:
    prompt = (
        f"{symbol} ({name}) entry={entry}, current={current}, stop={stop}. "
        "Approaching stop. Check for fresh structural news. Advise."
    )
    out = await _chat(_model_for("stop_adjust", OPUS_ONLINE, _ctx_cfg()),
                        STOP_SYS, prompt, max_tokens=400,
                       touchpoint="stop_adjust", pool=_ctx_pool(), cfg=_ctx_cfg())
    if out is None:
        return None
    v = pm.parse_or_default(pm.StopAdjust, out if isinstance(out, dict) else None)
    return v.model_dump()


# ── 5) Candidate ranking (when >1 symbol passes quant + veto) ─────────────────

RANK_SYS = """You are given several mean-reversion candidates that all passed quant +
veto screens. Rank them by qualitative risk/reward for a 1-2 week hold.

Reply STRICT JSON:
{"order": ["SYMBOL1","SYMBOL2",...],
 "reasoning": "..."}"""


async def rank_candidates(candidates: list[dict]) -> list[str] | None:
    if not candidates:
        return []
    prompt = "Candidates:\n" + "\n".join(
        f"- {c['symbol']} ({c.get('name')}, {c.get('sector')}): "
        f"score={c.get('score')}, RSI={c.get('rsi')}, σ={c.get('sigma')}"
        for c in candidates
    )
    out = await _chat(_model_for("rank", HAIKU, _ctx_cfg()),
                        RANK_SYS, prompt, max_tokens=200, temperature=0.0,
                       touchpoint="rank_candidates", pool=_ctx_pool(), cfg=_ctx_cfg())
    if out is None:
        return None
    v = pm.parse_or_default(pm.Ranking, out if isinstance(out, dict) else None)
    # Symbolic gate: only allow symbols that were actually in the candidate set.
    allowed = {c["symbol"] for c in candidates}
    return [s for s in v.order if s in allowed] or None


# ── 6) Daily post-mortem ──────────────────────────────────────────────────────

REPORT_SYS = """You are a trading coach reviewing one day of a dip-buy bot's decisions.
Summarize what happened, flag patterns (good and bad), and suggest concrete improvements.

Reply STRICT JSON:
{"summary": "one paragraph",
 "wins": number, "losses": number, "net_pnl": number,
 "patterns": ["..."],
 "recommendations": [{"change": "...", "why": "..."}]}"""


async def daily_report(stats: dict, signals_summary: list[dict],
                       closed_trades: list[dict]) -> dict | None:
    prompt = (
        f"Day stats: {json.dumps(stats, default=str)}\n\n"
        f"Signals (top 20 by score):\n{json.dumps(signals_summary[:20], indent=2, default=str)}\n\n"
        f"Closed trades today:\n{json.dumps(closed_trades, indent=2, default=str)}\n\n"
        "Produce the daily report."
    )
    return await _chat(OPUS_ONLINE, REPORT_SYS, prompt, max_tokens=1200, temperature=0.3,
                        touchpoint="daily_report", pool=_ctx_pool(), cfg=_ctx_cfg())


# ── 7) Self-tuning threshold proposals (weekly) ───────────────────────────────

TUNE_SYS = """You are an analyst proposing threshold tweaks to a dip-buy bot based on the
last 7 days of decisions. Do NOT propose radical changes. Favor small moves with clear
rationale tied to observed data. Only propose changes if there's a concrete pattern.

Reply STRICT JSON:
{"proposals": [
   {"key": "QUANT_SCORE_MIN"|"TARGET_PROFIT_PCT"|"STOP_LOSS_PCT"|"MIN_NET_MARGIN_EUR"|"SIGMA_BELOW_SMA20"|"RSI_BUY_THRESHOLD",
    "from": number, "to": number, "why": "..."}
 ],
 "overall_rationale": "..."}"""


# ── 8) Proactive news watcher (held positions) ────────────────────────────────

NEWS_SYS = """You scan for news on a currently-held equity position and decide whether
there is any fresh, high-impact negative development (last 4h) that warrants closing
the position NOW rather than waiting for the target/stop.

Reply STRICT JSON:
{"action": "hold"|"exit_now"|"tighten_stop",
 "severity": "none"|"low"|"medium"|"high",
 "headline": "<best single headline that informs this>",
 "reasoning": "..."}"""


async def news_watch(symbol: str, name: str, entry: float, current: float) -> dict | None:
    prompt = (
        f"Held position: {symbol} ({name}), entry={entry}, current={current}. "
        "Scan last 4 hours for fresh material news (earnings prelim, downgrade, "
        "guidance cut, litigation, product recall, regulatory action). Only flag "
        "high-severity items; ignore generic market chatter."
    )
    out = await _chat(_model_for("news_watch", OPUS_ONLINE, _ctx_cfg()),
                        NEWS_SYS, prompt, max_tokens=400, temperature=0.1,
                       touchpoint="news_watch", pool=_ctx_pool(), cfg=_ctx_cfg())
    if out is None:
        return None
    v = pm.parse_or_default(pm.NewsWatch, out if isinstance(out, dict) else None)
    return v.model_dump()


# ── 9) Pre-open briefing ───────────────────────────────────────────────────────

BRIEF_SYS = """You are a pre-market briefing assistant for a dip-buy bot. Given the
bot's overnight scan results, held positions, and the current market regime, produce
a one-paragraph actionable briefing + a ranked list of the top 5 candidate tickers
to watch at the open.

Reply STRICT JSON:
{"summary": "one paragraph — what matters for today",
 "candidates": [{"symbol": "X", "why": "..."}],
 "warnings": ["..."]}"""


async def pre_open_briefing(context: dict) -> dict | None:
    prompt = (
        "Context:\n" + json.dumps(context, indent=2, default=str)[:8000] + "\n\n"
        "Write the briefing."
    )
    return await _chat(OPUS_ONLINE, BRIEF_SYS, prompt, max_tokens=1200, temperature=0.3,
                        touchpoint="pre_open_briefing", pool=_ctx_pool(), cfg=_ctx_cfg())


async def propose_tuning(week_summary: dict) -> dict | None:
    prompt = (
        f"Weekly bot summary:\n{json.dumps(week_summary, indent=2, default=str)}\n\n"
        "Propose small, evidence-based threshold tweaks."
    )
    return await _chat(OPUS_OFFLINE, TUNE_SYS, prompt, max_tokens=1200, temperature=0.3,
                        touchpoint="propose_tuning", pool=_ctx_pool(), cfg=_ctx_cfg())
