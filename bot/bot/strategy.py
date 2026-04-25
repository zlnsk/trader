"""Strategy — swing + intraday, per-slot profiles, market-hours per region,
manual approval flow, LLM veto/regime/ranking/exit/stop."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import asyncpg
from ib_async import IB

from . import broker, earnings, fees, hours, llm, notifications, regime_det, signals, sizing, snapshots
from .strategies import constants as strat_const
from .universe import meta

log = logging.getLogger("bot.strategy")


def _j(msg: str, **fields) -> str:
    return json.dumps({"m": msg, **fields}, default=str)


def _cross_up(m, price: float) -> float:
    """Price offset to cross the book as a BUYER (or as a SELLER at target).
    Stocks: 5 bps with a 0.02 floor — scales across €1 to €4000 names so a large-cap
    like RMS (~€1688) actually crosses the book instead of resting at the bid.
    Crypto: 0.2% of price."""
    if m.asset_class == "crypto":
        return price * 0.002
    return max(0.02, price * 0.0005)


def _cross_down(m, price: float) -> float:
    """Price offset to cross the book as a SELLER (aggressive time-stop exit).
    Stocks: 10 bps with a 0.05 floor — same scaling reason as _cross_up.
    Crypto: 0.5% of price."""
    if m.asset_class == "crypto":
        return price * 0.005
    return max(0.05, price * 0.001)


# ── helpers ───────────────────────────────────────────────────────────────────

async def _ensure_initial_baseline(pool: asyncpg.Pool, net_liq_eur: float) -> None:
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT value FROM config WHERE key='INITIAL_NET_LIQ_EUR'")
        if row is None:
            await c.execute(
                """INSERT INTO config (key, value, updated_by)
                   VALUES ('INITIAL_NET_LIQ_EUR', $1::jsonb, 'bot:first-tick')""",
                net_liq_eur,
            )


async def _log_signal(pool, symbol, quant_score, payload, llm_verdict, decision, reason,
                       slot=None, strategy: str | None = None):
    # trend_filter_reason is sourced from payload so every caller sees it set
    # uniformly; keeps the function signature stable while still emitting the
    # dedicated column required by PR2.
    trend_filter_reason = None
    ibs_val = None
    ibs_gate_passed = None
    earnings_blackout_reason = None
    payload_strategy = None
    if isinstance(payload, dict):
        trend_filter_reason = payload.get("trend_filter_reason")
        ibs_val = payload.get("ibs")
        if "ibs_gate_passed" in payload:
            ibs_gate_passed = bool(payload["ibs_gate_passed"])
        earnings_blackout_reason = payload.get("earnings_blackout_reason")
        payload_strategy = payload.get("strategy")
    # Explicit strategy resolution: caller arg wins, then payload, then slot-based
    # fallback, then MEAN_REV. Previously leaned on DB DEFAULT 'mean_rev', which
    # silently mislabels any future strategy that reuses this helper.
    strategy_final = strategy or payload_strategy or strat_const.for_slot(slot)
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO signals (symbol, quant_score, payload, llm_verdict,
                                     decision, reason, trend_filter_reason,
                                     ibs, ibs_gate_passed,
                                     earnings_blackout_reason, strategy)
               VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, $8, $9, $10, $11)""",
            symbol, quant_score,
            {**payload, "slot": slot} if slot is not None else payload,
            llm_verdict, decision, reason, trend_filter_reason,
            ibs_val, ibs_gate_passed, earnings_blackout_reason, strategy_final,
        )


async def _slots_in_use(pool) -> set[int]:
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT slot FROM positions WHERE status IN ('opening','open','closing')"
        )
    return {r["slot"] for r in rows}


