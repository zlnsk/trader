"""Pydantic schemas for every LLM touchpoint.

Each free-form Claude response must pass its declared schema before being
consumed by order/risk code. Validation failure → safe default (abstain /
bypass / no-change). This is rec #1 from the research: never let a free-form
LLM prose response drive live orders.

PR5 note: `widen` was removed as a StopAdjust action. Every academic treatment
of optimal mean-reversion exit (Leung & Li 2015, Chiu & Wong 2012) shows
stops should only tighten or trigger, never widen — widening a stop when
price moves against you is a martingale dressed up as intelligence. If the
LLM returns `widen` anyway, StopAdjust coerces to `hold` and logs at WARNING
with the full response so we can audit prompt-drift.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("bot.pydantic_models")


class EntryVeto(BaseModel):
    verdict: Literal["allow", "veto", "abstain"] = "abstain"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    sentiment_score: int | None = Field(default=None, ge=0, le=100)
    dive_cause: str | None = None
    recovery_likelihood: Literal["high", "medium", "low"] | None = None
    red_flags: list[str] = Field(default_factory=list)
    reasoning: str | None = None


class RegimeVerdict(BaseModel):
    regime: Literal["mean_reversion", "momentum", "risk_off", "mixed"] = "mixed"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reasoning: str | None = None


class ExitVeto(BaseModel):
    action: Literal["sell", "hold", "tighten"] = "sell"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    extra_target_pct: float | None = Field(default=None, ge=0.0, le=20.0)
    reasoning: str | None = None


class StopAdjust(BaseModel):
    # Allowed actions — "widen" was removed in PR5 as a safety change.
    action: Literal["hold", "tighten", "exit_now"] = "hold"
    new_stop_pct: float | None = Field(default=None, ge=-30.0, le=0.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reasoning: str | None = None


class Ranking(BaseModel):
    order: list[str] = Field(default_factory=list)
    reasoning: str | None = None

    @field_validator("order")
    @classmethod
    def _uppercase_symbols(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v if isinstance(s, str) and s.strip()]


class NewsWatch(BaseModel):
    action: Literal["hold", "exit_now", "tighten_stop"] = "hold"
    severity: Literal["none", "low", "medium", "high"] = "none"
    headline: str | None = None
    reasoning: str | None = None


def parse_or_default(model: type[BaseModel], raw: dict | None):
    """Validate `raw` against `model`. On failure, return model() — the
    field-level defaults encode the safe fallback for each touchpoint.

    Special case for StopAdjust: if the LLM returns the legacy verdict
    "widen" (now banned), coerce to the safe default "hold" and log loudly
    rather than failing the whole response. Other fields (new_stop_pct,
    reasoning) are preserved so the audit trail stays intact."""
    if not isinstance(raw, dict):
        return model()
    if model is StopAdjust:
        action = raw.get("action")
        if isinstance(action, str) and action.lower() == "widen":
            log.warning(
                "stop_adjust_widen_coerced_to_hold raw=%s — widen is no "
                "longer a permitted action (PR5). Check the prompt template.",
                raw,
            )
            raw = {**raw, "action": "hold", "legacy_widen": True}
    try:
        return model(**raw)
    except Exception:
        return model()
