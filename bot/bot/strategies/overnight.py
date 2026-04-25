"""Overnight Edge strategy — MOC entry at close, MOO exit at next open.

Completely isolated from mean-reversion (bot/strategy.py):
  * Own slot range: 25-29.
  * Own kill switch: config.OVERNIGHT_ENABLED (separate from BOT_ENABLED).
  * Own strategy tag on positions/signals/orders: 'overnight'.
  * Paper-only asserted at runtime — refuses to place orders if
    config.TRADING_MODE != 'paper'.

Thesis: SPY-universe overnight returns accrue as a variance risk premium
between ~15:58 ET close and ~09:31 ET open. Filtered variants (earnings-
clear, SPY-trend-gated, momentum-ranked, VWAP-proximate) preserve the edge
in retail replication after the naive form decayed.

Schedule (all America/New_York wall-clock, DST-aware via zoneinfo):
  15:45-15:55  scan + place MOC BUY for top N candidates
  anytime      monitor MOC BUY fills → submit MOO SELL (tif=OPG)
  09:25-09:30  safety: verify MOO SELL still live for each open position
  anytime      monitor MOO SELL fills → close position

No LLM calls in v1: overnight is a pure-quant filter; LLM cost not justified.
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
import uuid
from datetime import date, datetime, time as dtime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
from ib_async import IB

from .. import broker, earnings, sizing, snapshots
from ..universe import UNIVERSE_META

log = logging.getLogger("bot.overnight")

STRATEGY_TAG = "overnight"
SLOT_RANGE = range(25, 30)  # 25..29 inclusive

ET = ZoneInfo("America/New_York")

# Scan window: 15:45-15:55 ET. MOC deadline on US primary exchanges is ~15:50
# ET; submitting before 15:45 gives IBKR time to route to the auction.
SCAN_START_ET = dtime(15, 45)
SCAN_END_ET = dtime(15, 55)

# Exit-safety window: 09:25-09:30 ET. If our MOO SELL is missing at 09:25,
# emergency-submit before the 09:28 opening cross deadline.
EXIT_SAFETY_START_ET = dtime(9, 25)
EXIT_SAFETY_END_ET = dtime(9, 30)

# Filter parameters (conservative defaults; tuneable via config later).
SPY_SMA_WINDOW = 50
SPY_MIN_RATIO_TO_SMA50 = 0.98  # skip day when SPY < SMA50 * this
INTRADAY_MAX_DROP_PCT = 2.0    # skip candidate if down > this today
MOMENTUM_LOOKBACK_DAYS = 21    # ~1 month of trading days
VOL_LOOKBACK_DAYS = 20
MAX_CANDIDATES = 5             # matches slot count

# Composite rank weights: positive momentum preferred, VWAP proximity preferred,
# low vol preferred. Kept simple — optimizer can tune later.
W_MOMENTUM = 2.0
W_VWAP_DIST = 1.0
W_VOL = 0.5


# ── Gates ────────────────────────────────────────────────────────────────────

def _now_et() -> datetime:
    return datetime.now(tz=ET)


def _et_today(now_et: datetime | None = None) -> date:
    return (now_et or _now_et()).date()


def _is_weekday(now_et: datetime) -> bool:
    return now_et.weekday() < 5


def _in_scan_window(now_et: datetime) -> bool:
    return _is_weekday(now_et) and SCAN_START_ET <= now_et.time() < SCAN_END_ET


def _in_exit_safety_window(now_et: datetime) -> bool:
    return _is_weekday(now_et) and EXIT_SAFETY_START_ET <= now_et.time() < EXIT_SAFETY_END_ET


def _is_enabled(cfg: dict) -> tuple[bool, str]:
    """Strategy-level gate. Returns (enabled, reason_if_disabled)."""
    if cfg.get("OVERNIGHT_ENABLED") is not True:
        return False, "OVERNIGHT_ENABLED=false"
    if cfg.get("BOT_ENABLED") is not True:
        return False, "BOT_ENABLED=false"
    # Hard paper-only gate. Overnight gap risk on individual names is the exact
    # failure mode we don't want to debut against real capital — keep this
    # assertion even after option B ships.
    mode = cfg.get("TRADING_MODE")
    if mode != "paper":
        return False, f"TRADING_MODE={mode!r} (overnight is paper-only in v1)"
    return True, ""


# ── Universe ─────────────────────────────────────────────────────────────────

def us_stock_universe() -> list[str]:
    """US-listed stocks in USD from UNIVERSE_META. Excludes EU blue-chips
    (currency != USD), crypto (asset_class != stock), and anything without
    explicit metadata. Recomputed on each call so universe edits take effect
    without restart."""
    return sorted(
        sym for sym, m in UNIVERSE_META.items()
        if m.asset_class == "stock" and m.currency == "USD"
    )


# ── Filters ──────────────────────────────────────────────────────────────────

async def _spy_regime_ok(ib: IB) -> tuple[bool, str]:
    """SPY close must be ≥ SMA50 * SPY_MIN_RATIO_TO_SMA50. Returns (ok, reason).
    On fetch failure, FAIL-SAFE and reject the day (better to miss trades than
    trade blind into a downtrend that inverts the edge)."""
    hist = await broker.get_daily_closes(ib, "SPY", lookback_days=SPY_SMA_WINDOW + 5)
    if hist is None or len(hist.closes) < SPY_SMA_WINDOW:
        return False, "spy_hist_unavailable"
    closes = hist.closes
    sma50 = sum(closes[-SPY_SMA_WINDOW:]) / SPY_SMA_WINDOW
    last = closes[-1]
    ratio = last / sma50 if sma50 > 0 else 0.0
    if ratio < SPY_MIN_RATIO_TO_SMA50:
        return False, f"spy_below_sma50:last={last:.2f}_sma50={sma50:.2f}_ratio={ratio:.3f}"
    return True, f"spy_ok:ratio={ratio:.3f}"


async def _earnings_clear(pool: asyncpg.Pool, symbol: str, cfg: dict) -> tuple[bool, str]:
    """Reuse bot.earnings for the 2-session buffer. Uses our overnight slot
    profile's earnings_blackout_days=3 (calendar days covering Fri→Mon)."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT symbol, earnings_date FROM earnings_calendar
                WHERE earnings_date >= CURRENT_DATE - INTERVAL '1 day'"""
        )
    # Synthetic slot profile with the overnight blackout_days value. We don't
    # join slot_profiles here because the profile is uniform across slots 25-29.
    sp = {"slot": "overnight", "earnings_blackout_days": 3}
    reason = earnings.apply_earnings_blackout(
        sp, symbol, _et_today(), [dict(r) for r in rows], cfg,
    )
    if reason is None:
        return True, "earnings_ok"
    return False, reason


async def _intraday_drop_ok(ib: IB, symbol: str) -> tuple[bool, str, float | None]:
    """Skip candidates down more than INTRADAY_MAX_DROP_PCT today. Uses 5-min
    bars to compute today's open (first bar) vs latest price. Returns
    (ok, reason, current_price_hint)."""
    hist = await broker.get_intraday_closes(ib, symbol, bar_size="5 mins", duration="1 D")
    if hist is None or len(hist.closes) < 2:
        return False, "intraday_hist_unavailable", None
    today_open = hist.closes[0]
    current = hist.closes[-1]
    drop_pct = (current - today_open) / today_open * 100 if today_open > 0 else 0.0
    if drop_pct < -INTRADAY_MAX_DROP_PCT:
        return False, f"intraday_drop:{drop_pct:.2f}pct", current
    return True, f"intraday_ok:{drop_pct:.2f}pct", current


# ── Ranking ──────────────────────────────────────────────────────────────────

def _momentum_1m(closes: list[float]) -> float | None:
    if len(closes) < MOMENTUM_LOOKBACK_DAYS + 1:
        return None
    return (closes[-1] - closes[-MOMENTUM_LOOKBACK_DAYS - 1]) / closes[-MOMENTUM_LOOKBACK_DAYS - 1]


def _vol_20d(closes: list[float]) -> float | None:
    if len(closes) < VOL_LOOKBACK_DAYS + 1:
        return None
    returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(len(closes) - VOL_LOOKBACK_DAYS, len(closes))
        if closes[i - 1] > 0
    ]
    if len(returns) < 2:
        return None
    return statistics.stdev(returns)


def _vwap_distance(intraday_closes: list[float], intraday_volumes: list[float]) -> float | None:
    """Today's VWAP distance as (current - vwap) / vwap. 0 is "at VWAP".
    Positive = overextended above VWAP (penalized). Uses 5-min bar closes + volumes
    from broker.get_intraday_closes."""
    if not intraday_closes or len(intraday_closes) != len(intraday_volumes):
        return None
    total_v = sum(intraday_volumes)
    if total_v <= 0:
        return None
    vwap = sum(p * v for p, v in zip(intraday_closes, intraday_volumes)) / total_v
    current = intraday_closes[-1]
    if vwap <= 0:
        return None
    return (current - vwap) / vwap


def _composite_score(momentum: float, vwap_dist: float, vol: float) -> float:
    """Higher is better. Positive momentum contributes, VWAP distance
    (either direction) penalizes, higher vol penalizes."""
    return W_MOMENTUM * momentum - W_VWAP_DIST * abs(vwap_dist) - W_VOL * vol


async def _rank_candidates(
    ib: IB, pool: asyncpg.Pool, cfg: dict, symbols: list[str],
) -> list[dict[str, Any]]:
    """Score every candidate that passes all filters. Returns list of dicts
    with keys: symbol, score, current_price, momentum, vwap_dist, vol."""
    scored: list[dict[str, Any]] = []
    for sym in symbols:
        if broker.is_unresolvable(sym) or broker.is_hist_blacklisted(sym):
            continue

        ok, reason = await _earnings_clear(pool, sym, cfg)
        if not ok:
            log.info(_j("overnight_skip", symbol=sym, reason=reason))
            continue

        ok, reason, current = await _intraday_drop_ok(ib, sym)
        if not ok:
            log.info(_j("overnight_skip", symbol=sym, reason=reason))
            continue

        daily = await broker.get_daily_closes(ib, sym, lookback_days=60)
        if daily is None or len(daily.closes) < MOMENTUM_LOOKBACK_DAYS + 1:
            log.info(_j("overnight_skip", symbol=sym, reason="daily_hist_short"))
            continue
        momentum = _momentum_1m(daily.closes)
        vol = _vol_20d(daily.closes)
        if momentum is None or vol is None:
            continue
        if momentum <= 0:
            # Spec: "positive 1-month momentum" — strict filter, not a weight.
            log.info(_j("overnight_skip", symbol=sym, reason=f"momentum_neg:{momentum:.4f}"))
            continue

        intraday = await broker.get_intraday_closes(ib, sym, bar_size="5 mins", duration="1 D")
        if intraday is None or not intraday.volumes:
            log.info(_j("overnight_skip", symbol=sym, reason="intraday_vol_missing"))
            continue
        vwap_dist = _vwap_distance(intraday.closes, intraday.volumes)
        if vwap_dist is None:
            continue

        score = _composite_score(momentum, vwap_dist, vol)
        scored.append({
            "symbol": sym, "score": score, "current_price": current or intraday.closes[-1],
            "momentum": momentum, "vwap_dist": vwap_dist, "vol": vol,
        })

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:MAX_CANDIDATES]


# ── Slot allocation ──────────────────────────────────────────────────────────

async def _free_overnight_slots(pool: asyncpg.Pool) -> list[int]:
    """Overnight slots (25-29) not currently occupied by open/opening/closing
    positions. Returns sorted list so allocation is deterministic."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT slot FROM positions
                WHERE strategy=$1 AND status IN ('opening','open','closing')""",
            STRATEGY_TAG,
        )
    occupied = {r["slot"] for r in rows}
    return [s for s in SLOT_RANGE if s not in occupied]


# ── DB writes (strategy-tagged) ──────────────────────────────────────────────

async def _insert_signal(
    pool: asyncpg.Pool, symbol: str, score: float, payload: dict,
    decision: str, reason: str,
) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO signals (symbol, quant_score, payload, decision, reason, strategy)
               VALUES ($1, $2, $3::jsonb, $4, $5, $6)""",
            symbol, score, payload, decision, reason, STRATEGY_TAG,
        )