async def _pending_slots(pool) -> set[int]:
    """Slots currently tied up by a pending_approvals row so we don't double-queue."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT slot FROM pending_approvals WHERE status='pending'"
        )
    return {r["slot"] for r in rows}


async def _symbol_held_or_pending(pool, symbol) -> bool:
    """Single-symbol check retained for clarity; scan path uses the batched
    `_tied_up_symbols` instead to avoid N+1 DB roundtrips."""
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """SELECT 1 FROM positions WHERE symbol=$1 AND status IN ('opening','open','closing')
               UNION ALL
               SELECT 1 FROM pending_approvals WHERE symbol=$1 AND status='pending'
               LIMIT 1""",
            symbol,
        )
    return row is not None


async def _tied_up_symbols(pool) -> set[str]:
    """Symbols currently held OR queued for approval — one roundtrip."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT symbol FROM positions
                WHERE status IN ('opening','open','closing')
               UNION
               SELECT symbol FROM pending_approvals WHERE status='pending'"""
        )
    return {r["symbol"] for r in rows}


async def _open_sector_counts(pool, strategy: str | None = None) -> dict[str, int]:
    """Sector → count of open/opening/closing positions PLUS pending approvals.

    When `strategy` is provided, counts are restricted to positions whose
    slot belongs to that strategy. Otherwise all open positions contribute
    — the PORTFOLIO-WIDE interpretation enforced by PR8 default scope.
    """
    async with pool.acquire() as c:
        if strategy is None:
            pos_rows = await c.fetch(
                """SELECT sector FROM positions
                    WHERE status IN ('opening','open','closing')"""
            )
        else:
            pos_rows = await c.fetch(
                """SELECT p.sector FROM positions p
                   JOIN slot_profiles sp ON sp.slot = p.slot
                   WHERE p.status IN ('opening','open','closing')
                     AND sp.strategy = $1""",
                strategy,
            )
        pend_rows = await c.fetch(
            """SELECT pa.symbol, sp.strategy
                 FROM pending_approvals pa
            LEFT JOIN slot_profiles sp ON sp.slot = pa.slot
                WHERE pa.status='pending'"""
        )
    counts: dict[str, int] = {}
    for r in pos_rows:
        s = r["sector"]
        if s:
            counts[s] = counts.get(s, 0) + 1
    for r in pend_rows:
        if strategy is not None and r["strategy"] != strategy:
            continue
        s = meta(r["symbol"]).sector
        if s:
            counts[s] = counts.get(s, 0) + 1
    return counts


async def _gross_notional_eur(pool) -> float:
    """Sum of (current_price × qty) across open positions. Used by the
    notional cap gate to prevent the bot from deploying more than
    MAX_GROSS_NOTIONAL_PCT of NetLiq on margin."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT COALESCE(current_price, entry_price) AS px, qty
                 FROM positions
                WHERE status IN ('opening','open','closing')
                  AND qty IS NOT NULL"""
        )
    total = 0.0
    for r in rows:
        px = float(r["px"] or 0)
        qty = float(r["qty"] or 0)
        if px > 0 and qty > 0:
            total += px * qty
    return total


async def _gross_risk_pct(pool, equity_eur: float | None) -> float:
    """Sum over open positions of (notional × stop-distance%) / equity. Returns
    0.0 if equity missing — defensive, callers treat 0 as "don't halve"."""
    if not equity_eur or equity_eur <= 0:
        return 0.0
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT entry_price, qty, stop_price FROM positions
                WHERE status IN ('opening','open','closing')
                  AND entry_price IS NOT NULL AND qty IS NOT NULL
                  AND stop_price IS NOT NULL"""
        )
    total_risk = 0.0
    for r in rows:
        entry = float(r["entry_price"])
        qty = float(r["qty"])
        stop = float(r["stop_price"])
        if entry <= 0 or qty <= 0:
            continue
        risk_per_share = max(entry - stop, 0.0)
        total_risk += risk_per_share * qty
    return (total_risk / equity_eur) * 100.0


async def _slot_profiles(pool) -> dict[int, dict]:
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM slot_profiles ORDER BY slot")
    out: dict[int, dict] = {}
    for r in rows:
        out[r["slot"]] = {
            "slot": r["slot"],
            "profile": r["profile"],
            "strategy": r["strategy"],
            "quant_score_min": float(r["quant_score_min"]),
            "rsi_max": float(r["rsi_max"]),
            "sigma_min": float(r["sigma_min"]),
            "target_profit_pct": float(r["target_profit_pct"]),
            "stop_loss_pct": float(r["stop_loss_pct"]),
            "min_net_margin_eur": float(r["min_net_margin_eur"]),
            "max_hold_seconds": int(r["max_hold_seconds"]) if r["max_hold_seconds"] else int(r["max_hold_days"]) * 86400,
            "scan_interval_sec": int(r["scan_interval_sec"]),
            "sectors_allowed": r["sectors_allowed"],
            "llm_strict": r["llm_strict"],
            "stop_atr_mult": float(r["stop_atr_mult"]) if r.get("stop_atr_mult") is not None else None,
            "trend_filter_enabled": bool(r.get("trend_filter_enabled")) if r.get("trend_filter_enabled") is not None else False,
            "require_uptrend_50_200": bool(r.get("require_uptrend_50_200")) if r.get("require_uptrend_50_200") is not None else False,
            "ibs_max": float(r["ibs_max"]) if r.get("ibs_max") is not None else None,
            "earnings_blackout_days": int(r["earnings_blackout_days"]) if r.get("earnings_blackout_days") is not None else 0,
            "stop_mode": str(r["stop_mode"]) if r.get("stop_mode") else "pct",
        }
    return out


# Per-strategy ATR multiplier defaults used when a slot sets stop_mode='atr_native'
# but leaves its own stop_atr_mult null. Spec PR6 section 6.1.
_ATR_MULT_DEFAULT = {
    "swing":        1.5,
    "intraday":     1.0,
    "crypto_scalp": 1.25,
}


def _compute_stop(price: float, entry: float | None, prof: dict,
                   payload: dict, min_width_pct: float) -> tuple[float, str]:
    """Return (stop_price, stop_source). Consolidates PR6's atr_native branch
    alongside the prior max(atr,pct) behaviour so both can live side by side
    behind the per-slot stop_mode flag.

    atr_native: stop = price - atr_mult × atr14 (falls through to pct-stop
    when atr unavailable). MIN_STOP_WIDTH_PCT still clamps as a floor.
    pct (default): preserve the existing max(atr_stop, pct_stop) logic.
    """
    base = entry if entry is not None else price
    pct_stop = base * (1 + float(prof["stop_loss_pct"]) / 100.0)
    min_width_stop = base * (1 - min_width_pct / 100.0)
    stop_mode = str(prof.get("stop_mode") or "pct")
    atr_val = payload.get("atr14")
    atr_mult = prof.get("stop_atr_mult")
    if atr_mult in (None, 0):
        atr_mult = _ATR_MULT_DEFAULT.get(prof.get("strategy"), 1.0)

    if stop_mode == "atr_native" and atr_val and atr_val > 0:
        atr_stop = base - float(atr_mult) * float(atr_val)
        stop = atr_stop
        source = "atr_native"
    elif atr_mult and atr_val and atr_val > 0:
        atr_stop = base - float(atr_mult) * float(atr_val)
        stop = max(atr_stop, pct_stop)
        source = "atr" if atr_stop >= pct_stop else "pct_floor"
    else:
        stop = pct_stop
        source = "pct"
    if stop > min_width_stop:
        stop = min_width_stop
        source = "min_width_floor"
    return stop, source


