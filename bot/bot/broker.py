"""IB Gateway operations via ib_async — contracts, historical bars, orders.

Historical fetches are cached in-process with a TTL so repeated scans within
the TTL window don't re-hit IBKR's HMDS (which has strict rate limits and was
the suspected cause of Error 162 HMDS no-data responses).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ib_async import Contract, Crypto, IB, LimitOrder, MarketOrder, Order, Stock, StopOrder, Trade

from . import tick_size
from . import fill_quality
from .universe import meta

log = logging.getLogger("bot.broker")

# Symbols that have failed contract qualification this session — we stop
# re-trying to cut log noise and wasted IBKR requests. Reset on bot restart.
_UNRESOLVABLE: set[str] = set()


def is_unresolvable(symbol: str) -> bool:
    return symbol in _UNRESOLVABLE


def mark_unresolvable(symbol: str) -> None:
    if symbol not in _UNRESOLVABLE:
        log.warning("contract_blacklisted_for_session symbol=%s", symbol)
        _UNRESOLVABLE.add(symbol)


# Symbols whose historical-bar fetch keeps returning empty (typically IBKR
# Error 162 HMDS "No market data permissions", e.g. PAXOS crypto on
# paper accounts). After N consecutive empties we stop asking IBKR to cut
# log noise. Reset on bot restart. Separate from _UNRESOLVABLE which tracks
# contract-qualify failures.
_HIST_EMPTY_COUNT: dict[str, int] = {}
_HIST_BLACKLIST: set[str] = set()
_HIST_EMPTY_THRESHOLD = 3


def is_hist_blacklisted(symbol: str) -> bool:
    return symbol in _HIST_BLACKLIST


def _note_hist_empty(symbol: str) -> None:
    n = _HIST_EMPTY_COUNT.get(symbol, 0) + 1
    _HIST_EMPTY_COUNT[symbol] = n
    if n >= _HIST_EMPTY_THRESHOLD and symbol not in _HIST_BLACKLIST:
        log.warning("hist_blacklisted_for_session symbol=%s empties=%s", symbol, n)
        _HIST_BLACKLIST.add(symbol)


def _note_hist_ok(symbol: str) -> None:
    if symbol in _HIST_EMPTY_COUNT:
        _HIST_EMPTY_COUNT.pop(symbol, None)


@dataclass
class HistResult:
    """Historical bar slice. Closes always present; highs/lows/volumes may be
    empty for legacy callers but are populated by the default fetchers."""
    closes: list[float]
    last_close: float
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)


def _contract(symbol: str) -> Contract:
    """Build an IBKR contract. Stocks use SMART routing across the board —
    paper accounts get market-data permissions along the SMART route, so
    directly targeting the primary exchange (e.g. IBIS) strips permission
    and triggers Error 162/354. Symbols whose SMART route can't qualify at
    all (NOVO-B CPH, ROG EBS, SAN SBF) are dropped in universe.py instead.

    Crypto (BTC/ETH/LTC/BCH) routes via PAXOS exchange in USD — the only
    venue IBKR supports for crypto. Orders on PAXOS must be LMT with
    outsideRth=True (MKT not accepted)."""
    m = meta(symbol)
    if m.asset_class == "crypto":
        return Crypto(symbol, "PAXOS", "USD")
    if m.primary_exchange:
        return Stock(symbol, "SMART", m.currency, primaryExchange=m.primary_exchange)
    return Stock(symbol, "SMART", m.currency)


async def qualify(ib: IB, symbol: str) -> Contract | None:
    """Qualify a contract with session-level caching of failures. Returns None
    if the symbol is blacklisted or qualification fails; the caller should
    skip the symbol silently — we only log once per session."""
    if symbol in _UNRESOLVABLE:
        return None
    c = _contract(symbol)
    try:
        await ib.qualifyContractsAsync(c)
        return c
    except Exception as exc:
        mark_unresolvable(symbol)
        log.warning("qualify_failed symbol=%s err=%s", symbol, exc)
        return None


# ── in-process bar cache ──────────────────────────────────────────────────────
# key = (symbol, bar_size, duration, useRTH) → (expiry_ts, HistResult)
_BAR_CACHE: dict[tuple[str, str, str, bool], tuple[float, HistResult]] = {}
# Per-key lock so concurrent scans of the same symbol don't race to the
# broker; the first one fetches, the rest wait + read the cache.
_BAR_LOCKS: dict[tuple[str, str, str, bool], asyncio.Lock] = {}


def _lock_for(key: tuple[str, str, str, bool]) -> asyncio.Lock:
    lk = _BAR_LOCKS.get(key)
    if lk is None:
        lk = asyncio.Lock()
        _BAR_LOCKS[key] = lk
    return lk


def _cache_get(key: tuple[str, str, str, bool]) -> HistResult | None:
    entry = _BAR_CACHE.get(key)
    if entry is None:
        return None
    expiry, value = entry
    if time.time() > expiry:
        _BAR_CACHE.pop(key, None)
        return None
    return value


def _cache_put(key: tuple[str, str, str, bool], value: HistResult, ttl: float) -> None:
    _BAR_CACHE[key] = (time.time() + ttl, value)


def cache_stats() -> dict[str, int]:
    return {"entries": len(_BAR_CACHE)}


def _bars_to_hist(bars: list[Any]) -> HistResult | None:
    if not bars:
        return None
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    volumes: list[float] = []
    for b in bars:
        c = getattr(b, "close", None)
        if c is None or c <= 0:
            continue
        closes.append(float(c))
        h = getattr(b, "high", None)
        lo = getattr(b, "low", None)
        v = getattr(b, "volume", None)
        highs.append(float(h) if h is not None else float(c))
        lows.append(float(lo) if lo is not None else float(c))
        volumes.append(float(v) if v is not None and v > 0 else 0.0)
    if not closes:
        return None
    return HistResult(closes=closes, last_close=closes[-1],
                       highs=highs, lows=lows, volumes=volumes)


async def _fetch_historical(
    ib: IB, symbol: str, duration: str, bar_size: str,
    use_rth: bool, ttl_sec: float,
) -> HistResult | None:
    if is_hist_blacklisted(symbol):
        return None
    key = (symbol, bar_size, duration, use_rth)
    async with _lock_for(key):
        cached = _cache_get(key)
        if cached is not None:
            return cached
        c = await qualify(ib, symbol)
        if c is None:
            return None
        # IBKR crypto historical bars require whatToShow=AGGTRADES (aggregated
        # trades across makers); TRADES is stocks-only. Crypto is 24/7 so
        # useRTH is also forced to False — RTH=True drops all weekend bars.
        is_crypto = meta(symbol).asset_class == "crypto"
        what_to_show = "AGGTRADES" if is_crypto else "TRADES"
        effective_rth = False if is_crypto else use_rth
        try:
            bars = await ib.reqHistoricalDataAsync(
                c, endDateTime="", durationStr=duration,
                barSizeSetting=bar_size, whatToShow=what_to_show,
                useRTH=effective_rth, formatDate=1,
            )
        except Exception as exc:
            log.warning("hist_fetch_failed symbol=%s bar=%s dur=%s err=%s",
                        symbol, bar_size, duration, exc)
            return None
        hist = _bars_to_hist(bars)
        if hist is None:
            _note_hist_empty(symbol)
        else:
            _note_hist_ok(symbol)
            _cache_put(key, hist, ttl_sec)
        return hist


async def get_daily_closes(
    ib: IB, symbol: str, lookback_days: int = 250, ttl_sec: float = 240.0,
) -> HistResult | None:
    """Fetch daily TRADES bars (RTH only). Default 250-day window so SMA200 fits.
    Cached per (symbol, bar_size, duration). TTL default 4min aligns with 5-min scan.

    IBKR's "D" duration is reliable up to ~365 days; beyond that many contracts
    return Error 366. Switch to year-granularity strings above that threshold."""
    if lookback_days > 365:
        years = max(2, (lookback_days + 364) // 365)
        duration = f"{min(years, 5)} Y"
    else:
        duration = f"{lookback_days} D"
    return await _fetch_historical(
        ib, symbol, duration=duration, bar_size="1 day",
        use_rth=True, ttl_sec=ttl_sec,
    )


async def get_intraday_closes(
    ib: IB, symbol: str, bar_size: str = "5 mins", duration: str = "2 D",
    ttl_sec: float = 45.0,
) -> HistResult | None:
    """Intraday bars for fast-RSI mean reversion. TTL default 45s (intraday
    scan cadence is 60s)."""
    return await _fetch_historical(
        ib, symbol, duration=duration, bar_size=bar_size,
        use_rth=True, ttl_sec=ttl_sec,
    )


async def _gather_with_sem(aws: list, concurrency: int) -> list:
    sem = asyncio.Semaphore(max(1, concurrency))
    async def _run(a):
        async with sem:
            try:
                return await a
            except Exception as exc:
                log.warning("gather_task_failed err=%s", exc)
                return None
    return await asyncio.gather(*[_run(a) for a in aws])


async def get_daily_closes_many(
    ib: IB, symbols: list[str], lookback_days: int = 250,
    concurrency: int = 8, ttl_sec: float = 240.0,
) -> dict[str, HistResult | None]:
    aws = [get_daily_closes(ib, s, lookback_days, ttl_sec=ttl_sec) for s in symbols]
    results = await _gather_with_sem(aws, concurrency)
    return {s: r for s, r in zip(symbols, results)}


async def get_intraday_closes_many(
    ib: IB, symbols: list[str], bar_size: str = "5 mins", duration: str = "2 D",
    concurrency: int = 8, ttl_sec: float = 45.0,
) -> dict[str, HistResult | None]:
    aws = [get_intraday_closes(ib, s, bar_size, duration, ttl_sec=ttl_sec) for s in symbols]
    results = await _gather_with_sem(aws, concurrency)
    return {s: r for s, r in zip(symbols, results)}


_DELAYED_DATA_SET = False


async def _ensure_delayed_fallback(ib: IB) -> None:
    """Enable IBKR delayed-market-data fallback for this connection. Mode 3:
    use live if subscribed, otherwise fall back to delayed (free for most
    venues, ~15-min lag). Eliminates 'Error 354: Requested market data is
    not subscribed' on paper accounts for EU venues."""
    global _DELAYED_DATA_SET
    if _DELAYED_DATA_SET:
        return
    try:
        ib.reqMarketDataType(3)  # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
        _DELAYED_DATA_SET = True
    except Exception as exc:
        log.warning("reqMarketDataType_failed err=%s", exc)


