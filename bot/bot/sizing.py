"""Position sizing — fixed-slot and volatility-target modes.

Fixed: legacy behaviour, qty = floor(SLOT_SIZE_EUR / price).

Vol target: qty such that (stop_distance_per_share * qty) ≈ risk_budget.
Rec #4 — fixed-euro slots ignore that a high-ATR symbol at €1000 risks 3× a
low-ATR symbol at the same euro slot. Vol target equalises per-trade risk.

Clamped: the resulting notional must stay within [0.3×, 2×] the fixed slot to
prevent runaway sizing on extreme low-volatility names and starvation on
extreme high-volatility names.

asset_class="crypto": qty is fractional (IBKR PAXOS supports fractional units
down to 0.0001 BTC / 0.001 ETH). floor() is skipped; qty rounded to 6 decimals.
"""
from __future__ import annotations

from math import floor


def _round_qty(qty: float, asset_class: str) -> float:
    if asset_class == "crypto":
        return round(max(qty, 0.0), 6)
    return float(floor(qty))


def fixed_qty(slot_size_eur: float, price: float, asset_class: str = "stock") -> float:
    if price <= 0:
        return 0.0
    return _round_qty(slot_size_eur / price, asset_class)


def vol_target_qty(
    equity_eur: float | None,
    slot_size_eur: float,
    price: float,
    stop_price: float,
    risk_pct: float = 0.5,
    min_notional_mult: float = 0.3,
    max_notional_mult: float = 2.0,
    asset_class: str = "stock",
) -> float:
    """qty such that (price - stop_price) * qty ≈ equity * risk_pct/100.
    Falls back to fixed sizing when equity not available or stop invalid.
    Clamped between [min×, max×] the fixed slot notional."""
    if price <= 0:
        return 0.0
    if equity_eur is None or equity_eur <= 0 or stop_price is None:
        return fixed_qty(slot_size_eur, price, asset_class)
    stop_dist = max(price - stop_price, 0.0)
    if stop_dist <= 0:
        return fixed_qty(slot_size_eur, price, asset_class)
    risk_budget = equity_eur * max(0.0, risk_pct) / 100.0
    raw_qty = risk_budget / stop_dist
    raw_notional = raw_qty * price
    lo = slot_size_eur * min_notional_mult
    hi = slot_size_eur * max_notional_mult
    notional = max(lo, min(hi, raw_notional))
    return _round_qty(notional / price, asset_class)


def compute_qty(
    mode: str,
    slot_size_eur: float,
    price: float,
    stop_price: float | None = None,
    equity_eur: float | None = None,
    risk_pct: float = 0.5,
    asset_class: str = "stock",
) -> tuple[float, str]:
    """Returns (qty, source_label). source_label goes into the signal payload
    so you can audit how each entry was sized."""
    mode = (mode or "fixed").lower()
    if mode == "vol_target" and stop_price is not None:
        q = vol_target_qty(equity_eur, slot_size_eur, price, stop_price, risk_pct,
                            asset_class=asset_class)
        return q, "vol_target"
    return fixed_qty(slot_size_eur, price, asset_class), "fixed"