async def _record_order(pool, position_id, side, status, ib_order_id,
                        limit_price, fill_price, fill_qty, fee, raw,
                        client_order_id=None,
                        quote: "fill_quality.Quote | None" = None,
                        paper: bool = True,
                        strategy: str | None = None,
                        slot: int | None = None) -> int:
    bid = quote.bid if quote else None
    ask = quote.ask if quote else None
    mid = quote.mid if quote else None
    spread_bps = quote.spread_bps if quote else None
    slip_bps = None
    shadow = None
    if status == "filled" and fill_price:
        slip_bps = fill_quality.compute_slippage_bps(side, float(fill_price), mid)
        shadow = fill_quality.shadow_fill_price(side, float(fill_price), spread_bps, paper)
    raw_strategy = None
    effective_slot = slot
    if isinstance(raw, dict):
        raw_strategy = raw.get("strategy")
        if effective_slot is None:
            effective_slot = raw.get("slot")
    strategy_final = strategy or raw_strategy or strat_const.for_slot(effective_slot)
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """INSERT INTO orders
               (position_id, side, status, ib_order_id, limit_price, fill_price,
                fill_qty, fees, raw, client_order_id,
                bid_at_submit, ask_at_submit, spread_at_submit_bps,
                slippage_bps, shadow_fill_price, strategy)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,
                       $11,$12,$13,$14,$15,$16) RETURNING id""",
            position_id, side, status, ib_order_id,
            limit_price, fill_price, fill_qty, fee, raw, client_order_id,
            bid, ask, spread_bps, slip_bps, shadow, strategy_final,
        )
    return row["id"]


def _bad_fill(status: str, fill_qty: float, fill_price: float, ref_price: float) -> bool:
    """True when the broker reports Filled but fill fields are missing/zero.
    Inserting a position with entry_price=0 corrupts all %-based target/stop
    math; callers use this to mark the order 'rejected' and skip position
    creation. Applies to both bracket and legacy single-order entry paths."""
    return (
        status == "Filled"
        and not (
            fill_qty and fill_qty > 0
            and fill_price and fill_price > 0
            and ref_price and ref_price > 0
        )
    )


async def _insert_position(pool, symbol, slot, entry_price, qty,
                           target_price, stop_price, current_price,
                           sector, company_name, strategy: str | None = None) -> int:
    strategy_final = strategy or strat_const.for_slot(slot)
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """INSERT INTO positions
               (symbol, slot, status, entry_price, qty, target_price, stop_price,
                current_price, last_price_update, sector, company_name, strategy)
               VALUES ($1,$2,'open',$3,$4,$5,$6,$7,now(),$8,$9,$10) RETURNING id""",
            symbol, slot, entry_price, qty, target_price, stop_price,
            current_price, sector, company_name, strategy_final,
        )
    return row["id"]


_COOLDOWN_SECONDS_BY_STRATEGY = {
    "swing": 86400,         # 24h
    "intraday": 7200,       # 2h
    "crypto_scalp": 1800,   # 30min
}


async def _close_position(pool, pid, exit_price):
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """UPDATE positions SET status='closed', exit_price=$2, closed_at=now(),
                   current_price=$2, last_price_update=now()
                WHERE id=$1
            RETURNING symbol, entry_price, qty, slot""",
            pid, exit_price,
        )
        if row is None:
            return
        try:
            entry = float(row["entry_price"]) if row["entry_price"] is not None else None
            qty = float(row["qty"]) if row["qty"] is not None else None
            exit_px = float(exit_price) if exit_price is not None else None
        except (TypeError, ValueError):
            return
        if entry is None or qty is None or exit_px is None or qty <= 0:
            return
        pnl = (exit_px - entry) * qty
        if pnl >= 0:
            return
        # PR7 — only record cooldown rows on losing exits. Strategy is
        # resolved via slot_profiles, which also carries the per-slot
        # cooldown_seconds_override field for future customisation.
        strat_row = await c.fetchrow(
            """SELECT strategy, cooldown_seconds_override FROM slot_profiles
                WHERE slot=$1""",
            row["slot"],
        )
        if strat_row is None:
            return
        strategy = str(strat_row["strategy"])
        override = strat_row["cooldown_seconds_override"]
        cooldown_sec = int(override) if override is not None else \
            _COOLDOWN_SECONDS_BY_STRATEGY.get(strategy, 0)
        if cooldown_sec <= 0:
            return
        await c.execute(
            """INSERT INTO position_exits_cooldown
               (symbol, strategy, exit_ts, cooldown_until_ts, exit_pnl_eur)
               VALUES ($1, $2, NOW(), NOW() + make_interval(secs => $3), $4)""",
            row["symbol"], strategy, cooldown_sec, pnl,
        )


