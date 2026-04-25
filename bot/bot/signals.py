"""Quant signal math — Wilder RSI, SMA, ATR, σ-below-SMA20, trend + volume filters.

The core `score` is a 0-100 composite with a **geometric** blend of RSI and σ so
neither factor alone can dominate — a stock oversold on one axis but flat on the
other will not outrank a stock moderate on both (mean-reversion setups are
stronger when two independent indicators agree).

Optional modifiers:
- `trend_ok(closes)`    — True if last close is within tolerance of SMA(period).
                          Use to avoid catching falling knives below long-term trend.
- `volume_confirm(vols)` — True if last bar volume ≥ multiple × 20-bar mean.
                          Not strictly required, but boosts score when present.

New in rec #1:
- `bullish_rsi_divergence` — price lower low + RSI higher low (high-probability
  mean-reversion signal).
- `multi_timeframe_uptrend` — daily SMA50/200 check for intraday entries.
- `relative_volume_gate` — hard gate (was soft boost only).
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, pstdev


@dataclass
class Bars:
    """Legacy single-series shape. Kept for back-compat callers; new code
    should use broker.HistResult which carries highs/lows/volumes too."""
    closes: list[float]


# ── RSI (Wilder's smoothing) ─────────────────────────────────────────────────

def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI — SMMA of gains/losses. Published RSI thresholds (30/70)
    are calibrated to this, not the simple-mean variant."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    # Seed with SMA of first `period` gains/losses (Wilder convention).
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Iteratively apply Wilder smoothing over the rest.
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def rsi14(closes: list[float]) -> float | None:
    return rsi(closes, 14)


# ── Moving averages / dispersion ──────────────────────────────────────────────

def sma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return mean(closes[-n:])


def sigma_below_sma20(closes: list[float]) -> float | None:
    if len(closes) < 20:
        return None
    recent = closes[-20:]
    m = mean(recent)
    s = pstdev(recent)
    if s == 0:
        return 0.0
    return (m - closes[-1]) / s


def returns_zscore(closes: list[float], period: int = 20) -> float | None:
    """Z-score of the latest single-bar return vs `period` prior returns.
    Positive → unusually large drop vs recent volatility. Complements σ-below-SMA20
    by looking at *return shock* instead of *level drift*."""
    if len(closes) < period + 2:
        return None
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < period + 1:
        return None
    hist = rets[-(period + 1):-1]
    last = rets[-1]
    m = mean(hist)
    s = pstdev(hist)
    if s == 0:
        return 0.0
    # Flip sign so downward shocks are positive (matches σ-below-SMA20 convention).
    return (m - last) / s


# ── ATR (Wilder) ──────────────────────────────────────────────────────────────

def atr(highs: list[float], lows: list[float], closes: list[float],
        period: int = 14) -> float | None:
    """Wilder's Average True Range. Requires ≥ period+1 bars with high/low."""
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, n):
        hi, lo, pc = highs[i], lows[i], closes[i - 1]
        tr = max(hi - lo, abs(hi - pc), abs(lo - pc))
        trs.append(tr)
    atr_val = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
    return atr_val


# ── Trend filter ──────────────────────────────────────────────────────────────

def trend_ok(closes: list[float], period: int = 200, tolerance_pct: float = -5.0) -> bool | None:
    """True if last close ≥ SMA(period) × (1 + tolerance_pct/100).
    Default: accept when price is within 5% below SMA200. Rejects deep
    downtrends where 'dip' is actually a breakdown.
    Returns None if not enough history (caller decides: skip or bypass).

    Kept unchanged for rollback safety; TREND_FILTER_V2_ENABLED switches
    callers to trend_ok_v2 with a tighter default tolerance (-2%)."""
    s = sma(closes, period)
    if s is None:
        return None
    threshold = s * (1 + tolerance_pct / 100.0)
    return closes[-1] >= threshold


def trend_ok_v2(closes: list[float], period: int = 200,
                 tolerance_pct_v2: float = -2.0) -> bool | None:
    """v2 of the trend filter. Canonical Connors/Alvarez rule: buy
    mean-reversion dips only when price sits in a primary uptrend, i.e.
    close to (or above) the long-term SMA. Default tolerance of -2% lets
    the strategy accept small dips while rejecting deeper breakdowns that
    indicate regime change, not mean-reversion.

    Semantics match trend_ok — only the default tolerance is tighter.
    Returns None when history is insufficient."""
    s = sma(closes, period)
    if s is None:
        return None
    threshold = s * (1 + tolerance_pct_v2 / 100.0)
    return closes[-1] >= threshold


