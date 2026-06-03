"""Position monitoring — exit/stop logic for both strategies.

Extracted from bot/strategy.py 2026-05-08 (was lines 656-973). Kept here as
a sibling module rather than `bot/strategy/monitor.py` so the existing call
sites (`from . import strategy; strategy.monitor_open_positions(...)`)
keep working via the re-export at the bottom of strategy.py.

Imports from `bot.strategy` for the shared helpers; one-way dependency
(this module is loaded after strategy.py reaches the bottom-of-file
re-export, by which point all referenced helpers are defined).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import asyncpg
from ib_async import IB

from . import (
    broker, earnings, entry_price_guard, fees, fill_quality,
    hours, llm, notifications, regime_det, signals, sizing, snapshots,
)
from .strategies import constants as strat_const
from .universe import meta
from .strategy import (
    log,
    _j,
    _cross_up,
    _cross_down,
    _close_position,
    _update_position_price,
    _update_position_stop,
    _update_position_target,
    _record_order,
    _slot_profiles,
    _log_signal,
    _COOLDOWN_SECONDS_BY_STRATEGY,
)


async def monitor_open_positions(pool, ib: IB, cfg: dict) -> None:
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT id, symbol, slot, entry_price, qty, target_price, stop_price,
                      current_price, sector, company_name, opened_at
               FROM positions WHERE status IN ('open','closing') AND target_price IS NOT NULL"""
        )
    profiles = await _slot_profiles(pool)
    llm_enabled = cfg.get("LLM_VETO_ENABLED") is True

    for r in rows:
        sym = r["symbol"]
        m = meta(sym)
        price = await broker.latest_trade_price(ib, sym)
        if price is None:
            log.warning(_j("no_price", symbol=sym))
            continue
        await _update_position_price(pool, r["id"], price)








        try:
            ibkr_pos_list = ib.positions()
            if not ibkr_pos_list:


                log.warning(_j("pre_sell_skipped_ibkr_empty",
                                 position_id=r["id"], symbol=sym))

            else:
                ibkr_actual = 0.0
                for p_pos in ibkr_pos_list:
                    if p_pos.contract.symbol == sym and abs(float(p_pos.position)) > 1e-9:
                        ibkr_actual = float(p_pos.position)
                        break
                db_qty_signed = float(r["qty"] or 0)
                long_db_flat_broker = db_qty_signed > 0 and ibkr_actual <= 0
                short_db_flat_broker = db_qty_signed < 0 and ibkr_actual >= 0
                if long_db_flat_broker or short_db_flat_broker:
                    log.warning(_j("pre_sell_abort_ibkr_mismatch",
                                     position_id=r["id"], symbol=sym,
                                     db_qty=db_qty_signed,
                                     ibkr_qty=ibkr_actual))

                    await _close_position(pool, r["id"], price)
                    continue
        except Exception as exc:
            log.warning(_j("pre_sell_ib_check_failed", err=str(exc)))

            continue

        entry = float(r["entry_price"])
        qty = float(r["qty"])





        if qty <= 0:
            log.warning(_j("position_qty_non_positive_closed", position_id=r["id"], symbol=sym, qty=qty))
            await _close_position(pool, r["id"], price)
            continue
        target = float(r["target_price"])
        stop = float(r["stop_price"])
        prof = profiles.get(r["slot"], {})


        opened_at = r["opened_at"].astimezone(timezone.utc)
        held_sec = (datetime.now(timezone.utc) - opened_at).total_seconds()
        max_sec = int(prof.get("max_hold_seconds") or 10 * 86400)




        tiered = bool(cfg.get("TIERED_TIME_STOP_ENABLED"))
        if tiered and max_sec > 0:
            pnl_per_share = price - entry
            stop_distance = max(entry - stop, 0.0)
            frac = held_sec / max_sec
            if frac >= 0.75 and stop_distance > 0 and \
               pnl_per_share <= -0.3 * stop_distance:
                log.info(_j("time_stop_underwater", symbol=sym,
                              held_sec=int(held_sec), max_sec=max_sec,
                              pnl_per_share=round(pnl_per_share, 4),
                              stop_distance=round(stop_distance, 4)))
                held_sec = max_sec
            elif frac >= 0.5 and frac < 1.0:
                log.info(_j("time_stop_warning", symbol=sym,
                              held_sec=int(held_sec), max_sec=max_sec,
                              frac=round(frac, 3),
                              pnl_per_share=round(pnl_per_share, 4)))

        if held_sec >= max_sec:
            if not hours.market_open_for_symbol(sym):
                log.info(_j("time_stop_deferred_market_closed", symbol=sym, held_sec=int(held_sec)))
                continue
            log.info(_j("time_stop", symbol=sym, held_sec=int(held_sec), max_sec=max_sec))




            mtc = hours.minutes_to_close_for_symbol(sym)
            moc_min, moc_max = hours.moc_window_for_currency(m.currency, cfg)
            use_moc = (mtc is not None and moc_min <= mtc <= moc_max
                       and m.asset_class != "crypto"
                       and prof.get("strategy") == "intraday")
            if use_moc:
                log.info(_j("time_stop_route_moc", symbol=sym, minutes_to_close=mtc))
                trade, coid, quote = await broker.place_moc_sell(ib, sym, qty)
            else:
                trade, coid, quote = await broker.place_limit_sell(ib, sym, qty, price - _cross_down(m, price))
            if trade is None:
                continue
            ts_timeout = 30 if m.asset_class == "crypto" else 90
            status = await broker.wait_for_fill_or_cancel(trade, timeout_sec=ts_timeout, ib=ib)
            fill_price = trade.orderStatus.avgFillPrice or price
            real_filled_qty = float(trade.orderStatus.filled or 0)



            did_fill = real_filled_qty > 0 and fill_price and fill_price > 0
            fee = fees.estimate_side("SELL", real_filled_qty if did_fill else qty,
                                       fill_price, m.currency, m.asset_class).total
            await _record_order(
                pool, r["id"], "SELL",
                "filled" if did_fill else "cancelled",
                getattr(trade.order, "orderId", None),
                float(trade.order.lmtPrice) if trade and trade.order else None,
                float(fill_price) if fill_price else None,
                real_filled_qty if did_fill else 0.0,
                fee if did_fill else 0.0,
                {"status": status, "reason": "time_stop"},
                client_order_id=coid,
                quote=quote,
                paper=(cfg.get("TRADING_MODE") == "paper"),
            )
            if did_fill:
                await _close_position(pool, r["id"], float(fill_price))
                try:
                    pnl = (float(fill_price) - entry) * float(real_filled_qty)
                    await notifications.notify_trade_fill(
                        symbol=sym, side="SELL", qty=float(real_filled_qty),
                        fill_price=float(fill_price), pnl=round(pnl, 2),
                        slot=r["slot"], reason="time_stop",
                        paper=(cfg.get("TRADING_MODE") == "paper"),
                    )
                except Exception:
                    pass
            continue






        near_stop = price <= stop * 1.01 and price > stop
        if near_stop and llm_enabled:
            advice = await llm.stop_adjust(sym, r["company_name"] or sym, entry=entry, current=price, stop=stop)
            if isinstance(advice, dict):
                action = advice.get("action")
                stop_before = stop
                stop_after = stop
                if action == "tighten":
                    new_stop = max(stop, price * 0.995)
                    await _update_position_stop(pool, r["id"], new_stop)
                    stop = new_stop
                    stop_after = new_stop
                elif action == "exit_now":
                    target = price
                legacy_widen = bool(advice.get("legacy_widen"))
                async with pool.acquire() as c:
                    await c.execute(
                        """INSERT INTO stop_adjust_decisions
                           (position_id, symbol, entry_price, current_price,
                            stop_before, stop_after, action, new_stop_pct,
                            confidence, reasoning, legacy_widen_action,
                            raw_response)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb)""",
                        r["id"], sym, entry, price, stop_before, stop_after,
                        action, advice.get("new_stop_pct"),
                        advice.get("confidence"), advice.get("reasoning"),
                        legacy_widen, json.dumps(advice, default=str),
                    )

        at_target = price >= target
        at_stop = price <= stop
        if not (at_target or at_stop):
            continue


        partial_tp_enabled = cfg.get("PARTIAL_TP_ENABLED") is True
        if at_target and partial_tp_enabled and qty > 1:

            async with pool.acquire() as c:
                partial_row = await c.fetchrow(
                    "SELECT 1 FROM orders WHERE position_id=$1 AND side='SELL' AND status='filled' AND raw->>'partial_tp' = 'true' LIMIT 1",
                    r["id"],
                )
            if partial_row is None:
                half_qty = qty / 2.0
                if m.asset_class == "crypto":
                    half_qty = round(half_qty, 6)
                else:
                    half_qty = float(int(half_qty)) if half_qty >= 1 else qty
                if half_qty > 0:
                    log.info(_j("partial_tp", symbol=sym, qty=half_qty, remaining=qty - half_qty, price=price))
                    trade, coid, quote = await broker.place_limit_sell(ib, sym, half_qty, price + _cross_up(m, price))
                    if trade is not None:
                        status = await broker.wait_for_fill_or_cancel(trade, timeout_sec=90, ib=ib)
                        fill_price = trade.orderStatus.avgFillPrice or price
                        real_filled_qty = float(trade.orderStatus.filled or 0)
                        did_fill = real_filled_qty > 0 and fill_price and fill_price > 0
                        fee = fees.estimate_side("SELL", real_filled_qty if did_fill else half_qty,
                                                   fill_price, m.currency, m.asset_class).total
                        await _record_order(
                            pool, r["id"], "SELL",
                            "filled" if did_fill else "cancelled",
                            getattr(trade.order, "orderId", None),
                            float(trade.order.lmtPrice) if trade and trade.order else None,
                            float(fill_price) if fill_price else None,
                            real_filled_qty if did_fill else 0.0,
                            fee if did_fill else 0.0,
                            {"status": status, "reason": "target_partial", "partial_tp": "true"},
                            client_order_id=coid,
                            quote=quote,
                            paper=(cfg.get("TRADING_MODE") == "paper"),
                        )
                        if did_fill:
                            new_qty = qty - real_filled_qty
                            await _update_position_stop(pool, r["id"], entry)
                            async with pool.acquire() as c:
                                await c.execute(
                                    "UPDATE positions SET qty=$1, stop_price=$2 WHERE id=$3",
                                    new_qty, entry, r["id"],
                                )
                            log.info(_j("partial_tp_done", symbol=sym, sold=real_filled_qty, remaining=new_qty, stop=entry))
                            try:
                                pnl = (price - entry) * float(real_filled_qty)
                                await notifications.notify_trade_fill(
                                    symbol=sym, side="SELL", qty=float(real_filled_qty),
                                    fill_price=price, pnl=round(pnl, 2),
                                    slot=r["slot"], reason="partial_tp",
                                    paper=(cfg.get("TRADING_MODE") == "paper"),
                                )
                            except Exception:
                                pass
                            continue

        if at_target and llm_enabled:
            advice = await llm.exit_veto(
                sym, r["company_name"] or sym,
                entry=entry, current=price, target=target,
                held_days=int(held_sec // 86400),
            )
            if isinstance(advice, dict):
                action = advice.get("action")
                if action == "hold":
                    extra = float(advice.get("extra_target_pct") or 0)
                    if extra > 0:
                        new_target = entry * (1 + (prof.get("target_profit_pct", 3.0) + extra) / 100.0)
                        await _update_position_target(pool, r["id"], new_target)
                    continue
                if action == "tighten":
                    await _update_position_stop(pool, r["id"], entry)
                    continue

        rt_fees = fees.round_trip(qty, entry, price, m.currency, m.asset_class)
        net = (price - entry) * qty - rt_fees
        if at_target and net < float(prof.get("min_net_margin_eur", 0.5)):
            continue

        if not hours.market_open_for_symbol(sym):
            continue





        if at_stop:
            trade, coid, quote = await broker.place_market_sell(ib, sym, qty)
            fill_timeout = 30
        else:
            trade, coid, quote = await broker.place_limit_sell(ib, sym, qty, price + _cross_up(m, price))
            fill_timeout = 90
        if trade is None:
            continue
        status = await broker.wait_for_fill_or_cancel(trade, timeout_sec=fill_timeout, ib=ib)
        fill_price = trade.orderStatus.avgFillPrice or price
        real_filled_qty = float(trade.orderStatus.filled or 0)
        did_fill = real_filled_qty > 0 and fill_price and fill_price > 0
        fee = fees.estimate_side("SELL", real_filled_qty if did_fill else qty,
                                   fill_price, m.currency, m.asset_class).total
        await _record_order(
            pool, r["id"], "SELL",
            "filled" if did_fill else "cancelled",
            getattr(trade.order, "orderId", None),
            float(getattr(trade.order, "lmtPrice", 0)) if trade and trade.order else None,
            float(fill_price) if fill_price else None,
            real_filled_qty if did_fill else 0.0,
            fee if did_fill else 0.0,
            {"status": status, "reason": "target" if at_target else "stop",
             "exit_type": "mkt" if at_stop else "lmt"},
            client_order_id=coid,
            quote=quote,
            paper=(cfg.get("TRADING_MODE") == "paper"),
        )
        if did_fill:
            await _close_position(pool, r["id"], float(fill_price))
            try:
                pnl = (float(fill_price) - entry) * float(real_filled_qty)
                await notifications.notify_trade_fill(
                    symbol=sym, side="SELL", qty=float(real_filled_qty),
                    fill_price=float(fill_price), pnl=round(pnl, 2),
                    slot=r["slot"], reason="target" if at_target else "stop",
                    paper=(cfg.get("TRADING_MODE") == "paper"),
                )
            except Exception:
                pass