async def _update_position_price(pool, pid, price):
    """Update latest price + append to position_price_ticks for the dashboard
    chart. Dedupe: skip appending when the newest recorded price matches
    within 0.01 to keep the series compact during flat periods."""
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE positions SET current_price=$2, last_price_update=now() WHERE id=$1",
            pid, price,
        )
        last = await c.fetchrow(
            """SELECT price FROM position_price_ticks WHERE position_id=$1
                ORDER BY ts DESC LIMIT 1""",
            pid,
        )
        if last is None or abs(float(last["price"]) - float(price)) >= 0.01:
            await c.execute(
                "INSERT INTO position_price_ticks (position_id, price) VALUES ($1, $2)",
                pid, price,
            )


async def _update_position_stop(pool, pid, new_stop):
    async with pool.acquire() as c:
        await c.execute("UPDATE positions SET stop_price=$2 WHERE id=$1", pid, new_stop)


async def _update_position_target(pool, pid, new_target):
    async with pool.acquire() as c:
        await c.execute("UPDATE positions SET target_price=$2 WHERE id=$1", pid, new_target)


# ── regime cache ──────────────────────────────────────────────────────────────

REGIME_FAILURE_BACKOFF_SEC = 300


async def current_regime(pool, max_age_sec: int = 3600,
                          ib=None, cfg: dict | None = None,
                          asset_class: str = "stock") -> dict | None:
    """Regime source depends on cfg['REGIME_SOURCE']:
      - "deterministic": SPY 20d-vol z-score only (rec #3).
      - "llm": legacy cached-LLM label only.
      - "hybrid" (default): SPY vol-z as the authoritative gate (if it says
        risk_off, we pause); otherwise defer to LLM for the finer label.

    asset_class="crypto": deterministic-only via BTC 20d-vol percentile, with
    crypto-calibrated rv_floor (1.0 ≈ 100% annualised). No LLM fallback for
    crypto — there is no crypto-specific LLM regime prompt, and the BTC-vol
    signal is the authoritative gate for slots 19-21.
    """
    cfg = cfg or {}
    source = (cfg.get("REGIME_SOURCE") or "hybrid").lower()

    if asset_class == "crypto":
        p_risk = float(cfg.get("CRYPTO_VOL_PERCENTILE_RISKOFF", 95.0) or 95.0)
        p_mom = float(cfg.get("CRYPTO_VOL_PERCENTILE_MOMENTUM", 10.0) or 10.0)
        rv_floor = float(cfg.get("CRYPTO_VOL_RV_RISKOFF_MIN", 1.0) or 1.0)
        lookback = int(cfg.get("CRYPTO_LOOKBACK_CALENDAR_DAYS", 400) or 400)
        ref = str(cfg.get("CRYPTO_REGIME_REFERENCE_SYMBOL") or "BTC")
        det = None
        if ib is not None:
            try:
                det = await regime_det.compute_crypto(
                    ib, lookback_days=lookback,
                    percentile_riskoff=p_risk,
                    percentile_momentum=p_mom,
                    rv_floor_riskoff=rv_floor,
                    reference_symbol=ref,
                )
            except Exception as exc:
                log.warning(_j("crypto_regime_det_failed", err=str(exc)))
        if det is None:
            return None
        try:
            async with pool.acquire() as c:
                await c.execute(
                    """INSERT INTO market_regime
                       (regime, confidence, reasoning, raw, source,
                        realized_vol_z, realized_vol_percentile, asset_class)
                       VALUES ($1, $2, $3, $4::jsonb, 'deterministic', $5, $6, 'crypto')""",
                    det["regime"], det.get("confidence"), det.get("reasoning"), det,
                    det.get("realized_vol_z"), det.get("realized_vol_percentile"),
                )
        except Exception:
            pass
        return {"regime": det["regime"], "confidence": det.get("confidence"),
                "reasoning": det.get("reasoning"), "age_sec": 0,
                "source": "deterministic",
                "asset_class": "crypto",
                "realized_vol_z": det.get("realized_vol_z"),
                "realized_vol_percentile": det.get("realized_vol_percentile")}

    lookback = int(cfg.get("SPY_LOOKBACK_CALENDAR_DAYS", 500) or 500)
    p_risk = float(cfg.get("VOL_PERCENTILE_RISKOFF", 95.0) or 95.0)
    p_mom = float(cfg.get("VOL_PERCENTILE_MOMENTUM", 10.0) or 10.0)
    rv_floor = float(cfg.get("VOL_RV_RISKOFF_MIN", 0.25) or 0.25)

    det = None
    if source in {"deterministic", "hybrid"} and ib is not None:
        try:
            det = await regime_det.compute(
                ib, lookback_days=lookback,
                percentile_riskoff=p_risk,
                percentile_momentum=p_mom,
                rv_floor_riskoff=rv_floor,
            )
        except Exception as exc:
            log.warning(_j("regime_det_failed", err=str(exc)))

    # If deterministic says risk_off, that decision stands regardless of source.
    if det and det.get("regime") == "risk_off":
        try:
            async with pool.acquire() as c:
                await c.execute(
                    """INSERT INTO market_regime
                       (regime, confidence, reasoning, raw, source,
                        realized_vol_z, realized_vol_percentile, asset_class)
                       VALUES ($1, $2, $3, $4::jsonb, 'deterministic', $5, $6, 'stock')""",
                    det["regime"], det.get("confidence"), det.get("reasoning"), det,
                    det.get("realized_vol_z"), det.get("realized_vol_percentile"),
                )
        except Exception:
            pass
        return {"regime": det["regime"], "confidence": det.get("confidence"),
                "reasoning": det.get("reasoning"), "age_sec": 0,
                "source": "deterministic",
                "asset_class": "stock",
                "realized_vol_z": det.get("realized_vol_z")}

    # Hybrid / deterministic: when det succeeded and says non-risk_off, TRUST it.
    # Previously we fell through to the cached-LLM row here, which meant a stale
    # risk_off row could override a fresh "mean_reversion" reading indefinitely.
    if det is not None and source in {"deterministic", "hybrid"}:
        try:
            async with pool.acquire() as c:
                await c.execute(
                    """INSERT INTO market_regime
                       (regime, confidence, reasoning, raw, source,
                        realized_vol_z, realized_vol_percentile, asset_class)
                       VALUES ($1, $2, $3, $4::jsonb, 'deterministic', $5, $6, 'stock')""",
                    det["regime"], det.get("confidence"), det.get("reasoning"), det,
                    det.get("realized_vol_z"), det.get("realized_vol_percentile"),
                )
        except Exception:
            pass
        return {"regime": det["regime"], "confidence": det.get("confidence"),
                "reasoning": det.get("reasoning"), "age_sec": 0,
                "source": "deterministic",
                "asset_class": "stock",
                "realized_vol_z": det.get("realized_vol_z"),
                "realized_vol_percentile": det.get("realized_vol_percentile")}

    if source == "deterministic":
        # Deterministic source configured but det is None — no data available.
        return None

    # llm or (hybrid where det failed) path. Only trust LLM-sourced cache rows;
    # deterministic rows are computed fresh every call, so a stale deterministic
    # row in the DB must not be served as the current regime.
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """SELECT ts, regime, confidence, reasoning FROM market_regime
                WHERE (source='llm' OR source IS NULL)
                  AND asset_class='stock'
                ORDER BY ts DESC LIMIT 1"""
        )
        last_fail = await c.fetchrow(
            "SELECT value FROM config WHERE key='_last_regime_fail_ts'"
        )
    if row is not None:
        age = (datetime.now(timezone.utc) - row["ts"]).total_seconds()
        if age < max_age_sec:
            return {"regime": row["regime"], "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
                    "reasoning": row["reasoning"], "age_sec": int(age),
                    "source": "llm", "asset_class": "stock"}
    # Back off on recent failures so we don't hammer OpenRouter every tick when the
    # API is flaky or returning malformed JSON. Return stale cache if available.
    now_ts = time.time()
    last_fail_ts = float(last_fail["value"]) if last_fail and last_fail["value"] is not None else 0
    if now_ts - last_fail_ts < REGIME_FAILURE_BACKOFF_SEC:
        return {"regime": row["regime"], "reasoning": row["reasoning"], "age_sec": None} if row else None
    verdict = await llm.market_regime()
    if not verdict:
        async with pool.acquire() as c:
            await c.execute(
                """INSERT INTO config (key, value, updated_by)
                   VALUES ('_last_regime_fail_ts', $1::jsonb, 'bot')
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""",
                now_ts,
            )
        return {"regime": row["regime"], "reasoning": row["reasoning"], "age_sec": None} if row else None
    regime = verdict.get("regime", "mixed")
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO market_regime
               (regime, confidence, reasoning, raw, source,
                realized_vol_z, realized_vol_percentile, asset_class)
               VALUES ($1, $2, $3, $4::jsonb, 'llm', $5, $6, 'stock')""",
            regime,
            float(verdict.get("confidence")) if verdict.get("confidence") is not None else None,
            verdict.get("reasoning"),
            verdict,
            det.get("realized_vol_z") if det else None,
            det.get("realized_vol_percentile") if det else None,
        )
    return {"regime": regime, "confidence": verdict.get("confidence"),
            "reasoning": verdict.get("reasoning"), "age_sec": 0,
            "source": "llm", "asset_class": "stock",
            "realized_vol_z": det.get("realized_vol_z") if det else None}


# ── exit / stop monitoring (both strategies) ──────────────────────────────────

async def monitor_open_positions(pool, ib: IB, cfg: dict) -> None:
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT id, symbol, slot, entry_price, qty, target_price, stop_price,
                      current_price, sector, company_name, opened_at
               FROM positions WHERE status IN ('open','closing')"""
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

        # Belt-and-suspenders pre-SELL IBKR check. The top-of-tick reconcile
        # should already have synced DB state, but verify IBKR hasn't flipped
        # since reconcile ran (fills can arrive mid-tick). If IBKR qty is
        # short or flat, do NOT fire another SELL — that's how today's runaway
        # happened (7 SELL 17 = -102 short on a 17-long AIR position).
        try:
            ibkr_actual = 0.0
            for p_pos in ib.positions():
                if p_pos.contract.symbol == sym and abs(float(p_pos.position)) > 1e-9:
                    ibkr_actual = float(p_pos.position)
                    break
            if ibkr_actual <= 0:
                log.warning(_j("pre_sell_abort_ibkr_flat",
                                 position_id=r["id"], symbol=sym,
                                 ibkr_qty=ibkr_actual))
                # Close DB position so monitor never re-evaluates it.
                await _close_position(pool, r["id"], price)
                continue
        except Exception as exc:
            log.warning(_j("pre_sell_ib_check_failed", err=str(exc)))
            # Fail closed: skip this position rather than risk another runaway.
            continue

        entry = float(r["entry_price"])
        qty = float(r["qty"])
        target = float(r["target_price"])
        stop = float(r["stop_price"])
        prof = profiles.get(r["slot"], {})

        # Time-stop (in seconds — handles both swing days + intraday hours).
        opened_at = r["opened_at"].astimezone(timezone.utc)
        held_sec = (datetime.now(timezone.utc) - opened_at).total_seconds()
        max_sec = int(prof.get("max_hold_seconds") or 10 * 86400)

        # PR6 tiered time stop. Flag-gated so legacy single-threshold
        # behaviour remains the default. All three tiers use the same
        # max_sec as the 100% anchor.
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
                held_sec = max_sec  # trigger the 100% exit branch below
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
            # Closing-auction routing: when the venue closes in 10-20 min AND
            # this is a mean-reversion intraday position, route via MOC so the
            # fill lands in the auction instead of a few ticks before. Outside
            # that window or for crypto, fall back to aggressive LMT.
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
            # Accept any positive fill_qty — on a cancel race, status may read
            # Cancelled/TimedOut while IBKR actually filled. Close the position
            # on the real fill to stop the re-entrancy loop at its source.
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

        # Stop-adjust when near stop. "widen" was removed in PR5 — the
        # pydantic schema coerces any stray widen response to hold, and
        # every decision (coerced or not) is persisted to
        # stop_adjust_decisions so the counterfactual-analysis script has
        # primary data.
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

        # Rec #2: partial take-profit — sell 50% at target, move stop to breakeven for remainder
        partial_tp_enabled = cfg.get("PARTIAL_TP_ENABLED") is True
        if at_target and partial_tp_enabled and qty > 1:
            # Check if we already did partial TP for this position
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
                            await _update_position_stop(pool, r["id"], entry)  # breakeven
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
                            continue  # let the remaining position run

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

        # Target: limit sell just through the quote (captures the tick).
        # Stop: MARKET sell — a limit at price-0.05 would fail to fill in a
        # falling market and each 90s retry cycle lets the position bleed
        # further (SAP 2026-04-20 lost 3.24% despite a -1.2% slot stop).
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


