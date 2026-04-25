"""IBKR minimum-price-variation (tick size) compliance.

Error 110 ("price does not conform to the minimum price variation for this
contract") happens on many European venues because our previous rounding was
always to 0.01 regardless of the instrument's actual tick. We query IBKR once
per symbol via reqContractDetailsAsync, cache the minTick in-process, and
round every limit price to it before submission.

Falls back to 0.01 if IB returns no detail (rare; typically for unqualified
contracts). Cache TTL is long (default 24h) — tick sizes rarely change.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

from ib_async import Contract, Crypto, IB, Stock

from .universe import meta

log = logging.getLogger("bot.tick_size")

_CACHE: dict[str, tuple[float, float]] = {}  # symbol -> (expiry_ts, min_tick)
_LOCKS: dict[str, asyncio.Lock] = {}


def _contract(symbol: str) -> Contract:
    m = meta(symbol)
    if m.asset_class == "crypto":
        return Crypto(symbol, "PAXOS", "USD")
    if m.primary_exchange:
        return Stock(symbol, "SMART", m.currency, primaryExchange=m.primary_exchange)
    return Stock(symbol, "SMART", m.currency)


def _tick_floor_for_crypto(price: float) -> float:
    """PAXOS crypto tick bands. BTC trades on $0.50 min-tick, ETH $0.10, LTC/BCH $0.01.
    Use price as proxy: BTC prices are >$1k, ETH $100-10k, LTC/BCH <$1k. Coarser than
    required = fills slightly off-best but no Error 110."""
    if price >= 1000:
        return 0.5
    if price >= 100:
        return 0.1
    return 0.01


def _tick_floor_for(currency: str, price: float) -> float:
    """MiFID II / UK tick-size heuristic floor. IBKR's reqContractDetails.minTick
    returns the *minimum possible* tick for the contract, but real exchange tick
    tables are liquidity- and price-band-dependent — so a €352 stock on Euronext
    often trades on a €0.1 tick, not €0.01, and a 1350p LSE name on 0.5p, not 0.01p.
    Error 110 ('price does not conform') happens when we submit sub-tick prices.
    We err coarse: when IB says 0.01 but exchange tables say 0.1, we round to 0.1.
    """
    cur = currency.upper()
    if cur == "USD":
        return 0.01  # US listed — actually 0.01 up to very low-price names
    if cur == "GBP":
        # LSE pence: Tick Size Regime bands (liquidity tier 5/6, typical).
        if price < 10:    return 0.01   # very low-priced stocks
        if price < 50:    return 0.1
        if price < 100:   return 0.2
        if price < 500:   return 0.5
        if price < 5000:  return 1.0
        return 2.0
    if cur in {"EUR", "CHF", "DKK"}:
        # Euronext / XETRA / SIX / CPH liquidity bands. Coarsened after Error 110
        # on L'Oreal @ €352 with a €0.05 tick — some tier-6 liquidity stocks use
        # €0.1 at this price band. Err coarse = orders fill, slightly off-best.
        if price < 10:    return 0.001
        if price < 50:    return 0.01
        if price < 100:   return 0.05
        if price < 500:   return 0.1
        if price < 5000:  return 0.5
        return 1.0
    return 0.01


async def min_tick(ib: IB, symbol: str, price: float | None = None,
                    ttl_sec: float = 86400.0) -> float:
    """Effective tick for an order. max(contract.minTick, exchange_tick_table(price)).
    Pass `price` for accurate band selection; omit for a conservative default."""
    from .universe import meta as _meta
    m = _meta(symbol)
    if m.asset_class == "crypto":
        heuristic = _tick_floor_for_crypto(price or 0.0)
    else:
        heuristic = _tick_floor_for(m.currency, price or 0.0)

    cached = _CACHE.get(symbol)
    if cached and cached[0] > time.time():
        ib_tick = cached[1]
        return max(ib_tick, heuristic)

    lock = _LOCKS.setdefault(symbol, asyncio.Lock())
    async with lock:
        cached = _CACHE.get(symbol)
        if cached and cached[0] > time.time():
            ib_tick = cached[1]
            return max(ib_tick, heuristic)

        tick = 0.01
        try:
            c = _contract(symbol)
            details = await ib.reqContractDetailsAsync(c)
            if details:
                mt = getattr(details[0], "minTick", None)
                if mt is not None and mt > 0:
                    tick = float(mt)
        except Exception as exc:
            log.warning("min_tick_fetch_failed symbol=%s err=%s", symbol, exc)
        _CACHE[symbol] = (time.time() + ttl_sec, tick)
        return max(tick, heuristic)


def round_to_tick(price: float, tick: float, direction: str = "nearest") -> float:
    """Round price to a multiple of tick. direction ∈ {"nearest","up","down"}.
    Use "up" for buy-side crossing limits, "down" for sell-side, "nearest" otherwise."""
    if tick <= 0:
        return round(price, 2)
    q = price / tick
    if direction == "up":
        qi = math.ceil(q - 1e-9)
    elif direction == "down":
        qi = math.floor(q + 1e-9)
    else:
        qi = round(q)
    # Use enough precision that float drift doesn't re-introduce an off-tick.
    decimals = max(0, -int(math.floor(math.log10(tick))) + 2) if tick < 1 else 2
    return round(qi * tick, decimals)


async def round_limit(ib: IB, symbol: str, price: float, direction: str = "nearest") -> float:
    tick = await min_tick(ib, symbol, price=price)
    return round_to_tick(price, tick, direction)