async def latest_trade_price(ib: IB, symbol: str) -> float | None:
    """Best available current price. Prefer live tick snapshot (sub-second);
    use delayed ticks if live isn't subscribed; fall back to 1-min
    historical bar if both unavailable."""
    await _ensure_delayed_fallback(ib)
    c = await qualify(ib, symbol)
    if c is None:
        return None
    try:
        tickers = await ib.reqTickersAsync(c, regulatorySnapshot=False)
        if tickers:
            t = tickers[0]
            # delayedLast / delayedClose / delayedMarketPrice() filled by IB
            # when reqMarketDataType(3) is active and live is unsubscribed.
            for attr in ("last", "delayedLast", "close", "delayedClose"):
                px = getattr(t, attr, None)
                if px is not None and not (isinstance(px, float) and (px != px)) and px > 0:
                    return float(px)
            try:
                mp = t.marketPrice()
                if mp is not None and not (isinstance(mp, float) and (mp != mp)) and mp > 0:
                    return float(mp)
            except Exception:
                pass
    except Exception:
        pass
    if is_hist_blacklisted(symbol):
        return None
    try:
        what_to_show = "AGGTRADES" if meta(symbol).asset_class == "crypto" else "TRADES"
        bars = await ib.reqHistoricalDataAsync(
            c, endDateTime="", durationStr="120 S",
            barSizeSetting="1 min", whatToShow=what_to_show,
            useRTH=False, formatDate=1,
        )
        if bars and bars[-1].close:
            _note_hist_ok(symbol)
            return float(bars[-1].close)
        _note_hist_empty(symbol)
    except Exception:
        return None
    return None


