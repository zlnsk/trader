"""Offline backtest harness for the swing strategy.

Replays IBKR daily bars through the live signal engine + slot-based portfolio
logic. No LLM, no live orders. Outputs a per-trade CSV and a summary JSON.

Usage:
    python -m bot.backtest [--lookback-days 400] [--currency USD]
                           [--slots 3] [--slot-size 100]
                           [--rsi-max 30] [--sigma-min 1.5] [--score-min 50]
                           [--target-pct 0.03] [--stop-pct -0.05]
                           [--max-hold-days 10]
                           [--out-dir ./backtests/TAG]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from pathlib import Path

from ib_async import IB, Stock

from . import fees, signals
from .universe import UNIVERSE_META, meta


@dataclass
class Bar:
    d: date
    open: float
    high: float
    low: float
    close: float


@dataclass
class Position:
    symbol: str
    slot: int
    entry_date: date
    entry_price: float
    qty: float
    target_price: float
    stop_price: float

    def exit_check(self, bar: Bar, max_hold_days: int) -> tuple[str, float] | None:
        """Return (reason, fill_price) if this bar triggers an exit, else None.
        Only the bar's close is used (daily bars) — deliberately pessimistic
        vs. intra-day fills. Time-stop handled by caller."""
        if bar.close >= self.target_price:
            return ("target", bar.close)
        if bar.close <= self.stop_price:
            return ("stop", bar.close)
        return None


@dataclass
class Trade:
    symbol: str
    slot: int
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    qty: float
    currency: str
    reason: str
    days_held: int
    gross_pnl: float
    fees: float
    net_pnl: float
    net_pct: float


async def fetch_bars(ib: IB, symbol: str, lookback_days: int) -> list[Bar] | None:
    m = meta(symbol)
    c = Stock(symbol, "SMART", m.currency, primaryExchange=m.primary_exchange or "")
    try:
        await asyncio.wait_for(ib.qualifyContractsAsync(c), timeout=15)
    except Exception as e:
        print(f"  qualify fail {symbol}: {e}", file=sys.stderr)
        return None
    # IBKR paper pacing: >365-day bars need "1 Y"/"2 Y" durationStr, not "N D".
    if lookback_days > 365:
        years = max(1, (lookback_days + 364) // 365)
        duration = f"{years} Y"
    else:
        duration = f"{lookback_days} D"
    for attempt in range(3):
        try:
            bars = await asyncio.wait_for(
                ib.reqHistoricalDataAsync(
                    c,
                    endDateTime="",
                    durationStr=duration,
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                ),
                timeout=60,
            )
            break
        except asyncio.TimeoutError:
            if attempt == 2:
                print(f"  timeout {symbol} after 3 attempts", file=sys.stderr)
                return None
            await asyncio.sleep(2 + attempt * 3)
        except Exception as e:
            print(f"  bars fail {symbol}: {e}", file=sys.stderr)
            return None
    # IBKR historical pacing: 60 req / 10 min. Throttle 0.3 s between symbols.
    await asyncio.sleep(0.3)
    out: list[Bar] = []
    for b in bars or []:
        if not (b.close and b.close > 0):
            continue
        d = b.date if isinstance(b.date, date) else b.date.date()
        out.append(Bar(d=d, open=b.open, high=b.high, low=b.low, close=b.close))
    return out or None


def simulate(
    history: dict[str, list[Bar]],
    slots: int,
    slot_size: float,
    rsi_max: float,
    sigma_min: float,
    score_min: float,
    target_pct: float,
    stop_pct: float,
    max_hold_days: int,
    rsi_period: int = 14,
) -> list[Trade]:
    """Walk chronologically through the intersection of bar dates, scan daily
    for entries, apply slot/target/stop/time rules, record trades.

    Rec #4: validates against at least 90 days of historical data to avoid
    over-fitting to the last week's market regime."""
    by_symbol: dict[str, dict[date, Bar]] = {s: {b.d: b for b in bars} for s, bars in history.items()}
    all_dates = sorted({b.d for bars in history.values() for b in bars})
    if not all_dates:
        return []

    open_positions: list[Position] = []
    trades: list[Trade] = []

    warmup = max(rsi_period + 1, 20)
    if len(all_dates) < 90:
        print(f"WARNING: only {len(all_dates)} trading days in history — "
              f"rec #4 recommends ≥90 days to avoid over-fitting.", file=sys.stderr)
    for i, day in enumerate(all_dates):
        if i < warmup:
            continue

        # --- EXITS first ---
        still_open: list[Position] = []
        for pos in open_positions:
            bar = by_symbol[pos.symbol].get(day)
            if bar is None:
                still_open.append(pos)
                continue

            held_days = (day - pos.entry_date).days
            exit_reason: str | None = None
            fill: float | None = None
            ex = pos.exit_check(bar, max_hold_days)
            if ex is not None:
                exit_reason, fill = ex
            elif held_days >= max_hold_days:
                exit_reason, fill = "time", bar.close

            if exit_reason and fill is not None:
                m = meta(pos.symbol)
                rt = fees.round_trip(pos.qty, pos.entry_price, fill, m.currency)
                gross = (fill - pos.entry_price) * pos.qty
                net = gross - rt
                cost_basis = pos.entry_price * pos.qty
                trades.append(Trade(
                    symbol=pos.symbol, slot=pos.slot,
                    entry_date=pos.entry_date, entry_price=pos.entry_price,
                    exit_date=day, exit_price=fill,
                    qty=pos.qty, currency=m.currency, reason=exit_reason,
                    days_held=held_days,
                    gross_pnl=round(gross, 4),
                    fees=round(rt, 4),
                    net_pnl=round(net, 4),
                    net_pct=round(net / cost_basis * 100.0, 3) if cost_basis else 0.0,
                ))
            else:
                still_open.append(pos)
        open_positions = still_open

        # --- ENTRIES ---
        free = slots - len(open_positions)
        if free <= 0:
            continue
        held_syms = {p.symbol for p in open_positions}

        candidates: list[tuple[float, str, dict, Bar]] = []
        for sym, bars_map in by_symbol.items():
            if sym in held_syms:
                continue
            bar = bars_map.get(day)
            if bar is None:
                continue
            # closes up to and including today
            closes = [b.close for b in history[sym] if b.d <= day]
            if len(closes) < warmup:
                continue
            s, payload = signals.score(closes, rsi_period=rsi_period)
            if s is None or s < score_min:
                continue
            if payload["rsi"] > rsi_max:
                continue
            if payload["sigma_below_sma20"] < sigma_min:
                continue
            candidates.append((s, sym, payload, bar))

        candidates.sort(key=lambda t: -t[0])
        for s, sym, payload, bar in candidates[:free]:
            m = meta(sym)
            qty = max(1.0, slot_size / bar.close)
            qty = round(qty)
            if qty < 1:
                continue
            target = round(bar.close * (1 + target_pct), 4)
            stop = round(bar.close * (1 + stop_pct), 4)
            open_positions.append(Position(
                symbol=sym, slot=(len(open_positions) % slots) + 1,
                entry_date=day, entry_price=bar.close, qty=qty,
                target_price=target, stop_price=stop,
            ))

    # Force-close anything still open at the last bar (mark-to-market)
    if open_positions:
        last_day = all_dates[-1]
        for pos in open_positions:
            bar = by_symbol[pos.symbol].get(last_day)
            if bar is None:
                continue
            m = meta(pos.symbol)
            rt = fees.round_trip(pos.qty, pos.entry_price, bar.close, m.currency)
            gross = (bar.close - pos.entry_price) * pos.qty
            net = gross - rt
            cost_basis = pos.entry_price * pos.qty
            trades.append(Trade(
                symbol=pos.symbol, slot=pos.slot,
                entry_date=pos.entry_date, entry_price=pos.entry_price,
                exit_date=last_day, exit_price=bar.close,
                qty=pos.qty, currency=m.currency, reason="mtm",
                days_held=(last_day - pos.entry_date).days,
                gross_pnl=round(gross, 4),
                fees=round(rt, 4),
                net_pnl=round(net, 4),
                net_pct=round(net / cost_basis * 100.0, 3) if cost_basis else 0.0,
            ))
    return trades