# ── buy execution (used by both direct-auto + manual-approval paths) ──────────

async def _execute_buy(pool, ib: IB, sym: str, slot: int, price: float,
                       qty: float, target: float, stop: float, m,
                       source_reason: str, s_score: float, payload: dict,
                       verdict: dict, cfg: dict) -> None:
    limit_price = price + _cross_up(m, price)  # tick_size rounding inside place_limit_buy
    use_bracket = (
        cfg.get("BRACKET_ORDER_ENABLED") is True
        and m.asset_class != "crypto"
        and not cfg.get("MANUAL_APPROVAL_MODE")
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
            # Record bracket children as submitted
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

    # Fallback: legacy single-order path
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
    # Crypto scalp tolerates only a short fill window — next 30s scan tick
    # will look for fresh setups, so a 90s resting order would wedge the slot.
    buy_timeout = 15 if m.asset_class == "crypto" else 90
    status = await broker.wait_for_fill_or_cancel(trade, timeout_sec=buy_timeout, ib=ib)
    fill_price = trade.orderStatus.avgFillPrice or 0
    fill_qty = trade.orderStatus.filled or 0
    # Guard against broker reporting Filled with missing/zero fill fields — inserting a
    # position with entry_price=0 corrupts all downstream %-based target/stop math.
    # Accept any positive fill_qty — on a timeout/cancel race, status may read
    # Cancelled or TimedOut while IBKR actually filled. Recording the real fill
    # prevents zombie positions sitting at the broker without bot oversight.
    if fill_qty > 0 and fill_price and fill_price > 0 and price > 0:
        fee = fees.estimate_side("BUY", fill_qty, fill_price, m.currency, m.asset_class).total
        pid = await _insert_position(
            pool, sym, slot, float(fill_price), float(fill_qty),
            target_price=float(fill_price) * target / price,
            stop_price=float(fill_price) * stop / price,
            current_price=float(fill_price),
            sector=m.sector, company_name=m.name,
        )
        # Bootstrap the dashboard chart with ~30 recent daily closes (cached from
        # the scan path, so no extra IBKR hit). Timestamps are synthetic — one
        # per trading day leading up to now — giving the sparkline immediate shape.
        try:
            # Sparkline bootstrap: shape matches hold horizon. Crypto scalp
            # closes in 1h so a 30-day daily-bar tail buries the live section;
            # use 1-min bars instead.
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


# ── universe scan with per-slot profiles + candidate ranking ──────────────────

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

    # Regime: per-asset-class. Stock slots gated by SPY RV percentile; crypto
    # slots gated by BTC RV percentile. Either class can be risk_off without
    # halting the other — uncorrelated vol regimes.
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
    # Full halt on the affected class only: empty set → the per-sym filter drops.

    # Batch DB reads: held/pending symbols + sector counts — one roundtrip each.
    tied_symbols = await _tied_up_symbols(pool)
    sector_scope = str(cfg.get("MAX_POSITIONS_PER_SECTOR_SCOPE") or "portfolio").lower()
    sector_counts = await _open_sector_counts(
        pool, strategy=None if sector_scope == "portfolio" else strategy,
    )
    # PR7 — active cooldowns (per-strategy, symbol-level). Fetched once per
    # scan; the gate checks the in-memory set so the scan loop doesn't
    # roundtrip per candidate.
    cooldown_symbols: set[str] = set()
    if cfg.get("REENTRY_COOLDOWN_ENABLED"):
        async with pool.acquire() as c:
            cd_rows = await c.fetch(
                """SELECT symbol FROM position_exits_cooldown
                    WHERE strategy=$1 AND cooldown_until_ts > NOW()""",
                strategy,
            )
        cooldown_symbols = {r["symbol"] for r in cd_rows}

    # Earnings calendar rows — only pulled when the flag is on (the gate's
    # apply_earnings_blackout helper short-circuits cheaply otherwise).
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

    # Pre-filter universe before hitting IBKR: drop tied-up, closed markets,
    # sector-cap-saturated symbols, and symbols whose asset-class regime is
    # risk_off. crypto_scalp restricts further to crypto-only — its 1-min
    # bars would waste IBKR requests on stocks that the Crypto-only slot
    # profiles would never accept anyway.
    crypto_only_strategy = strategy == "crypto_scalp"
    scan_syms: list[str] = []
    scan_meta: dict[str, object] = {}
    for sym in universe:
        if sym in tied_symbols:
            continue
        if sym in cooldown_symbols:
            # PR7: within the per-strategy cooldown window after a losing
            # exit. Logged via _log_signal so the filter-impact query has a
            # row; bumps to the next symbol otherwise.
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

    # Batch-fetch historical bars (cached + concurrency-capped).
    if strategy == "crypto_scalp":
        # 1-min AGGTRADES bars over 1 trading day. ~1440 bars of context is
        # plenty for RSI-2 + SMA20. TTL tight (20s) since scans every 30s.
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
        # Rec #1: fetch daily bars for multi-timeframe SMA50/200 confirmation
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

        # Slot filtering (quant + sector + optional trend).
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
            "gate_outcome": None,  # PR12 — set by each terminal branch
        })

    if not candidates:
        return

    # PR12 — one regime read per scan for snapshot tagging.
    _stock_regime_label = (regime_stock or {}).get("regime") if regime_stock else None
    _crypto_regime_label = (regime_crypto or {}).get("regime") if regime_crypto else None

    # LLM ranking first (1 call), then parallel veto checks across survivors.
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

    # Sequential slot assignment + execution (order-dependent due to slot lifecycle).
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
        # crypto_scalp: LLM abstains on most crypto dips (prompt tuned for
        # equities, no catalyst context for 1-min moves). Treat abstain as
        # pass when the slot is non-strict — the quant gate already filters.
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

        m = meta(cand["symbol"])
        # Sector cap re-check (may have saturated during this scan cycle).
        if max_per_sector and sector_counts.get(m.sector, 0) >= max_per_sector:
            cand["gate_outcome"] = "sector_cap"
            await _log_signal(pool, cand["symbol"], cand["score"], cand["payload"], verdict,
                              "skip", f"sector cap {max_per_sector} hit for {m.sector}", slot=chosen_slot)
            continue

        price = cand["last_close"]

        target = price * (1 + prof["target_profit_pct"] / 100.0)
        # Stop mode routed through _compute_stop. MIN_STOP_WIDTH_PCT default
        # bumped to 0.75 in PR6 — an 0.5% stop on an intraday 5-min bar can't
        # survive normal tick jitter on volatile names (SAP 2026-04-20).
        min_width_pct = float(cfg.get("MIN_STOP_WIDTH_PCT", 0.75) or 0.0)
        stop, stop_source = _compute_stop(price, None, prof, cand["payload"],
                                             min_width_pct)
        cand["payload"]["stop_source"] = stop_source
        cand["payload"]["stop_mode"] = prof.get("stop_mode") or "pct"
        cand["payload"]["stop_distance_pct"] = round((price - stop) / price * 100, 3)

        # Sizing: fixed or vol-target. vol_target requires known equity + stop.
        size_mode = (cfg.get("POSITION_SIZE_MODE") or "fixed").lower()
        equity_eur = cfg.get("_equity_eur")
        risk_pct = float(cfg.get("POSITION_RISK_PCT", 0.5) or 0.5)

        # Rec #3: sentiment-based position scaling from LLM check
        sentiment = int((verdict or {}).get("sentiment_score") or 50)
        sentiment_mult = max(0.25, min(1.0, sentiment / 100.0))
        if cfg.get("LLM_SENTIMENT_SIZING_ENABLED") and sentiment_mult < 1.0:
            slot_size_eur = slot_size_eur * sentiment_mult
            cand["payload"]["sentiment_score"] = sentiment
            cand["payload"]["sentiment_mult"] = round(sentiment_mult, 3)
            log.info(_j("sentiment_sizing", symbol=cand["symbol"],
                        sentiment=sentiment, mult=round(sentiment_mult, 3)))

        # PR8 — gross-risk halving. When the portfolio is already carrying
        # ≥ MAX_GROSS_RISK_PCT of equity in stop-distance exposure, new
        # entries size down to keep aggregate risk bounded. Repeat halving
        # until the projected aggregate drops back below threshold or the
        # slot is cut to zero.
        max_gross_risk = float(cfg.get("MAX_GROSS_RISK_PCT", 6.0) or 0.0)
        size_multiplier = 1.0
        if max_gross_risk > 0 and equity_eur and equity_eur > 0:
            current_risk_pct = await _gross_risk_pct(pool, equity_eur)
            while current_risk_pct >= max_gross_risk and size_multiplier > 0.0625:
                size_multiplier *= 0.5
                # Approximate: assume we'll contribute risk proportional to
                # the sizing reduction. Log the halving for visibility.
                current_risk_pct = current_risk_pct  # fixed in-flight; loop breaks via multiplier floor

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
        # Gross notional cap (PR10) — hard refuse if this entry would push
        # aggregate (existing + proposed) notional above MAX_GROSS_NOTIONAL_PCT
        # of NetLiq. At 100 means no margin; 150 allows 1.5× leverage; 0 disables.
        # Applied BEFORE the qty<1 rescue below so we don't waste a slot on an
        # entry we would just cap afterwards.
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
                log.info(_j("gross_notional_cap_skip",
                              symbol=cand["symbol"],
                              existing=round(existing_notional, 2),
                              proposed=round(qty * price, 2),
                              cap=round(cap_eur, 2),
                              netliq=round(equity_eur, 2)))
                continue

        # Crypto: fractional OK (PAXOS supports 0.0001 BTC). Skip the
        # "qty < 1" rescue meant for shares-not-integer-divisible-by-price.
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

    # PR12 — signal_snapshots instrumentation. Fire-and-forget per candidate
    # that entered the dispatch loop (i.e. passed pre-filter + matched slot).
    # No flag: instrumentation is always on.
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


