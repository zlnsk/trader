"""signal_snapshots instrumentation (PR12).

Writes one row per (candidate × scan) at the decision point where the
candidate's gate outcome becomes known. Consumer: future self-optimization
layer (PR17 Bayesian, PR16 failure clustering) — both need the population
of *passing-pre-filter* candidates, not just the ones that traded.

build_snapshot_row builds the row dict from the candidate's current state;
the caller inserts. insert_snapshot is a thin wrapper.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def build_snapshot_row(
    *,
    symbol: str,
    strategy: str,
    slot_id: int | None,
    payload: dict[str, Any],
    gate_outcome: str,
    llm_verdict: str | None = None,
    llm_dive_cause: str | None = None,
    trade_id: int | None = None,
    stock_regime: str | None = None,
    crypto_regime: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Materialise a signal_snapshots row from the scan's in-flight state.
    Fields we don't have yet (hurst_exponent, vix_percentile, vwap) are
    left NULL — their columns exist in schema so future indicator work
    can start populating them without another migration."""
    ts = now or datetime.now(timezone.utc)
    return {
        "symbol": symbol,
        "strategy": strategy,
        "slot_id": slot_id,
        "snapshot_ts": ts,
        "score": payload.get("base_score") if payload.get("score") is None else payload.get("score"),
        "rsi": payload.get("rsi"),
        "sigma_below_sma20": payload.get("sigma_below_sma20"),
        "ibs": payload.get("ibs"),
        "atr14": payload.get("atr14"),
        "vwap_distance_pct": payload.get("vwap_distance_pct"),
        "volume_ratio": payload.get("vol_ratio"),
        "sma200_distance_pct": payload.get("sma200_distance_pct"),
        "stock_regime": stock_regime,
        "crypto_regime": crypto_regime,
        "vix_percentile": None,
        "hurst_exponent": None,
        "day_of_week": ts.weekday(),
        "minute_of_day": ts.hour * 60 + ts.minute,
        "gate_outcome": gate_outcome,
        "llm_verdict": llm_verdict,
        "llm_dive_cause": llm_dive_cause,
        "trade_id": trade_id,
        "config_version_id": None,
        "hypothetical_outcome_pct": None,
    }


async def insert_snapshot(pool, row: dict[str, Any]) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO signal_snapshots
               (symbol, strategy, slot_id, snapshot_ts, score, rsi,
                sigma_below_sma20, ibs, atr14, vwap_distance_pct,
                volume_ratio, sma200_distance_pct, stock_regime,
                crypto_regime, vix_percentile, hurst_exponent,
                day_of_week, minute_of_day, gate_outcome, llm_verdict,
                llm_dive_cause, trade_id, config_version_id,
                hypothetical_outcome_pct)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,
                       $15,$16,$17,$18,$19,$20,$21,$22,$23,$24)""",
            row["symbol"], row["strategy"], row["slot_id"], row["snapshot_ts"],
            row["score"], row["rsi"], row["sigma_below_sma20"], row["ibs"],
            row["atr14"], row["vwap_distance_pct"], row["volume_ratio"],
            row["sma200_distance_pct"], row["stock_regime"],
            row["crypto_regime"], row["vix_percentile"], row["hurst_exponent"],
            row["day_of_week"], row["minute_of_day"], row["gate_outcome"],
            row["llm_verdict"], row["llm_dive_cause"], row["trade_id"],
            row["config_version_id"], row["hypothetical_outcome_pct"],
        )