def _is_crypto(symbol: str) -> bool:
    return meta(symbol).asset_class == "crypto"


# ── Crypto shadow-sim (paper mode) ────────────────────────────────────────────
# IBKR paper accounts cannot be permissioned for Crypto trading on PAXOS;
# orders silently flip to Inactive. Shadow-sim bypasses ib.placeOrder for
# crypto symbols and synthesizes a fill at latest_trade_price ± slippage.
# Live IBKR data feed still populates prices + bars, so P/L realism is high.
# Toggle via strategy.run_once on each tick from cfg['CRYPTO_PAPER_SIM'].

_paper_sim_crypto: bool = False
_sim_order_id_seq: int = 90_000_000  # well above real IBKR order IDs
_SIM_SLIPPAGE_BPS: float = 3.0  # 0.03% — realistic PAXOS crossing


def set_crypto_paper_sim(enabled: bool) -> None:
    global _paper_sim_crypto
    _paper_sim_crypto = bool(enabled)


def is_crypto_paper_sim() -> bool:
    return _paper_sim_crypto


def set_crypto_paper_sim_slippage_bps(bps: float) -> None:
    global _SIM_SLIPPAGE_BPS
    _SIM_SLIPPAGE_BPS = float(bps)


@dataclass
class _SimOrderStatus:
    status: str = "Filled"
    avgFillPrice: float = 0.0
    filled: float = 0.0
    remaining: float = 0.0