def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"n_trades": 0}
    by_ccy: dict[str, list[Trade]] = {}
    for t in trades:
        by_ccy.setdefault(t.currency, []).append(t)

    def bucket(ts: list[Trade]) -> dict:
        wins = [t for t in ts if t.net_pnl > 0]
        losses = [t for t in ts if t.net_pnl <= 0]
        pcts = [t.net_pct for t in ts]
        days = [t.days_held for t in ts]
        reasons: dict[str, int] = {}
        for t in ts:
            reasons[t.reason] = reasons.get(t.reason, 0) + 1
        total_net = sum(t.net_pnl for t in ts)
        return {
            "n_trades": len(ts),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(ts) * 100, 2),
            "total_net_pnl": round(total_net, 2),
            "avg_net_pct": round(statistics.mean(pcts), 3),
            "median_net_pct": round(statistics.median(pcts), 3),
            "stdev_net_pct": round(statistics.pstdev(pcts), 3) if len(pcts) > 1 else 0.0,
            "best_trade_pct": round(max(pcts), 3),
            "worst_trade_pct": round(min(pcts), 3),
            "avg_days_held": round(statistics.mean(days), 2),
            "exit_reasons": reasons,
        }

    return {
        "n_trades": len(trades),
        "by_currency": {ccy: bucket(ts) for ccy, ts in by_ccy.items()},
        "overall": bucket(trades),
    }


