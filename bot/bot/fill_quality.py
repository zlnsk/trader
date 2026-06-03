"""Fill-quality instrumentation.

Captures bid/ask at submit time; computes slippage vs mid-at-submit on fill;
and in paper mode produces a shadow fill price with a half-spread adverse
penalty (crude correction for paper's perfect-limit-fill optimism).

Reddit research 2026-04-21 (r/algotrading 1rvk302, r/quant 1r1bmif):
 - Slippage on real executions is typically 2-3x commissions.
 - Paper fills exactly at limit with no simulated slippage -> paper P&L is
   optimistic. Baking a synthetic half-spread penalty surfaces the gap.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass
class Quote:
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    spread_bps: float | None = None


async def capture_quote(ib: Any, contract: Any, timeout: float = 2.0) -> Quote:
    """Request a snapshot; wait up to `timeout` for bid+ask; compute mid+spread.
    Returns an empty Quote on failure — callers treat missing values as skip."""
    if ib is None or contract is None:
        return Quote()
    try:
        t = ib.reqMktData(contract, "", snapshot=False, regulatorySnapshot=False)
    except Exception:
        return Quote()
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            bid = getattr(t, "bid", None)
            ask = getattr(t, "ask", None)
            if bid and ask and bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                spread_bps = ((ask - bid) / mid) * 10000 if mid > 0 else None
                try:
                    ib.cancelMktData(contract)
                except Exception:
                    pass
                return Quote(bid=float(bid), ask=float(ask), mid=float(mid),
                             spread_bps=float(spread_bps) if spread_bps else None)
            await asyncio.sleep(0.1)
    finally:
        try:
            ib.cancelMktData(contract)
        except Exception:
            pass
    return Quote()


def compute_slippage_bps(side: str, fill_price: float, mid_at_submit: float | None) -> float | None:
    """+ve slippage_bps == bad (paid more on BUY / got less on SELL)."""
    if not mid_at_submit or mid_at_submit <= 0 or not fill_price:
        return None
    if side.upper() == "BUY":
        return ((fill_price - mid_at_submit) / mid_at_submit) * 10000
    return ((mid_at_submit - fill_price) / mid_at_submit) * 10000


def shadow_fill_price(side: str, fill_price: float, spread_bps: float | None, paper: bool) -> float | None:
    """Paper-realism correction: penalise fill by half the spread-at-submit.
    Returns None when inputs missing or not applicable (non-paper mode returns
    fill unchanged)."""
    if not fill_price or fill_price <= 0:
        return None
    if not paper:
        return float(fill_price)
    if spread_bps is None or spread_bps <= 0:
        return float(fill_price)
    penalty_frac = (spread_bps * 0.5) / 10000
    if side.upper() == "BUY":
        return float(fill_price) * (1 + penalty_frac)
    return float(fill_price) * (1 - penalty_frac)