@dataclass
class _SimOrder:
    orderId: int = 0
    lmtPrice: float = 0.0
    orderRef: str = ""


@dataclass
class _SimTrade:
    """Minimal stand-in for ib_async.Trade that the strategy consumes.
    Downstream code reads trade.orderStatus.{status, avgFillPrice, filled}
    and trade.order.{orderId, lmtPrice, orderRef} — replicate exactly those.
    """
    order: _SimOrder
    orderStatus: _SimOrderStatus
    contract: object = None


async def _simulate_crypto_order(
    ib: IB, symbol: str, side: str, qty: float, limit_price: float,
    coid: str,
) -> _SimTrade:
    """Synthesize a crypto order fill using live IBKR price as the anchor.
    - BUY fills at min(limit_price, ref * (1 + slip)) when limit >= ref,
      else status=Inactive (limit below market — would never fill on real).
    - SELL fills at max(limit_price, ref * (1 - slip)) when limit <= ref,
      else status=Inactive (limit above market).
    Slippage floor reflects PAXOS paper spread realism."""
    global _sim_order_id_seq
    _sim_order_id_seq += 1
    order_id = _sim_order_id_seq
    ref_px = await latest_trade_price(ib, symbol)
    tick = await tick_size.min_tick(ib, symbol, price=ref_px or limit_price)
    slip_frac = _SIM_SLIPPAGE_BPS / 10_000.0

    if ref_px is None or ref_px <= 0:
        # No price anchor — refuse to simulate a fill; downstream will skip.
        log.warning("sim_order_no_ref_price symbol=%s side=%s", symbol, side)
        status = _SimOrderStatus(status="Inactive", filled=0.0, remaining=qty)
        order = _SimOrder(orderId=order_id, lmtPrice=limit_price, orderRef=coid)
        return _SimTrade(order=order, orderStatus=status)

    if side == "BUY":
        cap = ref_px * (1 + slip_frac)
        if limit_price >= ref_px:
            fill = tick_size.round_to_tick(min(limit_price, cap), tick, "up")
            status = _SimOrderStatus(status="Filled", avgFillPrice=fill,
                                       filled=qty, remaining=0.0)
        else:
            status = _SimOrderStatus(status="Inactive", filled=0.0, remaining=qty)
    else:  # SELL
        floor = ref_px * (1 - slip_frac)
        if limit_price <= ref_px:
            fill = tick_size.round_to_tick(max(limit_price, floor), tick, "down")
            status = _SimOrderStatus(status="Filled", avgFillPrice=fill,
                                       filled=qty, remaining=0.0)
        else:
            status = _SimOrderStatus(status="Inactive", filled=0.0, remaining=qty)

    order = _SimOrder(orderId=order_id,
                      lmtPrice=status.avgFillPrice or limit_price,
                      orderRef=coid)
    log.info(
        "crypto_sim_fill symbol=%s side=%s qty=%s lmt=%s ref=%s status=%s fill=%s",
        symbol, side, qty, limit_price, ref_px, status.status, status.avgFillPrice,
    )
    return _SimTrade(order=order, orderStatus=status)