def uptrend_50_200_ok(closes: list[float]) -> bool | None:
    """Golden-cross regime check: True when the last close is strictly
    above SMA200 AND SMA50 is strictly above SMA200. Stricter than
    trend_ok_v2 — used by swing slots with the per-slot flag
    require_uptrend_50_200 set. Returns None when either SMA can't be
    computed (need ≥200 closes)."""
    s50 = sma(closes, 50)
    s200 = sma(closes, 200)
    if s50 is None or s200 is None:
        return None
    return closes[-1] > s200 and s50 > s200


def apply_trend_filter(closes, slot_profile, cfg, trend_period, trend_tol):
    """Run the slot's trend filter. Returns None on pass, a short reason
    string on rejection. Branching:

    - TREND_FILTER_V2_ENABLED=false (default): original trend_ok semantics.
      Reject reason is "trend_filter:below_sma{period}" to match pre-v2 log.
    - TREND_FILTER_V2_ENABLED=true: trend_ok_v2 with tolerance
      TREND_TOLERANCE_PCT_V2 (default -2.0). Additionally, if the slot
      profile flag require_uptrend_50_200 is true, check
      uptrend_50_200_ok; failure reason "trend_filter_v2:bearish_50_200"
      outranks the simple below-SMA rejection.

    None-returns from the signals helpers (insufficient history) are
    treated as pass, matching the prior let-through policy. Kept here
    (not in strategy.py) so tests can import without asyncpg.
    """
    v2 = bool(cfg.get("TREND_FILTER_V2_ENABLED"))
    if v2:
        tol_v2 = float(cfg.get("TREND_TOLERANCE_PCT_V2", -2.0))
        if slot_profile.get("require_uptrend_50_200"):
            gc = uptrend_50_200_ok(closes)
            if gc is False:
                return "trend_filter_v2:bearish_50_200"
        ok = trend_ok_v2(closes, period=trend_period, tolerance_pct_v2=tol_v2)
        if ok is False:
            return f"trend_filter_v2:below_sma{trend_period}"
        return None
    ok = trend_ok(closes, period=trend_period, tolerance_pct=trend_tol)
    if ok is False:
        return f"trend_filter:below_sma{trend_period}"
    return None


# ── Volume confirmation ──────────────────────────────────────────────────────

def volume_ratio(volumes: list[float], period: int = 20) -> float | None:
    """Last bar volume / mean of prior `period` bars. > 1 means elevated."""
    if not volumes or len(volumes) < period + 1:
        return None
    recent = volumes[-(period + 1):-1]
    if not recent:
        return None
    m = mean(recent)
    if m <= 0:
        return None
    return volumes[-1] / m


def volume_confirm(volumes: list[float], mult: float = 1.5, period: int = 20) -> bool | None:
    """Rec #1: default mult bumped from 1.2 → 1.5 for Relative Volume filter.
    Returns None if insufficient data, True/False otherwise."""
    vr = volume_ratio(volumes, period=period)
    return None if vr is None else vr >= mult


def relative_volume_gate(volumes: list[float], mult: float = 1.5, period: int = 20) -> str | None:
    """Hard gate version of volume_confirm. Returns None on pass,
    reason string on fail. Used when VOLUME_HARD_GATE_ENABLED is on."""
    vr = volume_ratio(volumes, period=period)
    if vr is None:
        return None  # insufficient data → pass (conservative)
    if vr < mult:
        return f"rv_filter:{vr:.2f}x < {mult}x"
    return None


# ── Internal Bar Strength ────────────────────────────────────────────────────

def ibs(high: float, low: float, close: float) -> float | None:
    """Internal Bar Strength: (close - low) / (high - low). 0 = closed at
    bar low (strong dip → mean-reversion long candidate). 1 = closed at
    bar high. Returns None on degenerate/zero-range bars."""
    rng = high - low
    if rng <= 0:
        return None
    return max(0.0, min(1.0, (close - low) / rng))


def ibs_last(highs: list[float], lows: list[float], closes: list[float]) -> float | None:
    """IBS of the last bar in the series. Safe guard for missing inputs."""
    if not highs or not lows or not closes:
        return None
    if len(highs) == 0 or len(lows) == 0 or len(closes) == 0:
        return None
    return ibs(highs[-1], lows[-1], closes[-1])


