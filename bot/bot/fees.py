"""IBKR fee estimator.

US (USD): Tiered — $0.0035/share commission (min $0.35, cap 1% of trade value) +
$0.00013/share regulatory.

Non-US: flat percent-of-trade approximation by currency (Tiered retail approx):
- EUR (XETRA/Euronext/AEB/SIX-EUR): 0.10% of trade value, min €3 one-side
- GBP (LSE):                        0.05% of trade value, min £1 one-side
- CHF (SIX Swiss):                  0.10% of trade value, min CHF 1.50 one-side
- DKK (Copenhagen):                 0.10% of trade value, min DKK 15 one-side

Crypto (PAXOS, USD): 0.18% of trade value with $1.75 floor per side, no
regulatory component. Round-trip ≈ 0.36% — slots must gate on a net-margin
that exceeds this or entries never clear.

Anything else → treat like EUR conservatively.
"""
from __future__ import annotations

from dataclasses import dataclass

USD_PER_COMMISSION_SHARE = 0.0035
USD_MIN = 0.35
USD_MAX_PCT = 0.01
USD_REG_PER_SHARE = 0.00013

CRYPTO_PCT = 0.0018
CRYPTO_MIN = 1.75

NON_US = {
    "EUR": (0.0010, 3.0),
    "GBP": (0.0005, 1.0),
    "CHF": (0.0010, 1.5),
    "DKK": (0.0010, 15.0),
}

# Per-asset-class slippage (basis points, one-side). R:R math must account for
# slippage on both entry and exit, so effective cost = 2 × these values.
# Values picked to reflect observed fills, not theoretical bid/ask midpoints:
# - US large-caps on SMART route during RTH: 1-3 bps typical, 3 is a
#   conservative planning number.
# - EU venues: wider synthetic spreads on IBKR paper, 8 bps reflects real
#   Euronext/XETRA slippage on tier-5/6 liquidity names.
# - Crypto shadow-sim: matches CRYPTO_PAPER_SIM_SLIPPAGE_BPS (3 bps) since
#   that's what the sim fill adds — no point double-counting a different
#   slippage in the R:R math than what the sim actually applies.
# - Crypto live: PAXOS real spreads run 10-20 bps depending on hour; 15 is
#   a realistic planning number once the bot goes live on a funded account.
SLIPPAGE_BPS_US_LARGECAP = 3
SLIPPAGE_BPS_EU = 8
SLIPPAGE_BPS_CRYPTO_SIM = 3
SLIPPAGE_BPS_CRYPTO_LIVE = 15


def slippage_bps_for(asset_class: str, currency: str = "USD",
                      crypto_paper_sim: bool = True) -> float:
    """One-side slippage in basis points for (asset_class, currency). The
    `crypto_paper_sim` flag selects between sim (3 bps) and live (15 bps)
    crypto slippage; defaults to True since the bot currently runs shadow
    crypto. Stock EU vs US currency-dispatched."""
    if asset_class == "crypto":
        return float(SLIPPAGE_BPS_CRYPTO_SIM if crypto_paper_sim else SLIPPAGE_BPS_CRYPTO_LIVE)
    if currency == "USD":
        return float(SLIPPAGE_BPS_US_LARGECAP)
    return float(SLIPPAGE_BPS_EU)


@dataclass
class Fee:
    side: str
    qty: float
    price: float
    currency: str
    commission: float
    regulatory: float

    @property
    def total(self) -> float:
        return self.commission + self.regulatory


def _us_fee(side: str, qty: float, price: float) -> Fee:
    trade_value = qty * price
    raw = qty * USD_PER_COMMISSION_SHARE
    capped = min(max(raw, USD_MIN), USD_MAX_PCT * trade_value if trade_value > 0 else raw)
    reg = qty * USD_REG_PER_SHARE
    return Fee(side, qty, price, "USD", capped, reg)


def _intl_fee(side: str, qty: float, price: float, currency: str) -> Fee:
    pct, minf = NON_US.get(currency, (0.0010, 3.0))
    trade_value = qty * price
    raw = trade_value * pct
    comm = max(raw, minf)
    return Fee(side, qty, price, currency, comm, 0.0)