async def place_limit_buy(
    ib: IB, symbol: str, qty: float, limit_price: float,
    *, client_order_id: str | None = None,
) -> tuple[Trade | None, str | None, fill_quality.Quote]:
    """Submit a limit BUY. Returns (trade, coid, quote_at_submit).

    Quote is captured right before placeOrder for slippage accounting. Empty
    Quote on failure paths — callers must treat missing bid/ask as skip."""
    if _is_crypto(symbol) and _paper_sim_crypto:
        coid = client_order_id or str(uuid.uuid4())
        trade = await _simulate_crypto_order(ib, symbol, "BUY", qty, limit_price, coid)
        return trade, coid, fill_quality.Quote()
    c = await qualify(ib, symbol)
    if c is None:
        return None, None, fill_quality.Quote()
    try:
        tick = await tick_size.min_tick(ib, symbol, price=limit_price)
        px = tick_size.round_to_tick(limit_price, tick, direction="up")
        coid = client_order_id or str(uuid.uuid4())
        order = LimitOrder("BUY", qty, px)
        if _is_crypto(symbol):
            order.tif = "GTC"
            order.outsideRth = True
        else:
            order.tif = "DAY"
        order.orderRef = coid
        quote = await fill_quality.capture_quote(ib, c)
        return ib.placeOrder(c, order), coid, quote
    except Exception as exc:
        log.warning("place_order_failed symbol=%s side=BUY err=%s", symbol, exc)
        return None, None, fill_quality.Quote()


async def place_limit_sell(
    ib: IB, symbol: str, qty: float, limit_price: float,
    *, client_order_id: str | None = None,
) -> tuple[Trade | None, str | None, fill_quality.Quote]:
    """Submit a limit SELL. Returns (trade, coid, quote_at_submit)."""
    if _is_crypto(symbol) and _paper_sim_crypto:
        coid = client_order_id or str(uuid.uuid4())
        trade = await _simulate_crypto_order(ib, symbol, "SELL", qty, limit_price, coid)
        return trade, coid, fill_quality.Quote()
    c = await qualify(ib, symbol)
    if c is None:
        return None, None, fill_quality.Quote()
    try:
        tick = await tick_size.min_tick(ib, symbol, price=limit_price)
        px = tick_size.round_to_tick(limit_price, tick, direction="down")
        coid = client_order_id or str(uuid.uuid4())
        order = LimitOrder("SELL", qty, px)
        if _is_crypto(symbol):
            order.tif = "GTC"
            order.outsideRth = True
        else:
            order.tif = "DAY"
        order.orderRef = coid
        quote = await fill_quality.capture_quote(ib, c)
        return ib.placeOrder(c, order), coid, quote
    except Exception as exc:
        log.warning("place_order_failed symbol=%s side=SELL err=%s", symbol, exc)
        return None, None, fill_quality.Quote()