async def run(args) -> int:
    if args.currency == "ALL":
        syms = list(UNIVERSE_META.keys())
    else:
        syms = [s for s, m in UNIVERSE_META.items() if m.currency == args.currency]
    if not syms:
        print(f"No symbols matched currency={args.currency}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ib = IB()
    await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=30)
    print(f"Connected to IB {args.host}:{args.port} clientId={args.client_id}")

    t0 = time.time()
    history: dict[str, list[Bar]] = {}
    try:
        for idx, sym in enumerate(syms, 1):
            print(f"[{idx}/{len(syms)}] {sym} ...", end="", flush=True)
            bars = await fetch_bars(ib, sym, args.lookback_days)
            if not bars:
                print(" skip")
                continue
            history[sym] = bars
            print(f" {len(bars)} bars ({bars[0].d} → {bars[-1].d})")
    finally:
        ib.disconnect()

    fetch_secs = time.time() - t0
    print(f"Fetched {len(history)} symbols in {fetch_secs:.1f}s")

    trades = simulate(
        history=history,
        slots=args.slots,
        slot_size=args.slot_size,
        rsi_max=args.rsi_max,
        sigma_min=args.sigma_min,
        score_min=args.score_min,
        target_pct=args.target_pct,
        stop_pct=args.stop_pct,
        max_hold_days=args.max_hold_days,
    )

    trades_path = out_dir / "trades.csv"
    with trades_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(Trade(
            symbol="", slot=0, entry_date=date.today(), entry_price=0.0,
            exit_date=date.today(), exit_price=0.0, qty=0.0, currency="",
            reason="", days_held=0, gross_pnl=0.0, fees=0.0,
            net_pnl=0.0, net_pct=0.0,
        )).keys()))
        w.writeheader()
        for t in trades:
            row = asdict(t)
            row["entry_date"] = t.entry_date.isoformat()
            row["exit_date"] = t.exit_date.isoformat()
            w.writerow(row)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {k: getattr(args, k) for k in (
            "lookback_days", "currency", "slots", "slot_size",
            "rsi_max", "sigma_min", "score_min", "target_pct",
            "stop_pct", "max_hold_days")},
        "universe_size": len(history),
        "fetch_seconds": round(fetch_secs, 1),
        **summarize(trades),
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))

    print(f"Wrote {trades_path} + {summary_path}")
    print(json.dumps(summary.get("overall", {"n_trades": 0}), indent=2))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.getenv("IB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("IB_PORT", "4002")))
    ap.add_argument("--client-id", type=int, default=99)
    ap.add_argument("--lookback-days", type=int, default=400)
    ap.add_argument("--currency", default="USD",
                    help="USD | EUR | GBP | CHF | DKK | ALL")
    ap.add_argument("--slots", type=int, default=3)
    ap.add_argument("--slot-size", type=float, default=100.0)
    ap.add_argument("--rsi-max", type=float, default=30.0)
    ap.add_argument("--sigma-min", type=float, default=1.5)
    ap.add_argument("--score-min", type=float, default=50.0)
    ap.add_argument("--target-pct", type=float, default=0.03)
    ap.add_argument("--stop-pct", type=float, default=-0.05)
    ap.add_argument("--max-hold-days", type=int, default=10)
    ap.add_argument("--out-dir", default="./backtests/last")
    args = ap.parse_args()
    rc = asyncio.run(run(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