async def _insert_position_opening(
    pool: asyncpg.Pool, symbol: str, slot: int, qty: float, current_price: float,
    sector: str, company_name: str,
) -> int:
    # opened_at set explicitly so the 034 trade_outcomes trigger can bound
    # its signal_snapshots lookup window (snapshot_ts <= opened_at + 5min).
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """INSERT INTO positions
               (symbol, slot, status, entry_price, qty, current_price,
                last_price_update, opened_at, sector, company_name, strategy)
               VALUES ($1, $2, 'opening', NULL, $3, $4, NOW(), NOW(), $5, $6, $7) RETURNING id""",
            symbol, slot, qty, current_price, sector, company_name, STRATEGY_TAG,
        )
    return row["id"]


async def _insert_order(
    pool: asyncpg.Pool, position_id: int, side: str, coid: str,
    ib_order_id: int | None, status: str,
) -> int:
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """INSERT INTO orders
               (position_id, side, status, ib_order_id, raw, client_order_id, strategy)
               VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7) RETURNING id""",
            position_id, side, status, ib_order_id,
            {"type": "MOC" if side == "BUY" else "MOO", "strategy": STRATEGY_TAG},
            coid, STRATEGY_TAG,
        )
    return row["id"]


async def _insert_entry_snapshot(
    pool: asyncpg.Pool, symbol: str, slot: int, score: float,
    current_price: float, momentum: float, vwap_dist: float, vol: float,
    position_id: int,
) -> None:
    """Write the signal_snapshots row that the 034 trade_outcomes trigger
    reads when the overnight position later closes. Without this, the trigger
    writes trade_outcomes.strategy='unknown' (COALESCE fallback), breaking
    per-strategy attribution on the dashboard.
    Called after the MOC BUY is submitted — gate_outcome='executed' matches
    the trigger's lookup filter."""
    row = snapshots.build_snapshot_row(
        symbol=symbol,
        strategy=STRATEGY_TAG,
        slot_id=slot,
        payload={
            "score": score,
            "vwap_distance_pct": vwap_dist * 100 if vwap_dist is not None else None,
            "vol_ratio": None,  # overnight uses absolute stdev, not a ratio
        },
        gate_outcome="executed",
        trade_id=position_id,
    )
    await snapshots.insert_snapshot(pool, row)