async def place_market_sell(
    ib: IB, symbol: str, qty: float,
    *, client_order_id: str | None = None,
) -> tuple[Trade | None, str | None, fill_quality.Quote]:
    """Submit a MARKET SELL. Used as the stop-loss exit path: unlike our
    price-0.05 limit, a MKT order guarantees fill even in a falling market —
    necessary for real stop protection. Caller should still have screened
    for market-open for this symbol's currency.

    Crypto: IBKR PAXOS rejects MKT. Substitute an aggressive LMT SELL at
    best-bid-approx (price * 0.995) with tif=IOC + outsideRth=True, which
    sweeps the order book immediately while capping worst-case slippage at
    0.5%. Caller must ensure `latest_trade_price` is used as the reference."""
    if _is_crypto(symbol) and _paper_sim_crypto:
        coid = client_order_id or str(uuid.uuid4())
        ref_px = await latest_trade_price(ib, symbol)
        if ref_px is None or ref_px <= 0:
            log.warning("crypto_sim_mkt_sell_no_ref_price symbol=%s", symbol)
            return None, None, fill_quality.Quote()
        aggressive_lmt = ref_px * 0.995
        trade = await _simulate_crypto_order(ib, symbol, "SELL", qty, aggressive_lmt, coid)
        return trade, coid, fill_quality.Quote()
    c = await qualify(ib, symbol)
    if c is None:
        return None, None, fill_quality.Quote()
    try:
        coid = client_order_id or str(uuid.uuid4())
        if _is_crypto(symbol):
            ref_px = await latest_trade_price(ib, symbol)
            if ref_px is None or ref_px <= 0:
                log.warning("crypto_mkt_sell_no_ref_price symbol=%s", symbol)
                return None, None, fill_quality.Quote()
            tick = await tick_size.min_tick(ib, symbol, price=ref_px)
            aggressive = tick_size.round_to_tick(ref_px * 0.995, tick, direction="down")
            order = LimitOrder("SELL", qty, aggressive)
            order.tif = "GTC"
            order.outsideRth = True
        else:
            order = MarketOrder("SELL", qty)
            order.tif = "DAY"
        order.orderRef = coid
        quote = await fill_quality.capture_quote(ib, c)
        return ib.placeOrder(c, order), coid, quote
    except Exception as exc:
        log.warning("place_order_failed symbol=%s side=SELL(MKT) err=%s", symbol, exc)
        return None, None, fill_quality.Quote()


async def place_moc_sell(
    ib: IB, symbol: str, qty: float,
    *, client_order_id: str | None = None,
) -> tuple[Trade | None, str | None, fill_quality.Quote]:
    """Market-on-Close SELL. Routes the order to the closing auction so the
    fill lands at the official closing print. Backed by Reddit research
    (r/algotrading 1f0689m): strategies that backtest on the close but live-
    execute seconds before close blow up because the auction mechanics differ
    from continuous trading. Submitting as MOC lets the auction do the work.

    Caller should only route here when the venue's MOC deadline has not passed
    (roughly 10-15 min before close for US equities, tighter for Euronext).
    Returns (trade, coid, quote_at_submit). Crypto is rejected — no auction."""
    if _is_crypto(symbol):
        log.warning("moc_sell_rejected_crypto symbol=%s", symbol)
        return None, None, fill_quality.Quote()
    c = await qualify(ib, symbol)
    if c is None:
        return None, None, fill_quality.Quote()
    try:
        coid = client_order_id or str(uuid.uuid4())
        order = Order(action="SELL", totalQuantity=qty, orderType="MOC", tif="DAY")
        order.orderRef = coid
        quote = await fill_quality.capture_quote(ib, c)
        return ib.placeOrder(c, order), coid, quote
    except Exception as exc:
        log.warning("place_order_failed symbol=%s side=SELL(MOC) err=%s", symbol, exc)
        return None, None, fill_quality.Quote()