def apply_ibs_filter(slot_profile: dict, payload: dict, cfg: dict) -> str | None:
    """Reject when payload['ibs'] > slot_profile['ibs_max'] and both master
    flag + per-slot bound are set. Returns None on pass / not-applicable,
    reason string on reject. Callers record payload['ibs_gate_passed']
    based on the return value.
    """
    if not cfg.get("IBS_FILTER_ENABLED"):
        return None
    ibs_max = slot_profile.get("ibs_max")
    if ibs_max is None:
        return None
    v = payload.get("ibs")
    if v is None:
        # Insufficient data → let through, matching trend-filter policy.
        return None
    if v > float(ibs_max):
        return f"ibs_filter:ibs>{ibs_max}"
    return None


# ── Bullish RSI Divergence (rec #1) ───────────────────────────────────────────

def bullish_rsi_divergence(
    closes: list[float],
    period: int = 14,
    pivot_lookback: int = 5,
) -> tuple[bool, dict]:
    """Detect bullish RSI divergence: price makes a lower low but RSI
    makes a higher low. This is a high-probability mean-reversion signal
    more robust than a simple RSI level.

    Returns (detected, info_dict).
    """
    if len(closes) < period + pivot_lookback * 2 + 1:
        return False, {"reason": "insufficient_data"}

    rsi_vals: list[float] = []
    for i in range(period, len(closes)):
        window = closes[i - period + 1:i + 1]
        r = rsi(window, period)
        rsi_vals.append(r if r is not None else 50.0)

    # Need at least two pivots to compare
    if len(rsi_vals) < pivot_lookback * 2 + 1:
        return False, {"reason": "insufficient_rsi_data"}

    # Find recent price low and prior price low
    recent_prices = closes[-(pivot_lookback + 1):]
    prior_prices = closes[-(pivot_lookback * 2 + 1):-(pivot_lookback + 1)]

    price_low_recent = min(recent_prices)
    price_low_prior = min(prior_prices)
    price_low_recent_idx = len(closes) - (pivot_lookback + 1) + recent_prices.index(price_low_recent)
    price_low_prior_idx = len(closes) - (pivot_lookback * 2 + 1) + prior_prices.index(price_low_prior)

    # Corresponding RSI values (offset by period because rsi_vals starts at index `period`)
    rsi_idx_recent = price_low_recent_idx - period
    rsi_idx_prior = price_low_prior_idx - period
    if rsi_idx_recent < 0 or rsi_idx_prior < 0 or rsi_idx_recent >= len(rsi_vals) or rsi_idx_prior >= len(rsi_vals):
        return False, {"reason": "index_oob"}

    rsi_recent = rsi_vals[rsi_idx_recent]
    rsi_prior = rsi_vals[rsi_idx_prior]

    # Divergence: lower price low, higher RSI low
    price_lower_low = price_low_recent < price_low_prior * 0.999  # tiny tolerance
    rsi_higher_low = rsi_recent > rsi_prior * 1.001

    detected = price_lower_low and rsi_higher_low
    info = {
        "price_low_recent": round(price_low_recent, 4),
        "price_low_prior": round(price_low_prior, 4),
        "rsi_low_recent": round(rsi_recent, 2),
        "rsi_low_prior": round(rsi_prior, 2),
        "price_lower_low": price_lower_low,
        "rsi_higher_low": rsi_higher_low,
    }
    return detected, info


# ── Multi-timeframe confirmation (rec #1) ─────────────────────────────────────

def multi_timeframe_uptrend(closes_daily: list[float]) -> bool | None:
    """For intraday entries: require the daily trend (SMA50/200) to be
    positive or stabilising. Buying dips in a primary 1-hour (or daily)
    downtrend is significantly riskier.

    Returns True if SMA50 > SMA200 and last close > SMA200 (golden-cross
    regime). Returns None if insufficient daily history."""
    s50 = sma(closes_daily, 50)
    s200 = sma(closes_daily, 200)
    if s50 is None or s200 is None:
        return None
    return closes_daily[-1] > s200 and s50 > s200


def apply_multitimeframe_gate(
    strategy: str,
    closes_daily: list[float] | None,
    cfg: dict,
) -> str | None:
    """Hard gate for multi-timeframe confirmation. Returns None on pass,
    reason string on reject.

    Only applies to intraday strategy when MULTI_TF_CONFIRM_ENABLED is on.
    Swing strategy uses its own trend filter on the same timeframe bars."""
    if strategy != "intraday":
        return None
    if not cfg.get("MULTI_TF_CONFIRM_ENABLED"):
        return None
    if closes_daily is None or len(closes_daily) < 200:
        return None  # insufficient data → pass (don't block on missing data)
    ok = multi_timeframe_uptrend(closes_daily)
    if ok is False:
        return "multi_tf:daily_uptrend_required"
    return None