def _crypto_fee(side: str, qty: float, price: float) -> Fee:
    trade_value = qty * price
    comm = max(trade_value * CRYPTO_PCT, CRYPTO_MIN)
    return Fee(side, qty, price, "CRYPTO", comm, 0.0)


def estimate_side(side: str, qty: float, price: float, currency: str = "USD",
                   asset_class: str = "stock") -> Fee:
    if asset_class == "crypto":
        return _crypto_fee(side, qty, price)
    if currency == "USD":
        return _us_fee(side, qty, price)
    return _intl_fee(side, qty, price, currency)


def round_trip(qty: float, buy_price: float, sell_price: float, currency: str = "USD",
                asset_class: str = "stock") -> float:
    buy = estimate_side("BUY", qty, buy_price, currency, asset_class)
    sell = estimate_side("SELL", qty, sell_price, currency, asset_class)
    return buy.total + sell.total


def net_expected(
    qty: float, buy_price: float, target_price: float, currency: str = "USD",
    asset_class: str = "stock",
) -> float:
    gross = (target_price - buy_price) * qty
    return gross - round_trip(qty, buy_price, target_price, currency, asset_class)


def _reference_qty(slot_size_eur: float, price: float, asset_class: str) -> float:
    """Reference share/coin count used for R:R math. Mirrors sizing.compute_qty
    semantics: crypto fractional, stocks integer-floor. Never returns 0 — a
    zero-qty path would hide fee-floor effects that matter at tiny notionals."""
    if price <= 0:
        return 0.0
    if asset_class == "crypto":
        return max(slot_size_eur / price, 1e-9)
    return max(1.0, float(int(slot_size_eur // price)))


def net_expected_rr(
    slot_profile: dict,
    price: float,
    asset_class: str = "stock",
    currency: str = "USD",
    slot_size_eur: float = 1000.0,
    crypto_paper_sim: bool = True,
) -> float:
    """Fee- and slippage-adjusted reward-to-risk ratio for a slot profile.

    Approach: take the slot's gross target_profit_pct and stop_loss_pct and
    subtract (resp. add) real trade frictions at a reference notional derived
    from SLOT_SIZE_EUR. Frictions are round-trip commission/reg fees via
    fees.round_trip plus 2 × one-side slippage (both entry and exit).

    Returns the ratio net_target_pct / net_stop_pct. A value ≥1 means the
    slot has positive R:R after frictions; values below 0 mean fees/slippage
    swallow the entire target. The startup validator (config.validate_slot_rr)
    uses a 0.6 floor per PR1 spec: below that, required win rate for
    break-even is implausibly high for any realistic mean-reversion tape.
    """
    target_pct = float(slot_profile["target_profit_pct"]) / 100.0
    stop_pct_raw = float(slot_profile["stop_loss_pct"])
    stop_pct = abs(stop_pct_raw) / 100.0
    if target_pct <= 0 or stop_pct <= 0:
        return 0.0

    qty = _reference_qty(slot_size_eur, price, asset_class)
    if qty <= 0:
        return 0.0
    notional = qty * price

    target_price = price * (1 + target_pct)
    stop_price = price * (1 - stop_pct)

    fee_at_target = round_trip(qty, price, target_price, currency, asset_class)
    fee_at_stop = round_trip(qty, price, stop_price, currency, asset_class)

    slip_pct = slippage_bps_for(asset_class, currency, crypto_paper_sim) / 10_000.0
    slip_total_pct = 2.0 * slip_pct  # entry + exit

    fee_pct_target = fee_at_target / notional if notional else 0.0
    fee_pct_stop = fee_at_stop / notional if notional else 0.0

    net_target_pct = target_pct - fee_pct_target - slip_total_pct
    net_stop_pct = stop_pct + fee_pct_stop + slip_total_pct

    if net_stop_pct <= 0:
        return 0.0
    return net_target_pct / net_stop_pct


# Backwards-compatible aliases used elsewhere in the bot
round_trip_usd = round_trip
net_expected_usd = net_expected