async def place_moc_buy(
    ib: IB, symbol: str, qty: float,
    *, client_order_id: str | None = None,
) -> tuple[Trade | None, str | None, fill_quality.Quote]:
    """Market-on-Close BUY. Entry primitive for the overnight strategy: routes
    to the closing auction so the fill lands at the official closing print.
    Submitting as MOC lets the auction absorb the order rather than chasing the
    tape seconds before close. Caller must submit before the venue's MOC
    deadline (~15:50 ET for US stocks). Crypto is rejected — no auction."""
    if _is_crypto(symbol):
        log.warning("moc_buy_rejected_crypto symbol=%s", symbol)
        return None, None, fill_quality.Quote()
    c = await qualify(ib, symbol)
    if c is None:
        return None, None, fill_quality.Quote()
    try:
        coid = client_order_id or str(uuid.uuid4())
        order = Order(action="BUY", totalQuantity=qty, orderType="MOC", tif="DAY")
        order.orderRef = coid
        quote = await fill_quality.capture_quote(ib, c)
        return ib.placeOrder(c, order), coid, quote
    except Exception as exc:
        log.warning("place_order_failed symbol=%s side=BUY(MOC) err=%s", symbol, exc)
        return None, None, fill_quality.Quote()


async def place_moo_sell(
    ib: IB, symbol: str, qty: float,
    *, client_order_id: str | None = None,
) -> tuple[Trade | None, str | None, fill_quality.Quote]:
    """Market-on-Open SELL. Exit primitive for the overnight strategy: the
    order is held by IBKR overnight and released into the primary opening
    cross. Using orderType=MKT + tif=OPG is the documented ib_async recipe for
    auction-only orders; exits fill at the official opening print. Submit
    window: after prior close, before ~09:28 ET (NYSE/NASDAQ opening cross
    deadline). Crypto is rejected — no auction."""
    if _is_crypto(symbol):
        log.warning("moo_sell_rejected_crypto symbol=%s", symbol)
        return None, None, fill_quality.Quote()
    c = await qualify(ib, symbol)
    if c is None:
        return None, None, fill_quality.Quote()
    try:
        coid = client_order_id or str(uuid.uuid4())
        order = Order(action="SELL", totalQuantity=qty, orderType="MKT", tif="OPG")
        order.orderRef = coid
        quote = await fill_quality.capture_quote(ib, c)
        return ib.placeOrder(c, order), coid, quote
    except Exception as exc:
        log.warning("place_order_failed symbol=%s side=SELL(MOO) err=%s", symbol, exc)
        return None, None, fill_quality.Quote()


async def place_stop_sell(
    ib: IB, symbol: str, qty: float, stop_price: float,
    *, limit_offset_pct: float | None = None,
    client_order_id: str | None = None,
) -> tuple[Trade | None, str | None]:
    """Submit a resting STP (or STP LMT) SELL. Rounded DOWN to minTick.
    When `limit_offset_pct` is given, a STP LMT is placed with the limit
    `stop_price * (1 - limit_offset_pct/100)` — caps slippage at the cost of
    non-fill risk on gap-downs. When None, a plain STP (becomes MKT on
    trigger) is used — guaranteed fill but unbounded slippage.

    Native IBKR stops trigger on tape (trade) conditions, so they fire during
    fast moves that a poll-and-cancel limit loop would miss."""
    c = await qualify(ib, symbol)
    if c is None:
        return None, None
    try:
        tick = await tick_size.min_tick(ib, symbol, price=stop_price)
        stop_px = tick_size.round_to_tick(stop_price, tick, direction="down")
        coid = client_order_id or str(uuid.uuid4())
        if limit_offset_pct is not None and limit_offset_pct > 0:
            lmt = tick_size.round_to_tick(
                stop_px * (1 - limit_offset_pct / 100.0), tick, direction="down",
            )
            order = Order(
                action="SELL", orderType="STP LMT", totalQuantity=qty,
                auxPrice=stop_px, lmtPrice=lmt, tif="GTC",
            )
        else:
            order = StopOrder("SELL", qty, stop_px)
            order.tif = "GTC"
        order.orderRef = coid
        return ib.placeOrder(c, order), coid
    except Exception as exc:
        log.warning("place_order_failed symbol=%s side=SELL(STP) err=%s", symbol, exc)
        return None, None




