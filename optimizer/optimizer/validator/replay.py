"""Replay engine for candidate-vs-baseline comparisons.

Given a proposal (key→new_value per slot) and a set of historical signal
snapshots with hypothetical_outcome_pct, produce per-trade simulated
outcomes under both baseline and candidate rules. The candidate wins a
signal only if it passes the new threshold; each snapshot's
hypothetical_outcome_pct is the realised outcome IF that entry had been
taken.

This is NOT a full backtest (no order-fill modelling, no slippage
changes). It's an approximation sufficient to reject proposals that
don't improve the signal-gating decision. The canary stage re-validates
against real fills.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReplayedTrade:
    snapshot_id: int
    snapshot_ts: object
    symbol: str
    slot_id: int | None
    baseline_accept: bool
    candidate_accept: bool
    outcome_pct: float   # hypothetical_outcome_pct
    entry_regime: str | None


def _accept(row: dict, cfg: dict) -> bool:
    """Minimal gate: accept the snapshot if its features pass every
    threshold currently in cfg. Only the keys that actually exist in the
    snapshot are considered; unknown keys are permissive (they pass).
    """
    score_min = cfg.get("QUANT_SCORE_MIN")
    if score_min is not None and row.get("score") is not None:
        if float(row["score"]) < float(score_min):
            return False
    rsi_max = cfg.get("RSI_BUY_THRESHOLD")
    if rsi_max is not None and row.get("rsi") is not None:
        if float(row["rsi"]) > float(rsi_max):
            return False
    sigma_min = cfg.get("SIGMA_BELOW_SMA20")
    if sigma_min is not None and row.get("sigma_below_sma20") is not None:
        if float(row["sigma_below_sma20"]) < float(sigma_min):
            return False
    return True


def replay(
    snapshots: list[dict],
    *,
    baseline: dict,
    candidate: dict,
) -> list[ReplayedTrade]:
    """Return the per-snapshot accept/outcome pair for both configs."""
    out = []
    for s in snapshots:
        outcome = s.get("hypothetical_outcome_pct")
        if outcome is None:
            continue
        out.append(ReplayedTrade(
            snapshot_id=int(s["id"]),
            snapshot_ts=s["snapshot_ts"],
            symbol=s["symbol"],
            slot_id=s.get("slot_id"),
            baseline_accept=_accept(s, baseline),
            candidate_accept=_accept(s, candidate),
            outcome_pct=float(outcome),
            entry_regime=s.get("stock_regime") or s.get("crypto_regime"),
        ))
    return out


def summarise(trades: list[ReplayedTrade], accept_attr: str) -> dict:
    """Aggregate outcomes for whichever rule set accepted the trade."""
    accepted = [t for t in trades if getattr(t, accept_attr)]
    if not accepted:
        return {"n": 0, "mean_pct": 0.0, "win_rate": 0.0,
                 "total_pct": 0.0, "pf": 0.0}
    wins = [t.outcome_pct for t in accepted if t.outcome_pct > 0]
    losses = [t.outcome_pct for t in accepted if t.outcome_pct <= 0]
    sum_wins = sum(wins)
    sum_losses = sum(losses)
    pf = (sum_wins / abs(sum_losses)) if sum_losses else float("inf")
    mean_pct = sum(t.outcome_pct for t in accepted) / len(accepted)
    return {
        "n": len(accepted),
        "mean_pct": mean_pct,
        "win_rate": len(wins) / len(accepted),
        "total_pct": sum(t.outcome_pct for t in accepted),
        "pf": pf if pf != float("inf") else None,
    }
