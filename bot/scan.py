"""Universe scan + entry execution.

Extracted from bot/strategy.py 2026-05-08 (was lines 974-1693, ~720 lines).
Holds `_execute_buy` and `_scan_for_strategy`. Same one-way-import
pattern as monitor.py: imports shared helpers from `bot.strategy`.
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
    _bad_fill,
    _compute_stop,
    _gross_notional_eur,
    _gross_risk_pct,
    _insert_position,
    _log_signal,
    _open_sector_counts,
    _pending_slots,
    _record_order,
    _slot_profiles,
    _slots_in_use,
    _symbol_held_or_pending,
    _tied_up_symbols,
    current_regime,
    _ATR_MULT_DEFAULT,
    _COOLDOWN_SECONDS_BY_STRATEGY,
)


async def _execute_buy(pool, ib: IB, sym: str, slot: int, price: float,
                       qty: float, target: float, stop: float, m,
                       source_reason: str, s_score: float, payload: dict,
                       verdict: dict, cfg: dict) -> None:
    limit_price = price + _cross_up(m, price)
    is_fractional = abs(qty - round(qty)) > 0.0001
    use_bracket = (
        cfg.get("BRACKET_ORDER_ENABLED") is True
        and m.asset_class != "crypto"
        and not cfg.get("MANUAL_APPROVAL_MODE")
        and not is_fractional
    )
    if use_bracket:
        trades, coid, quote = await broker.place_bracket_buy(
            ib, sym, qty, limit_price, target, stop,
        )
        if not trades:
            await _log_signal(pool, sym, s_score, payload, verdict, "skip", "bracket order place failed", slot=slot)
            return
        parent_trade = trades[0]
        submitted_price = float(parent_trade.order.lmtPrice) if parent_trade and parent_trade.order else limit_price
        pre_order = await _record_order(
            pool, None, "BUY", "submitted",
            getattr(parent_trade.order, "orderId", None),
            submitted_price, None, None, None,
            {"symbol": sym, "qty": qty, "slot": slot, "source": source_reason, "bracket": True},
            client_order_id=coid,
            quote=quote,
            paper=(cfg.get("TRADING_MODE") == "paper"),
        )
        buy_timeout = 90
        status = await broker.wait_for_fill_or_cancel(parent_trade, timeout_sec=buy_timeout, ib=ib)
        fill_price = parent_trade.orderStatus.avgFillPrice or 0
        fill_qty = parent_trade.orderStatus.filled or 0
        if fill_qty > 0 and fill_price and fill_price > 0 and price > 0:
            fee = fees.estimate_side("BUY", fill_qty, fill_price, m.currency, m.asset_class).total
            pid = await _insert_position(
                pool, sym, slot, float(fill_price), float(fill_qty),
                target_price=target,
                stop_price=stop,
                current_price=float(fill_price),
                sector=m.sector, company_name=m.name,
            )

            for child in trades[1:]:
                if child and child.order:
                    await _record_order(
                        pool, pid, "SELL", "submitted",
                        getattr(child.order, "orderId", None),
                        None, None, None, None,
                        {"bracket_child": True, "parent_coid": coid},
                        client_order_id=getattr(child.order, "orderRef", None),
                        quote=fill_quality.Quote(),
                        paper=(cfg.get("TRADING_MODE") == "paper"),
                    )
            try:
                if m.asset_class == "crypto":
                    hist = await broker.get_intraday_closes(ib, sym, bar_size="1 min", duration="1 D")
                    interval_kind = "mins"
                else:
                    hist = await broker.get_daily_closes(ib, sym, lookback_days=35)
                    interval_kind = "days"
                if hist and hist.closes:
                    tail = hist.closes[-30:]
                    async with pool.acquire() as c:
                        for i, px in enumerate(tail):
                            await c.execute(
                                f"INSERT INTO position_price_ticks (position_id, ts, price) VALUES ($1, now() - make_interval({interval_kind} => $2), $3)",
                                pid, len(tail) - i, float(px),
                            )
            except Exception as exc:
                log.warning(_j("bootstrap_ticks_failed", symbol=sym, err=str(exc)))
            async with pool.acquire() as c:
                await c.execute(
                    """UPDATE orders SET position_id=$1, status='filled',
                       fill_price=$2, fill_qty=$3, fees=$4,
                       slippage_bps = CASE
                         WHEN bid_at_submit IS NOT NULL AND ask_at_submit IS NOT NULL
                           THEN (($2 - (bid_at_submit+ask_at_submit)/2.0) /
                                 ((bid_at_submit+ask_at_submit)/2.0)) * 10000
                         ELSE slippage_bps END,
                       shadow_fill_price = CASE
                         WHEN spread_at_submit_bps IS NOT NULL
                           THEN $2 * (1 + spread_at_submit_bps * 0.5 / 10000)
                         ELSE shadow_fill_price END
                     WHERE id=$5""",
                    pid, float(fill_price), float(fill_qty), fee, pre_order,
                )
            await _log_signal(pool, sym, s_score, payload, verdict, "buy",
                              f"bracket filled at {fill_price:.2f}, qty {fill_qty}, slot {slot}", slot=slot)
            try:
                await notifications.notify_trade_fill(
                    symbol=sym, side="BUY", qty=float(fill_qty),
                    fill_price=float(fill_price), slot=slot,
                    paper=(cfg.get("TRADING_MODE") == "paper"),
                )
            except Exception:
                pass
        else:
            bad_fill = _bad_fill(status, fill_qty, fill_price, price)
            if bad_fill:
                log.error(_j("buy_fill_inconsistent", symbol=sym, slot=slot,
                             status=status, fill_price=float(fill_price or 0),
                             fill_qty=float(fill_qty or 0), ref_price=float(price or 0),
                             pre_order_id=pre_order))
            final_status = "rejected" if bad_fill else "cancelled"
            async with pool.acquire() as c:
                await c.execute("UPDATE orders SET status=$2 WHERE id=$1", pre_order, final_status)
            await _log_signal(pool, sym, s_score, payload, verdict, "skip",
                              f"bracket not filled: {status}" + (" (bad fill data)" if bad_fill else ""),
                              slot=slot)
        return


    trade, coid, quote = await broker.place_limit_buy(ib, sym, qty, limit_price)
    if trade is None:
        await _log_signal(pool, sym, s_score, payload, verdict, "skip", "order place failed", slot=slot)
        return

    submitted_price = float(trade.order.lmtPrice) if trade and trade.order else limit_price
    pre_order = await _record_order(
        pool, None, "BUY", "submitted",
        getattr(trade.order, "orderId", None),
        submitted_price, None, None, None,
        {"symbol": sym, "qty": qty, "slot": slot, "source": source_reason},
        client_order_id=coid,
        quote=quote,
        paper=(cfg.get("TRADING_MODE") == "paper"),
    )


    buy_timeout = 15 if m.asset_class == "crypto" else 90
    status = await broker.wait_for_fill_or_cancel(trade, timeout_sec=buy_timeout, ib=ib)
    fill_price = trade.orderStatus.avgFillPrice or 0
    fill_qty = trade.orderStatus.filled or 0





    if fill_qty > 0 and fill_price and fill_price > 0 and price > 0:
        fee = fees.estimate_side("BUY", fill_qty, fill_price, m.currency, m.asset_class).total
        pid = await _insert_position(
            pool, sym, slot, float(fill_price), float(fill_qty),
            target_price=float(fill_price) * target / price,
            stop_price=float(fill_price) * stop / price,
            current_price=float(fill_price),
            sector=m.sector, company_name=m.name,
        )



        try:



            if m.asset_class == "crypto":
                hist = await broker.get_intraday_closes(
                    ib, sym, bar_size="1 min", duration="1 D",
                )
                interval_kind = "mins"
            else:
                hist = await broker.get_daily_closes(ib, sym, lookback_days=35)
                interval_kind = "days"
            if hist and hist.closes:
                tail = hist.closes[-30:]
                async with pool.acquire() as c:
                    for i, px in enumerate(tail):
                        await c.execute(
                            f"""INSERT INTO position_price_ticks (position_id, ts, price)
                                VALUES ($1, now() - make_interval({interval_kind} => $2), $3)""",
                            pid, len(tail) - i, float(px),
                        )
        except Exception as exc:
            log.warning(_j("bootstrap_ticks_failed", symbol=sym, err=str(exc)))
        async with pool.acquire() as c:
            await c.execute(
                """UPDATE orders SET position_id=$1, status='filled',
                   fill_price=$2, fill_qty=$3, fees=$4,
                   slippage_bps = CASE
                     WHEN bid_at_submit IS NOT NULL AND ask_at_submit IS NOT NULL
                       THEN (($2 - (bid_at_submit+ask_at_submit)/2.0) /
                             ((bid_at_submit+ask_at_submit)/2.0)) * 10000
                     ELSE slippage_bps END,
                   shadow_fill_price = CASE
                     WHEN spread_at_submit_bps IS NOT NULL
                       THEN $2 * (1 + spread_at_submit_bps * 0.5 / 10000)
                     ELSE shadow_fill_price END
                 WHERE id=$5""",
                pid, float(fill_price), float(fill_qty), fee, pre_order,
            )
        await _log_signal(pool, sym, s_score, payload, verdict, "buy",
                          f"filled at {fill_price:.2f}, qty {fill_qty}, slot {slot}", slot=slot)
        try:
            await notifications.notify_trade_fill(
                symbol=sym, side="BUY", qty=float(fill_qty),
                fill_price=float(fill_price), slot=slot,
                paper=(cfg.get("TRADING_MODE") == "paper"),
            )
        except Exception:
            pass
    else:
        bad_fill = _bad_fill(status, fill_qty, fill_price, price)
        if bad_fill:
            log.error(_j("buy_fill_inconsistent", symbol=sym, slot=slot,
                         status=status, fill_price=float(fill_price or 0),
                         fill_qty=float(fill_qty or 0), ref_price=float(price or 0),
                         pre_order_id=pre_order))
        final_status = "rejected" if bad_fill else "cancelled"
        async with pool.acquire() as c:
            await c.execute("UPDATE orders SET status=$2 WHERE id=$1", pre_order, final_status)
        await _log_signal(pool, sym, s_score, payload, verdict, "skip",
                          f"order not filled: {status}" + (" (bad fill data)" if bad_fill else ""),
                          slot=slot)




async def _scan_for_strategy(pool, ib: IB, cfg: dict, strategy: str) -> None:
    """Scan + decision pipeline for a given strategy. Batch-fetches IB bars,
    applies RSI/σ/sector/trend filters, parallel LLM veto, ATR-aware stops,
    and per-sector concurrency cap."""
    universe: list[str] = list(cfg.get("UNIVERSE", []))
    if not universe:
        return
    profiles_all = await _slot_profiles(pool)
    profiles = {k: v for k, v in profiles_all.items() if v["strategy"] == strategy}
    if not profiles:
        return
    slot_size_eur = float(cfg.get("SLOT_SIZE_EUR", 1000))
    llm_enabled = cfg.get("LLM_VETO_ENABLED") is True
    manual_mode = cfg.get("MANUAL_APPROVAL_MODE") is True

    used = await _slots_in_use(pool)
    pending = await _pending_slots(pool)
    tied_up = used | pending
    free_slots = sorted(s for s in profiles.keys() if s not in tied_up)
    if not free_slots:
        return




    regime_source = (cfg.get("REGIME_SOURCE") or "hybrid").lower()
    universe_has_crypto = any(meta(s).asset_class == "crypto" for s in universe)
    universe_has_stock = any(meta(s).asset_class != "crypto" for s in universe)

    regime_stock = None
    regime_crypto = None
    if universe_has_stock and (llm_enabled or regime_source in {"deterministic", "hybrid"}):
        regime_stock = await current_regime(pool, ib=ib, cfg=cfg, asset_class="stock")
    if universe_has_crypto:
        regime_crypto = await current_regime(pool, ib=ib, cfg=cfg, asset_class="crypto")

    stock_off = bool(regime_stock and regime_stock.get("regime") == "risk_off")
    crypto_off = bool(regime_crypto and regime_crypto.get("regime") == "risk_off")
    if stock_off:
        log.info(_j("scan_paused_risk_off", strategy=strategy, asset_class="stock",
                    source=(regime_stock or {}).get("source"),
                    vol_z=(regime_stock or {}).get("realized_vol_z")))
    if crypto_off:
        log.info(_j("scan_paused_risk_off", strategy=strategy, asset_class="crypto",
                    source=(regime_crypto or {}).get("source"),
                    vol_z=(regime_crypto or {}).get("realized_vol_z")))
    if stock_off and crypto_off:
        return



    tied_symbols = await _tied_up_symbols(pool)
    sector_scope = str(cfg.get("MAX_POSITIONS_PER_SECTOR_SCOPE") or "portfolio").lower()
    sector_counts = await _open_sector_counts(
        pool, strategy=None if sector_scope == "portfolio" else strategy,
    )



    cooldown_symbols: set[str] = set()
    if cfg.get("REENTRY_COOLDOWN_ENABLED"):
        async with pool.acquire() as c:
            cd_rows = await c.fetch(
                """SELECT symbol FROM position_exits_cooldown
                    WHERE strategy=$1 AND cooldown_until_ts > NOW()""",
                strategy,
            )
        cooldown_symbols = {r["symbol"] for r in cd_rows}



    earnings_rows: list[dict] = []
    if cfg.get("EARNINGS_BLACKOUT_ENABLED"):
        async with pool.acquire() as c:
            e_rows = await c.fetch(
                """SELECT symbol, earnings_date FROM earnings_calendar
                    WHERE earnings_date >= CURRENT_DATE - INTERVAL '1 day'"""
            )
        earnings_rows = [dict(r) for r in e_rows]
    max_per_sector = int(cfg.get("MAX_POSITIONS_PER_SECTOR", 3) or 0)
    broker_concurrency = int(cfg.get("BROKER_CONCURRENCY", 8))
    volume_mult = float(cfg.get("VOLUME_CONFIRM_MULT", 1.2))
    trend_period = int(cfg.get("TREND_SMA_PERIOD", 200))
    trend_tol = float(cfg.get("TREND_TOLERANCE_PCT", -5.0))






    crypto_only_strategy = strategy == "crypto_scalp"
    scan_syms: list[str] = []
    scan_meta: dict[str, object] = {}
    for sym in universe:
        if sym in tied_symbols:
            continue
        if sym in cooldown_symbols:



            await _log_signal(pool, sym, None,
                                {"strategy": strategy,
                                 "reentry_cooldown": True},
                                None, "skip", "reentry_cooldown")
            continue
        m = meta(sym)
        if crypto_only_strategy and m.asset_class != "crypto":
            continue
        if m.asset_class == "crypto" and crypto_off:
            continue
        if m.asset_class != "crypto" and stock_off:
            continue
        if not hours.market_open_for_symbol(sym):
            continue
        if max_per_sector and sector_counts.get(m.sector, 0) >= max_per_sector:
            continue
        scan_syms.append(sym)
        scan_meta[sym] = m
    if not scan_syms:
        return


    if strategy == "crypto_scalp":


        hist_map = await broker.get_intraday_closes_many(
            ib, scan_syms, bar_size="1 min", duration="1 D",
            concurrency=broker_concurrency,
            ttl_sec=float(cfg.get("BAR_CACHE_TTL_CRYPTO_SEC", 20)),
        )
        rsi_period = 2
    elif strategy == "intraday":
        hist_map = await broker.get_intraday_closes_many(
            ib, scan_syms, bar_size="5 mins", duration="2 D",
            concurrency=broker_concurrency,
            ttl_sec=float(cfg.get("BAR_CACHE_TTL_INTRADAY_SEC", 45)),
        )
        rsi_period = 2

        daily_hist_map = await broker.get_daily_closes_many(
            ib, scan_syms, lookback_days=250,
            concurrency=broker_concurrency,
            ttl_sec=float(cfg.get("BAR_CACHE_TTL_SWING_SEC", 240)),
        )
    else:
        hist_map = await broker.get_daily_closes_many(
            ib, scan_syms,
            lookback_days=max(35, trend_period + 5),
            concurrency=broker_concurrency,
            ttl_sec=float(cfg.get("BAR_CACHE_TTL_SWING_SEC", 240)),
        )
        rsi_period = 14

    candidates: list[dict] = []
    for sym in scan_syms:
        hist = hist_map.get(sym)
        m = scan_meta[sym]
        if hist is None or len(hist.closes) < 20:
            await _log_signal(pool, sym, None, {"err": "no_bars", "strategy": strategy}, None, "skip", "no bars")
            continue
        closes_daily = None
        if strategy == "intraday":
            dh = daily_hist_map.get(sym)
            if dh is not None:
                closes_daily = dh.closes
        s, payload = signals.score(
            hist.closes, rsi_period=rsi_period,
            highs=hist.highs or None, lows=hist.lows or None,
            volumes=hist.volumes or None, volume_mult=volume_mult,
            closes_daily=closes_daily, strategy=strategy, cfg=cfg,
        )
        payload["strategy"] = strategy
        if s is None:
            await _log_signal(pool, sym, None, payload, None, "skip", "insufficient data")
            continue


        matching_slots: list[int] = []
        for slot in free_slots:
            p = profiles[slot]
            if s < p["quant_score_min"]:
                continue
            if payload.get("rsi", 100) > p["rsi_max"]:
                continue
            if payload.get("sigma_below_sma20", 0) < p["sigma_min"]:
                continue
            if p.get("sectors_allowed") and m.sector not in p["sectors_allowed"]:
                continue
            if p.get("trend_filter_enabled"):
                trend_reason = signals.apply_trend_filter(hist.closes, p, cfg,
                                                            trend_period, trend_tol)
                if trend_reason is not None:
                    payload["trend_filter_reason"] = trend_reason
                    continue
                payload["trend_ok"] = True
            ibs_reason = signals.apply_ibs_filter(p, payload, cfg)
            if ibs_reason is not None:
                payload["ibs_filter_reason"] = ibs_reason
                payload["ibs_gate_passed"] = False
                continue
            payload["ibs_gate_passed"] = True
            if cfg.get("EARNINGS_BLACKOUT_ENABLED"):
                earn_reason = earnings.apply_earnings_blackout(
                    p, sym, datetime.now(timezone.utc).date(),
                    earnings_rows, cfg,
                )
                if earn_reason is not None:
                    payload["earnings_blackout_reason"] = earn_reason
                    continue
            matching_slots.append(slot)
        if not matching_slots:
            await _log_signal(pool, sym, s, payload, None, "skip", "no slot matches filters")
            continue

        candidates.append({
            "symbol": sym, "score": s, "payload": payload,
            "name": m.name, "sector": m.sector, "currency": m.currency,
            "last_close": hist.last_close, "matching_slots": matching_slots,
            "rsi": payload.get("rsi"), "sigma": payload.get("sigma_below_sma20"),
            "gate_outcome": None,
        })

    if not candidates:
        return





    min_quant = float(cfg.get("MIN_QUANT_SCORE", 75) or 0)
    if min_quant > 0:
        before = len(candidates)
        candidates = [c for c in candidates if float(c.get("score") or 0) >= min_quant]
        skipped = before - len(candidates)
        if skipped:
            log.info(_j("min_quant_score_filter",
                          threshold=min_quant, dropped=skipped, kept=len(candidates)))
        if not candidates:
            return



    if entry_price_guard.is_entry_halted():
        log.error(_j("entry_halted_null_entry_circuit",
                       count=entry_price_guard.session_null_entry_count(),
                       threshold=entry_price_guard.NULL_ENTRY_HALT_THRESHOLD))
        return


    _stock_regime_label = (regime_stock or {}).get("regime") if regime_stock else None
    _crypto_regime_label = (regime_crypto or {}).get("regime") if regime_crypto else None


    if len(candidates) > 1 and llm_enabled:
        order = await llm.rank_candidates(candidates)
        if order:
            rank_map = {s: i for i, s in enumerate(order)}
            candidates.sort(key=lambda c: rank_map.get(c["symbol"], 1e9))

    if llm_enabled:
        llm_conc = int(cfg.get("LLM_CHECK_CONCURRENCY", 4))
        sem = asyncio.Semaphore(max(1, llm_conc))
        async def _check_one(cand: dict) -> dict:
            async with sem:
                v = await llm.check(cand["symbol"], cand["name"], cand["sector"], cand["payload"])
            return v if isinstance(v, dict) else {"verdict": "abstain", "reasoning": "llm error"}
        verdicts = await asyncio.gather(*[_check_one(c) for c in candidates])
        for cand, v in zip(candidates, verdicts):
            cand["_verdict"] = v
    else:
        for cand in candidates:
            cand["_verdict"] = {"verdict": "bypassed", "reasoning": "LLM_VETO_ENABLED=false"}


    for cand in candidates:
        if not free_slots:
            break
        chosen_slot = next((s for s in free_slots if s in cand["matching_slots"]), None)
        if chosen_slot is None:
            continue
        prof = profiles[chosen_slot]
        verdict = cand["_verdict"]

        if verdict.get("verdict") == "veto":
            cand["gate_outcome"] = "llm_veto"
            await _log_signal(pool, cand["symbol"], cand["score"], cand["payload"], verdict,
                              "skip", f"llm veto: {verdict.get('dive_cause','')}", slot=chosen_slot)
            continue



        abstain_passes = (strategy == "crypto_scalp" and not prof.get("llm_strict"))
        if verdict.get("verdict") == "abstain" and not abstain_passes:
            cand["gate_outcome"] = "llm_veto"
            await _log_signal(pool, cand["symbol"], cand["score"], cand["payload"], verdict,
                              "skip", "llm abstained", slot=chosen_slot)
            continue
        if prof.get("llm_strict") and verdict.get("verdict") != "allow":
            cand["gate_outcome"] = "llm_veto"
            await _log_signal(pool, cand["symbol"], cand["score"], cand["payload"], verdict,
                              "skip", "strict slot requires allow", slot=chosen_slot)
            continue



        min_llm_conf = float(cfg.get("MIN_LLM_CONFIDENCE", 0.6) or 0)
        if min_llm_conf > 0 and verdict.get("verdict") != "bypassed":
            try:
                conf = float(verdict.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf < min_llm_conf:
                cand["gate_outcome"] = "llm_low_confidence"
                await _log_signal(pool, cand["symbol"], cand["score"], cand["payload"], verdict,
                                  "skip",
                                  f"llm confidence {conf:.2f} < {min_llm_conf:.2f}",
                                  slot=chosen_slot)
                continue

        m = meta(cand["symbol"])

        if max_per_sector and sector_counts.get(m.sector, 0) >= max_per_sector:
            cand["gate_outcome"] = "sector_cap"
            await _log_signal(pool, cand["symbol"], cand["score"], cand["payload"], verdict,
                              "skip", f"sector cap {max_per_sector} hit for {m.sector}", slot=chosen_slot)
            continue

        price = cand["last_close"]

        target = price * (1 + prof["target_profit_pct"] / 100.0)



        min_width_pct = float(cfg.get("MIN_STOP_WIDTH_PCT", 0.75) or 0.0)
        stop, stop_source = _compute_stop(price, None, prof, cand["payload"],
                                             min_width_pct)
        cand["payload"]["stop_source"] = stop_source
        cand["payload"]["stop_mode"] = prof.get("stop_mode") or "pct"
        cand["payload"]["stop_distance_pct"] = round((price - stop) / price * 100, 3)


        size_mode = (cfg.get("POSITION_SIZE_MODE") or "fixed").lower()
        equity_eur = cfg.get("_equity_eur")
        risk_pct = float(cfg.get("POSITION_RISK_PCT", 0.5) or 0.5)


        sentiment = int((verdict or {}).get("sentiment_score") or 50)
        sentiment_mult = max(0.25, min(1.0, sentiment / 100.0))
        if cfg.get("LLM_SENTIMENT_SIZING_ENABLED") and sentiment_mult < 1.0:
            slot_size_eur = slot_size_eur * sentiment_mult
            cand["payload"]["sentiment_score"] = sentiment
            cand["payload"]["sentiment_mult"] = round(sentiment_mult, 3)
            log.info(_j("sentiment_sizing", symbol=cand["symbol"],
                        sentiment=sentiment, mult=round(sentiment_mult, 3)))






        max_gross_risk = float(cfg.get("MAX_GROSS_RISK_PCT", 6.0) or 0.0)
        size_multiplier = 1.0
        if max_gross_risk > 0 and equity_eur and equity_eur > 0:
            current_risk_pct = await _gross_risk_pct(pool, equity_eur)
            while current_risk_pct >= max_gross_risk and size_multiplier > 0.0625:
                size_multiplier *= 0.5


                current_risk_pct = current_risk_pct

        qty, size_src = sizing.compute_qty(
            size_mode, slot_size_eur * size_multiplier, price,
            stop_price=stop, equity_eur=equity_eur, risk_pct=risk_pct,
            asset_class=m.asset_class,
        )
        cand["payload"]["size_source"] = size_src
        if size_multiplier < 1.0:
            cand["payload"]["gross_risk_cap_factor"] = size_multiplier
            log.info(_j("gross_risk_cap_halved", symbol=cand["symbol"],
                          factor=size_multiplier,
                          max_gross_risk_pct=max_gross_risk))





        max_gross_notional_pct = float(cfg.get("MAX_GROSS_NOTIONAL_PCT", 0) or 0)
        if max_gross_notional_pct > 0 and equity_eur and equity_eur > 0:
            existing_notional = await _gross_notional_eur(pool)
            projected = existing_notional + (qty * price)
            cap_eur = equity_eur * (max_gross_notional_pct / 100.0)
            if projected > cap_eur:
                await _log_signal(pool, cand["symbol"], cand["score"], cand["payload"], verdict,
                                  "skip",
                                  f"gross_notional_cap: {projected:.0f} > {cap_eur:.0f} EUR ({max_gross_notional_pct:.0f}% of NetLiq)",
                                  slot=chosen_slot)




                try:
                    proposed = qty * price
                    util = (existing_notional / cap_eur * 100.0) if cap_eur > 0 else None
                    async with pool.acquire() as c:
                        await c.execute(
                            """INSERT INTO gross_notional_rejections
                               (symbol, proposed_notional_eur, existing_notional_eur,
                                cap_eur, net_liq_eur, cap_pct, utilization_pct,
                                slot, strategy)
                               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                            cand["symbol"], float(proposed), float(existing_notional),
                            float(cap_eur), float(equity_eur), float(max_gross_notional_pct),
                            float(util) if util is not None else None,
                            int(chosen_slot) if chosen_slot is not None else None,
                            strategy,
                        )
                except Exception as e:
                    log.warning(_j("gross_notional_rejection_persist_failed",
                                      symbol=cand["symbol"], error=str(e)))
                log.info(_j("gross_notional_cap_skip",
                              symbol=cand["symbol"],
                              existing=round(existing_notional, 2),
                              proposed=round(qty * price, 2),
                              cap=round(cap_eur, 2),
                              netliq=round(equity_eur, 2)))
                continue



        if m.asset_class != "crypto" and qty < 1:
            if price <= slot_size_eur * 2:
                qty = 1.0
            else:
                await _log_signal(pool, cand["symbol"], cand["score"], cand["payload"], verdict,
                                  "skip", f"1 share ({price:.2f}) > 2× slot", slot=chosen_slot)
                continue
        if m.asset_class == "crypto" and qty <= 0:
            await _log_signal(pool, cand["symbol"], cand["score"], cand["payload"], verdict,
                              "skip", "crypto qty rounded to 0", slot=chosen_slot)
            continue

        net = fees.net_expected(qty, price, target, m.currency, m.asset_class)
        if net < prof["min_net_margin_eur"]:
            cand["gate_outcome"] = "fee_check"
            await _log_signal(pool, cand["symbol"], cand["score"], cand["payload"], verdict,
                              "skip", f"net_expected {net:.2f} < min_margin", slot=chosen_slot)
            continue

        if not hours.market_open_for_symbol(sym):
            cand["gate_outcome"] = "market_closed"
            await _log_signal(pool, cand["symbol"], cand["score"], cand["payload"], verdict,
                              "skip", "market closed for symbol currency", slot=chosen_slot)
            continue

        if manual_mode:
            async with pool.acquire() as c:
                await c.execute(
                    """INSERT INTO pending_approvals
                       (symbol, slot, strategy, profile, quant_score, payload, llm_verdict,
                        price, qty, target_price, stop_price, currency)
                       VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9,$10,$11,$12)""",
                    cand["symbol"], chosen_slot, strategy, prof["profile"],
                    cand["score"], cand["payload"], verdict,
                    price, qty, target, stop, m.currency,
                )
            cand["gate_outcome"] = "manual_rejected"
            await _log_signal(pool, cand["symbol"], cand["score"], cand["payload"], verdict,
                              "skip", f"queued for manual approval (slot {chosen_slot})", slot=chosen_slot)
            free_slots.remove(chosen_slot)
            sector_counts[m.sector] = sector_counts.get(m.sector, 0) + 1
            continue

        cand["gate_outcome"] = "executed"
        cand["chosen_slot"] = chosen_slot
        await _execute_buy(pool, ib, cand["symbol"], chosen_slot, price, qty,
                           target, stop, m, "auto", cand["score"], cand["payload"], verdict, cfg)
        free_slots.remove(chosen_slot)
        sector_counts[m.sector] = sector_counts.get(m.sector, 0) + 1




    for _cand in candidates:
        outcome = _cand.get("gate_outcome") or "tied_up"
        try:
            row = snapshots.build_snapshot_row(
                symbol=_cand["symbol"],
                strategy=strategy,
                slot_id=_cand.get("chosen_slot") or (_cand["matching_slots"][0]
                                                       if _cand.get("matching_slots") else None),
                payload=_cand["payload"],
                gate_outcome=outcome,
                llm_verdict=(_cand.get("_verdict") or {}).get("verdict"),
                llm_dive_cause=(_cand.get("_verdict") or {}).get("dive_cause"),
                stock_regime=_stock_regime_label,
                crypto_regime=_crypto_regime_label,
            )
            await snapshots.insert_snapshot(pool, row)
        except Exception as exc:
            log.warning(_j("snapshot_insert_failed",
                            symbol=_cand.get("symbol"), err=str(exc)))