async def place_bracket_buy(
    ib: IB, symbol: str, qty: float, limit_price: float,
    target_price: float, stop_price: float,
    *, client_order_id: str | None = None,
) -> tuple[list[Trade], str | None, fill_quality.Quote]:
    """Submit a native IBKR bracket order: Parent LMT Buy + Child LMT Sell
    (target) + Child STP Sell (stop). All three rest on IBKR's server so
    exit protection is active even if the bot or LXC loses connectivity.

    Returns (trades_list, parent_coid, quote_at_submit).
    Crypto is rejected — PAXOS does not support bracket orders.
    """
    if _is_crypto(symbol):
        log.warning("bracket_buy_rejected_crypto symbol=%s", symbol)
        return [], None, fill_quality.Quote()
    c = await qualify(ib, symbol)
    if c is None:
        return [], None, fill_quality.Quote()
    try:
        tick = await tick_size.min_tick(ib, symbol, price=limit_price)
        parent_px = tick_size.round_to_tick(limit_price, tick, direction="up")
        target_px = tick_size.round_to_tick(target_price, tick, direction="down")
        stop_px = tick_size.round_to_tick(stop_price, tick, direction="down")
        coid = client_order_id or str(uuid.uuid4())

        parent = LimitOrder("BUY", qty, parent_px)
        parent.tif = "DAY"
        parent.orderRef = coid + "_P"
        parent.transmit = False  # don't transmit until children attached

        take_profit = LimitOrder("SELL", qty, target_px)
        take_profit.tif = "GTC"
        take_profit.orderRef = coid + "_T"
        take_profit.parentId = 0  # will be set after parent placed
        take_profit.transmit = False

        stop_loss = StopOrder("SELL", qty, stop_px)
        stop_loss.tif = "GTC"
        stop_loss.orderRef = coid + "_S"
        stop_loss.parentId = 0
        stop_loss.transmit = True  # transmit the whole bracket

        quote = await fill_quality.capture_quote(ib, c)
        parent_trade = ib.placeOrder(c, parent)
        # Small delay so IBKR assigns parent orderId
        await asyncio.sleep(0.2)
        parent_id = getattr(parent_trade.order, "orderId", None)
        if parent_id is None:
            log.warning("bracket_parent_id_missing symbol=%s", symbol)
            return [parent_trade], coid, quote

        take_profit.parentId = parent_id
        stop_loss.parentId = parent_id
        tp_trade = ib.placeOrder(c, take_profit)
        sl_trade = ib.placeOrder(c, stop_loss)
        return [parent_trade, tp_trade, sl_trade], coid, quote
    except Exception as exc:
        log.warning("place_bracket_failed symbol=%s err=%s", symbol, exc)
        return [], None, fill_quality.Quote()

async def cancel_order_safe(ib: IB, trade: Trade | None) -> None:
    """Best-effort cancel; used when closing a position so the resting STP
    doesn't fire against the already-sold qty (paper IBKR tolerates orphaned
    cancels but real IBKR can mis-account)."""
    if trade is None or trade.orderStatus.status in {"Filled", "Cancelled", "ApiCancelled", "Inactive"}:
        return
    try:
        ib.cancelOrder(trade.order)
    except Exception as exc:
        log.warning("cancel_failed err=%s", exc)


async def wait_for_fill_or_cancel(
    trade: Trade, timeout_sec: float = 60, ib: IB | None = None
) -> str:
    """Wait for terminal order status; cancel if not filled by timeout."""
    async def _await_done() -> str:
        while True:
            status = trade.orderStatus.status
            if status in {"Filled", "Cancelled", "Inactive", "ApiCancelled"}:
                return status
            await asyncio.sleep(0.5)

    try:
        return await asyncio.wait_for(_await_done(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        pass
    # Timeout: request cancel, but IBKR may fill during the cancel race window.
    # Preserve whatever terminal state actually happens — treating a racing fill
    # as "TimedOut" would leave a zombie position at IBKR that the bot ignores.
    if ib is not None:
        try:
            ib.cancelOrder(trade.order)
            final = await asyncio.wait_for(_await_done(), timeout=10)
            return final
        except Exception:
            pass
    # Last-ditch: if any quantity filled during the race, caller must record it.
    if trade.orderStatus.filled and trade.orderStatus.filled > 0:
        return "Filled"
    return "TimedOut"