# ── Score (geometric blend, 0-100) ────────────────────────────────────────────

def score(
    closes: list[float],
    rsi_period: int = 14,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
    volume_mult: float = 1.5,
    closes_daily: list[float] | None = None,
    strategy: str = "swing",
    cfg: dict | None = None,
) -> tuple[float | None, dict]:
    """Composite 0-100 score. Geometric blend of RSI and σ-below-SMA20 so both
    must be non-trivial for a high score. Optional volume boost (up to +10%)
    and returns-z-score modulation (up to +10%).

    Rec #1 enhancements:
    - Bullish RSI divergence: +15% boost when detected (cap at 100).
    - Relative Volume hard gate: when VOLUME_HARD_GATE_ENABLED, reject
      (return None) if volume < mult × 20-bar mean.
    - Multi-timeframe confirmation: intraday entries require daily uptrend.
    """
    cfg = cfg or {}
    r = rsi(closes, rsi_period)
    sig = sigma_below_sma20(closes)
    sma20 = sma(closes, 20)
    if r is None or sig is None or sma20 is None:
        return None, {"rsi": r, "sigma_below_sma20": sig, "sma20": sma20}

    # Hard volume gate (rec #1)
    if cfg.get("VOLUME_HARD_GATE_ENABLED") and volumes:
        rv_reason = relative_volume_gate(volumes, mult=volume_mult)
        if rv_reason:
            return None, {"rsi": r, "sigma_below_sma20": sig, "sma20": sma20,
                          "rv_gate": rv_reason}

    # Multi-timeframe gate (rec #1) — intraday only
    mt_reason = apply_multitimeframe_gate(strategy, closes_daily, cfg)
    if mt_reason:
        return None, {"rsi": r, "sigma_below_sma20": sig, "sma20": sma20,
                      "multi_tf_reason": mt_reason}

    # Normalize each to 0-1. RSI component: 0 at RSI=50, 1 at RSI=0.
    rsi_norm = max(0.0, min(1.0, (50 - r) / 50.0))
    # σ component: 0 at σ=0, 1 at σ=2.5 (stretched dip).
    sig_norm = max(0.0, min(1.0, sig / 2.5))

    # Geometric mean — needs BOTH non-zero to produce meaningful score.
    base = 100.0 * (rsi_norm * sig_norm) ** 0.5

    # Volume confirmation: up to +10% boost if elevated; no penalty when absent.
    vol_ratio = volume_ratio(volumes) if volumes else None
    vol_boost = 0.0
    if vol_ratio is not None and vol_ratio >= volume_mult:
        vol_boost = min(0.10, (vol_ratio - volume_mult) * 0.05 + 0.05)

    # Return-shock modulation: z-score > 1.5 means today's drop is outsized
    # vs typical. Up to +10% boost.
    rz = returns_zscore(closes)
    rz_boost = 0.0
    if rz is not None and rz > 1.5:
        rz_boost = min(0.10, (rz - 1.5) * 0.05 + 0.025)

    # Bullish RSI divergence boost (rec #1)
    div_boost = 0.0
    div_info: dict = {"detected": False}
    if cfg.get("RSI_DIVERGENCE_ENABLED"):
        div_detected, div_info = bullish_rsi_divergence(closes, period=rsi_period)
        if div_detected:
            div_boost = 0.15

    total = min(100.0, base * (1.0 + vol_boost + rz_boost + div_boost))

    payload = {
        "rsi": round(r, 2),
        "rsi_period": rsi_period,
        "sigma_below_sma20": round(sig, 3),
        "sma20": round(sma20, 4),
        "last": round(closes[-1], 4),
        "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        "returns_z": round(rz, 2) if rz is not None else None,
        "base_score": round(base, 2),
        "vol_boost": round(vol_boost, 3),
        "rz_boost": round(rz_boost, 3),
        "div_boost": round(div_boost, 3),
        "rsi_divergence": div_info,
    }
    if highs and lows and len(highs) >= 15 and len(lows) >= 15:
        a = atr(highs, lows, closes, period=14)
        if a is not None:
            payload["atr14"] = round(a, 4)
            payload["atr_pct"] = round(a / closes[-1] * 100, 3) if closes[-1] else None
        # IBS of the signal bar is computed alongside ATR: same inputs, same
        # "needs OHLC not just closes" constraint. Cheap to compute and useful
        # for filter-impact analysis even when IBS_FILTER_ENABLED is off.
        ibs_val = ibs_last(highs, lows, closes)
        if ibs_val is not None:
            payload["ibs"] = round(ibs_val, 4)
    return total, payload
