"""Deterministic regime detection via SPY 20d realized-vol percentile + absolute floor.

Rec #3 from the research pass: replace "ask Claude for regime" with a
reproducible statistic. Prior version used a z-score, which was highly
sensitive to whether the trailing distribution included crisis-era tails —
produced false risk_off in a perfectly normal 18.9% annualised vol regime
(2026-04-20 incident).

Current version:
- Fetch ~500 calendar days of SPY daily bars (guarantees ~252 business-day
  trailing distribution even after IBKR RTH stripping).
- Today's 20d RV → trailing **percentile** instead of z-score. Invariant to
  tail shape: a flat 88th-percentile vol is clearly non-crisis.
- Two-gate classification:
    risk_off      ← percentile ≥ VOL_PERCENTILE_RISKOFF (95) AND
                     absolute RV ≥ VOL_RV_RISKOFF_MIN (0.25)
    momentum      ← percentile ≤ VOL_PERCENTILE_MOMENTUM (10)
    mean_reversion ← otherwise
  The absolute floor prevents risk_off on "technically elevated" vol that is
  still quiet in any common-sense reading. A 25% annualised 20d RV is
  historically mid-high — classic pull-back territory, not a crisis.

Returns dict compatible with strategy.current_regime consumers; z-score is
kept in the payload for observability but is no longer the gate.
"""
from __future__ import annotations

import logging
import math
from statistics import mean, pstdev

from ib_async import IB

from . import broker

log = logging.getLogger("bot.regime_det")


def _realized_vol(closes: list[float], window: int = 20) -> float | None:
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < window:
        return None
    win = rets[-window:]
    return pstdev(win) * math.sqrt(252)


async def compute(
    ib: IB,
    lookback_days: int = 500,
    percentile_riskoff: float = 95.0,
    percentile_momentum: float = 10.0,
    rv_floor_riskoff: float = 0.25,
) -> dict | None:
    """Pull SPY daily bars, compute 20d realized vol, classify vs its trailing
    distribution. Returns {regime, confidence, realized_vol_*, reasoning}.
    None if data unavailable."""
    hist = await broker.get_daily_closes(ib, "SPY", lookback_days=lookback_days)
    if hist is None or len(hist.closes) < 60:
        return None
    closes = hist.closes

    # Full trailing series of 20d realized vol.
    window = 20
    vols: list[float] = []
    for end in range(window + 1, len(closes) + 1):
        v = _realized_vol(closes[max(0, end - 50):end], window=window)
        if v is not None:
            vols.append(v)
    if len(vols) < 40:
        return None

    today_vol = vols[-1]
    # Use everything prior to today as the trailing distribution.
    trailing = vols[:-1]
    n_leq = sum(1 for v in trailing if v <= today_vol)
    percentile = n_leq / len(trailing) * 100.0

    # Keep z-score in the payload for comparison/debug — not load-bearing.
    m = mean(trailing)
    s = pstdev(trailing)
    z = (today_vol - m) / s if s > 0 else 0.0

    # Two-gate classification (percentile + absolute floor).
    if percentile >= percentile_riskoff and today_vol >= rv_floor_riskoff:
        regime = "risk_off"
        # Confidence scales with how far past the threshold we are, capped.
        head = min(100.0, percentile) - percentile_riskoff
        confidence = round(min(1.0, 0.5 + head / 10.0), 2)
    elif percentile <= percentile_momentum:
        regime = "momentum"
        confidence = round(min(1.0, 0.5 + (percentile_momentum - percentile) / 10.0), 2)
    else:
        regime = "mean_reversion"
        # Confidence highest near the middle of the distribution.
        mid = 50.0
        confidence = round(1.0 - abs(percentile - mid) / 50.0, 2)

    reasoning = (
        f"SPY 20d RV = {today_vol:.3f} ({today_vol*100:.1f}% annualised), "
        f"percentile {percentile:.1f}% over {len(trailing)} trailing points "
        f"(z={z:+.2f}); risk_off requires p≥{percentile_riskoff:.0f} "
        f"AND RV≥{rv_floor_riskoff:.2f}"
    )
    return {
        "regime": regime,
        "confidence": confidence,
        "realized_vol_annualized": round(today_vol, 4),
        "realized_vol_percentile": round(percentile, 2),
        "realized_vol_z": round(z, 3),
        "trailing_points": len(trailing),
        "reasoning": reasoning,
    }


async def compute_crypto(
    ib: IB,
    lookback_days: int = 400,
    percentile_riskoff: float = 95.0,
    percentile_momentum: float = 10.0,
    rv_floor_riskoff: float = 1.0,
    reference_symbol: str = "BTC",
) -> dict | None:
    """Crypto regime via BTC daily realized-vol percentile. Identical shape to
    compute() but with crypto-calibrated thresholds:
      - rv_floor_riskoff 1.0 (100% annualised) — crypto sits around 50-70%
        RV in quiet regimes and spikes past 100% only in genuine stress
        (LUNA-2022, FTX-2022, March-2020). Stock floor 0.25 would false-fire
        on normal crypto vol.
      - Same percentile gates (p95 / p10) — distribution-shape invariant.
    Uses AGGTRADES (crypto historical-data requirement); useRTH=False is
    forced upstream in broker._fetch_historical for crypto."""
    hist = await broker.get_daily_closes(ib, reference_symbol, lookback_days=lookback_days)
    if hist is None or len(hist.closes) < 60:
        return None
    closes = hist.closes

    window = 20
    vols: list[float] = []
    for end in range(window + 1, len(closes) + 1):
        v = _realized_vol(closes[max(0, end - 50):end], window=window)
        if v is not None:
            vols.append(v)
    if len(vols) < 40:
        return None

    today_vol = vols[-1]
    trailing = vols[:-1]
    n_leq = sum(1 for v in trailing if v <= today_vol)
    percentile = n_leq / len(trailing) * 100.0
    m = mean(trailing)
    s = pstdev(trailing)
    z = (today_vol - m) / s if s > 0 else 0.0

    if percentile >= percentile_riskoff and today_vol >= rv_floor_riskoff:
        regime = "risk_off"
        head = min(100.0, percentile) - percentile_riskoff
        confidence = round(min(1.0, 0.5 + head / 10.0), 2)
    elif percentile <= percentile_momentum:
        regime = "momentum"
        confidence = round(min(1.0, 0.5 + (percentile_momentum - percentile) / 10.0), 2)
    else:
        regime = "mean_reversion"
        mid = 50.0
        confidence = round(1.0 - abs(percentile - mid) / 50.0, 2)

    reasoning = (
        f"{reference_symbol} 20d RV = {today_vol:.3f} ({today_vol*100:.1f}% annualised), "
        f"percentile {percentile:.1f}% over {len(trailing)} trailing points "
        f"(z={z:+.2f}); risk_off requires p≥{percentile_riskoff:.0f} "
        f"AND RV≥{rv_floor_riskoff:.2f}"
    )
    return {
        "regime": regime,
        "confidence": confidence,
        "realized_vol_annualized": round(today_vol, 4),
        "realized_vol_percentile": round(percentile, 2),
        "realized_vol_z": round(z, 3),
        "trailing_points": len(trailing),
        "reference_symbol": reference_symbol,
        "reasoning": reasoning,
    }