async def _update_order_terminal(
    pool: asyncpg.Pool, order_id: int, status: str, fill_price: float | None,
    fill_qty: float | None,
) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """UPDATE orders SET status=$2, fill_price=$3, fill_qty=$4 WHERE id=$1""",
            order_id, status, fill_price, fill_qty,
        )


async def _mark_position_open(
    pool: asyncpg.Pool, position_id: int, entry_price: float, qty: float,
) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """UPDATE positions SET status='open', entry_price=$2, qty=$3,
                   current_price=$2, last_price_update=NOW() WHERE id=$1""",
            position_id, entry_price, qty,
        )


async def _mark_position_closed(
    pool: asyncpg.Pool, position_id: int, exit_price: float,
) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """UPDATE positions SET status='closed', exit_price=$2,
                   current_price=$2, last_price_update=NOW(), closed_at=NOW()
                WHERE id=$1""",
            position_id, exit_price,
        )


async def _mark_position_error(pool: asyncpg.Pool, position_id: int, reason: str) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """UPDATE positions SET status='error', closed_at=NOW() WHERE id=$1""",
            position_id,
        )
    log.error(_j("overnight_position_error", position_id=position_id, reason=reason))


# ── MOC BUY placement (scan phase) ───────────────────────────────────────────