async def process_approvals(pool, ib: IB, cfg: dict) -> None:
    """Pick up approved pending_approvals and execute them; expire stale pending ones."""
    expiry = int(cfg.get("APPROVAL_EXPIRY_SEC", 1800))
    async with pool.acquire() as c:
        await c.execute(
            """UPDATE pending_approvals
               SET status='expired', reviewed_at=now(), reviewed_by='system'
               WHERE status='pending' AND ts < now() - ($1 || ' seconds')::interval""",
            str(expiry),
        )
        approved = await c.fetch(
            """SELECT * FROM pending_approvals WHERE status='approved' ORDER BY ts ASC"""
        )
    for r in approved:
        sym = r["symbol"]
        m = meta(sym)
        if not hours.market_open_for_symbol(sym):
            continue
        price = float(r["price"])
        qty = float(r["qty"])
        target = float(r["target_price"])
        stop = float(r["stop_price"])
        log.info(_j("executing_approved", symbol=sym, slot=r["slot"]))
        # Mark executed BEFORE placing so concurrent ticks don't double-execute.
        async with pool.acquire() as c:
            await c.execute("UPDATE pending_approvals SET status='executed' WHERE id=$1", r["id"])
        await _execute_buy(
            pool, ib, sym, r["slot"], price, qty, target, stop, m,
            "manual_approval", float(r["quant_score"] or 0), r["payload"] or {}, r["llm_verdict"] or {},
            cfg,
        )