# In-process registry of live trades so we can poll their orderStatus without
# re-hydrating from IBKR each tick. Keyed by order id (our DB orders.id).
# Does not survive a bot restart — the reconciler in main.py covers orphan
# recovery by closing DB positions that no longer exist at IBKR.
_LIVE_TRADES: dict[int, Any] = {}


async def _already_scanned_today(pool: asyncpg.Pool, today_iso: str) -> bool:
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT value FROM config WHERE key='_last_overnight_scan_date'"
        )
    return row is not None and row["value"] == today_iso


async def _mark_scanned_today(pool: asyncpg.Pool, today_iso: str) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO config (key, value, updated_by)
               VALUES ('_last_overnight_scan_date', $1::jsonb, 'overnight')
               ON CONFLICT (key) DO UPDATE
                   SET value=EXCLUDED.value, updated_at=NOW()""",
            today_iso,
        )


async def maybe_scan(pool: asyncpg.Pool, ib: IB, cfg: dict) -> None:
    """Entry point called from the main tick. Cheap no-op outside scan window."""
    now_et = _now_et()
    if not _in_scan_window(now_et):
        return
    enabled, reason = _is_enabled(cfg)
    if not enabled:
        log.info(_j("overnight_scan_skipped", reason=reason))
        return

    today_iso = _et_today(now_et).isoformat()
    if await _already_scanned_today(pool, today_iso):
        return

    ok, reason = await _spy_regime_ok(ib)
    if not ok:
        log.info(_j("overnight_regime_block", reason=reason))
        await _mark_scanned_today(pool, today_iso)  # don't re-poll SPY every tick
        return

    free_slots = await _free_overnight_slots(pool)
    if not free_slots:
        log.info(_j("overnight_no_free_slots"))
        await _mark_scanned_today(pool, today_iso)
        return

    universe = us_stock_universe()
    candidates = await _rank_candidates(ib, pool, cfg, universe)
    if not candidates:
        log.info(_j("overnight_no_candidates", universe_size=len(universe)))
        await _mark_scanned_today(pool, today_iso)
        return

    slot_size_eur = float(cfg.get("SLOT_SIZE_EUR", 1000))
    placed = 0
    for cand in candidates[:len(free_slots)]:
        slot = free_slots[placed]
        qty = sizing.fixed_qty(slot_size_eur, cand["current_price"], asset_class="stock")
        if qty <= 0:
            log.warning(_j("overnight_zero_qty", symbol=cand["symbol"],
                           price=cand["current_price"]))
            continue
        sector = UNIVERSE_META[cand["symbol"]].sector
        name = UNIVERSE_META[cand["symbol"]].name
        try:
            position_id = await _insert_position_opening(
                pool, cand["symbol"], slot, qty, cand["current_price"], sector, name,
            )
            trade, coid, _quote = await broker.place_moc_buy(ib, cand["symbol"], qty)
            if trade is None or coid is None:
                await _mark_position_error(pool, position_id, "moc_buy_failed")
                continue
            ib_order_id = getattr(trade.order, "orderId", None)
            order_id = await _insert_order(
                pool, position_id, "BUY", coid, ib_order_id, "submitted",
            )
            _LIVE_TRADES[order_id] = trade
            await _insert_signal(
                pool, cand["symbol"], cand["score"],
                {
                    "slot": slot, "momentum": cand["momentum"],
                    "vwap_dist": cand["vwap_dist"], "vol": cand["vol"],
                    "qty": qty, "current_price": cand["current_price"],
                },
                decision="buy",
                reason=f"overnight_moc:score={cand['score']:.4f}",
            )
            await _insert_entry_snapshot(
                pool, cand["symbol"], slot, cand["score"],
                cand["current_price"], cand["momentum"],
                cand["vwap_dist"], cand["vol"], position_id,
            )
            log.info(_j("overnight_moc_buy_submitted",
                        symbol=cand["symbol"], slot=slot, qty=qty,
                        price=cand["current_price"], score=cand["score"]))
            placed += 1
        except Exception as exc:
            log.exception(_j("overnight_entry_failed", symbol=cand["symbol"], err=str(exc)))

    await _mark_scanned_today(pool, today_iso)
    log.info(_j("overnight_scan_complete", placed=placed,
                free_slots=len(free_slots), candidates=len(candidates)))


# ── MOC fill → MOO submit (monitoring phase) ─────────────────────────────────

async def _monitor_buy_fills(pool: asyncpg.Pool, ib: IB) -> None:
    """For each overnight position in 'opening' state, check its BUY order
    status. On Filled, advance position to 'open' and submit the MOO SELL."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT p.id AS position_id, p.symbol, p.qty, p.slot,
                      o.id AS order_id, o.ib_order_id, o.client_order_id
                 FROM positions p
                 JOIN orders o ON o.position_id = p.id AND o.side='BUY'
                                  AND o.status IN ('submitted','partial')
                WHERE p.strategy=$1 AND p.status='opening'""",
            STRATEGY_TAG,
        )
    for r in rows:
        trade = _LIVE_TRADES.get(r["order_id"])
        status = getattr(getattr(trade, "orderStatus", None), "status", None) if trade else None
        # If the trade isn't in our memory registry (bot restarted), resolve
        # from ib.trades() by client order id. Fall back to leaving as-is.
        if status is None and ib.isConnected():
            for t in ib.trades():
                if getattr(t.order, "orderRef", None) == str(r["client_order_id"]):
                    trade = t
                    _LIVE_TRADES[r["order_id"]] = t
                    status = t.orderStatus.status
                    break
        if status is None:
            continue

        if status == "Filled":
            fill_price = float(trade.orderStatus.avgFillPrice)
            fill_qty = float(trade.orderStatus.filled)
            if fill_price <= 0 or fill_qty <= 0:
                await _mark_position_error(pool, r["position_id"],
                                            f"moc_fill_inconsistent:price={fill_price}_qty={fill_qty}")
                await _update_order_terminal(pool, r["order_id"], "rejected", None, None)
                _LIVE_TRADES.pop(r["order_id"], None)
                continue
            await _update_order_terminal(pool, r["order_id"], "filled", fill_price, fill_qty)
            await _mark_position_open(pool, r["position_id"], fill_price, fill_qty)
            _LIVE_TRADES.pop(r["order_id"], None)
            # Submit the MOO SELL immediately — can rest until next open.
            sell_trade, sell_coid, _q = await broker.place_moo_sell(ib, r["symbol"], fill_qty)
            if sell_trade is None or sell_coid is None:
                log.error(_j("overnight_moo_sell_failed", symbol=r["symbol"],
                             position_id=r["position_id"]))
                continue
            sell_order_id = await _insert_order(
                pool, r["position_id"], "SELL", sell_coid,
                getattr(sell_trade.order, "orderId", None), "submitted",
            )
            _LIVE_TRADES[sell_order_id] = sell_trade
            log.info(_j("overnight_moo_sell_submitted", symbol=r["symbol"],
                        position_id=r["position_id"], qty=fill_qty))
        elif status in ("Cancelled", "ApiCancelled", "Inactive"):
            await _update_order_terminal(pool, r["order_id"], "cancelled", None, None)
            await _mark_position_error(pool, r["position_id"], f"moc_buy_{status.lower()}")
            _LIVE_TRADES.pop(r["order_id"], None)


async def _monitor_sell_fills(pool: asyncpg.Pool, ib: IB) -> None:
    """For each overnight position 'open' (or 'closing') with a live SELL
    order, close it when the SELL fills."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT p.id AS position_id, p.symbol,
                      o.id AS order_id, o.ib_order_id, o.client_order_id
                 FROM positions p
                 JOIN orders o ON o.position_id = p.id AND o.side='SELL'
                                  AND o.status IN ('submitted','partial')
                WHERE p.strategy=$1 AND p.status IN ('open','closing')""",
            STRATEGY_TAG,
        )
    for r in rows:
        trade = _LIVE_TRADES.get(r["order_id"])
        status = getattr(getattr(trade, "orderStatus", None), "status", None) if trade else None
        if status is None and ib.isConnected():
            for t in ib.trades():
                if getattr(t.order, "orderRef", None) == str(r["client_order_id"]):
                    trade = t
                    _LIVE_TRADES[r["order_id"]] = t
                    status = t.orderStatus.status
                    break
        if status is None:
            continue

        if status == "Filled":
            fill_price = float(trade.orderStatus.avgFillPrice)
            fill_qty = float(trade.orderStatus.filled)
            if fill_price <= 0:
                log.error(_j("overnight_moo_fill_inconsistent",
                             position_id=r["position_id"], fill_price=fill_price))
                continue
            await _update_order_terminal(pool, r["order_id"], "filled", fill_price, fill_qty)
            await _mark_position_closed(pool, r["position_id"], fill_price)
            _LIVE_TRADES.pop(r["order_id"], None)
            log.info(_j("overnight_position_closed", symbol=r["symbol"],
                        position_id=r["position_id"], exit_price=fill_price))
        elif status in ("Cancelled", "ApiCancelled", "Inactive"):
            await _update_order_terminal(pool, r["order_id"], "cancelled", None, None)
            # Revert position to 'open' so exit safety can re-submit.
            async with pool.acquire() as c:
                await c.execute(
                    "UPDATE positions SET status='open' WHERE id=$1 AND status='closing'",
                    r["position_id"],
                )
            log.warning(_j("overnight_moo_sell_lost", position_id=r["position_id"],
                           status=status))
            _LIVE_TRADES.pop(r["order_id"], None)


# ── Exit-window safety ───────────────────────────────────────────────────────

async def maybe_exit_safety_check(pool: asyncpg.Pool, ib: IB, cfg: dict) -> None:
    """Between 09:25 and 09:30 ET, verify every 'open' overnight position has
    a live SELL order. If missing (e.g. bot was down when MOC filled, or the
    MOO was cancelled), submit an emergency MOO SELL before the opening
    cross closes at ~09:28."""
    now_et = _now_et()
    if not _in_exit_safety_window(now_et):
        return
    enabled, reason = _is_enabled(cfg)
    if not enabled:
        return

    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT p.id AS position_id, p.symbol, p.qty
                 FROM positions p
                WHERE p.strategy=$1 AND p.status='open'
                  AND NOT EXISTS (
                      SELECT 1 FROM orders o
                       WHERE o.position_id = p.id AND o.side='SELL'
                         AND o.status IN ('submitted','partial')
                  )""",
            STRATEGY_TAG,
        )
    for r in rows:
        log.warning(_j("overnight_missing_sell_emergency_submit",
                       position_id=r["position_id"], symbol=r["symbol"]))
        trade, coid, _q = await broker.place_moo_sell(ib, r["symbol"], float(r["qty"]))
        if trade is None or coid is None:
            log.error(_j("overnight_emergency_moo_failed", position_id=r["position_id"]))
            continue
        order_id = await _insert_order(
            pool, r["position_id"], "SELL", coid,
            getattr(trade.order, "orderId", None), "submitted",
        )
        _LIVE_TRADES[order_id] = trade


# ── Top-level tick entry point ───────────────────────────────────────────────

async def run(pool: asyncpg.Pool, ib: IB, cfg: dict) -> None:
    """Called from main.Bot.tick(). Safe to call every tick: all internal
    phases early-return when not applicable. Exceptions are logged and
    swallowed — this must never crash the bot's tick loop."""
    enabled, _reason = _is_enabled(cfg)
    if not enabled:
        return
    try:
        await maybe_scan(pool, ib, cfg)
    except Exception as exc:
        log.exception(_j("overnight_scan_error", err=str(exc)))
    try:
        await _monitor_buy_fills(pool, ib)
    except Exception as exc:
        log.exception(_j("overnight_buy_monitor_error", err=str(exc)))
    try:
        await maybe_exit_safety_check(pool, ib, cfg)
    except Exception as exc:
        log.exception(_j("overnight_exit_safety_error", err=str(exc)))
    try:
        await _monitor_sell_fills(pool, ib)
    except Exception as exc:
        log.exception(_j("overnight_sell_monitor_error", err=str(exc)))


# ── JSON log helper (mirrors main.py style) ──────────────────────────────────

def _j(msg: str, **fields) -> str:
    return json.dumps({"m": msg, **fields}, default=str)