# ── top-level tick ───────────────────────────────────────────────────────────

async def run_once(pool, ib: IB, cfg: dict) -> None:
    # Crypto paper-sim toggle. IBKR paper accounts can't be permissioned for
    # Paxos crypto — orders silently flip to Inactive. With sim on, broker
    # synthesizes fills at live prices ± slippage so scalp strategy can be
    # validated end-to-end without funding a live account.
    broker.set_crypto_paper_sim(bool(cfg.get("CRYPTO_PAPER_SIM", True)))
    if cfg.get("CRYPTO_PAPER_SIM_SLIPPAGE_BPS") is not None:
        try:
            broker.set_crypto_paper_sim_slippage_bps(float(cfg["CRYPTO_PAPER_SIM_SLIPPAGE_BPS"]))
        except (TypeError, ValueError):
            pass

    # Exit logic runs every tick regardless of region / strategy.
    await monitor_open_positions(pool, ib, cfg)
    # Pick up any approvals the user clicked.
    await process_approvals(pool, ib, cfg)

    now = time.time()
    # Per-strategy scan gating via separate _last_scan_ts_<strategy> config keys.
    # crypto_scalp runs every 30s on 1-min AGGTRADES bars — short-term
    # micro-dip capture on BTC/ETH/LTC/BCH. Keeps intraday at 60s / 5-min
    # for equities where fees dominate below 1% moves.
    for strategy, default_interval in (("swing", 300), ("intraday", 60), ("crypto_scalp", 30)):
        async with pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT value FROM config WHERE key=$1",
                f"_last_scan_ts_{strategy}",
            )
        last = float(row["value"]) if row and row["value"] is not None else 0
        if now - last < default_interval:
            continue
        await _scan_for_strategy(pool, ib, cfg, strategy)
        async with pool.acquire() as c:
            await c.execute(
                """INSERT INTO config (key, value, updated_by)
                   VALUES ($1, $2::jsonb, 'bot')
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""",
                f"_last_scan_ts_{strategy}", now,
            )


__all__ = ["run_once", "_ensure_initial_baseline", "current_regime"]
