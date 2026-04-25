"""Trader bot — v3.

Persistent IB Gateway connection; runs account snapshots + strategy ticks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal as _signal
import sys
from contextlib import suppress

import asyncpg
from dotenv import load_dotenv
from ib_async import IB, util as ib_util

from . import config as cfg_validate, llm, jobs, risk, strategy, notifications
from .strategies import overnight as overnight_strategy

load_dotenv("./infra/.env")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":%(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%S%z",
    stream=sys.stdout,
)
log = logging.getLogger("bot")
# ib_util.logToConsole() replaces all root handlers and forces WARNING level,
# which suppresses our INFO tick logs. Configure ib_async separately.
logging.getLogger('ib_async').setLevel(logging.WARNING)


def j(msg: str, **fields) -> str:
    return json.dumps({"m": msg, **fields}, default=str)


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


class Bot:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.stopping = False
        self.ib = IB()
        self.ib_host = os.getenv("IB_HOST", "127.0.0.1")
        self.ib_port = int(os.getenv("IB_PORT", "4002"))
        self.ib_client_id = int(os.getenv("IB_CLIENT_ID", "1"))

    async def read_config(self) -> dict:
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT key, value FROM config")
        return {r["key"]: r["value"] for r in rows}

    async def ensure_ib(self) -> bool:
        if self.ib.isConnected():
            return True
        try:
            await self.ib.connectAsync(
                self.ib_host, self.ib_port, clientId=self.ib_client_id,
                timeout=15,
            )
            log.info(j("ib_connected", host=self.ib_host, port=self.ib_port))
            return True
        except Exception as exc:
            log.warning(j("ib_connect_failed", err=str(exc)))
            return False

    async def account_snapshot(self) -> dict:
        if not self.ib.isConnected():
            return {}
        try:
            summary = await self.ib.accountSummaryAsync()
            want = {
                "NetLiquidation", "TotalCashValue", "BuyingPower",
                "AvailableFunds", "GrossPositionValue",
            }
            out: dict = {}
            for row in summary:
                if row.tag in want:
                    out[row.tag] = {
                        "value": row.value,
                        "currency": row.currency,
                    }
            return out
        except Exception as exc:
            log.warning(j("account_snapshot_failed", err=str(exc)))
            return {}

    async def heartbeat(self, cfg: dict, ib_ok: bool, account: dict) -> None:
        info = {
            "bot_enabled": cfg.get("BOT_ENABLED"),
            "mode": cfg.get("TRADING_MODE"),
            "universe_size": len(cfg.get("UNIVERSE", [])),
            "ib_connected": ib_ok,
            "account": account,
        }
        async with self.pool.acquire() as c:
            await c.execute(
                """
                INSERT INTO heartbeat (component, ts, info)
                VALUES ('bot', now(), $1::jsonb)
                ON CONFLICT (component) DO UPDATE
                SET ts = EXCLUDED.ts, info = EXCLUDED.info
                """,
                info,
            )

    async def _reconcile_positions_authoritative(self) -> None:
        """Sync DB open positions to IBKR positions. IBKR is source of truth.

        Runs every tick at the top. If IBKR shows qty<=0 for a symbol the DB
        thinks is open, close the DB position at its last known current_price.
        This prevents the SELL re-entrancy runaway: if a SELL already filled
        at IBKR but the DB still says 'open', next tick would fire another SELL
        — catastrophic when the runaway repeats (AIR: +17 -> -102 today).

        Does NOT auto-insert orphans (positions at IBKR the DB doesn't know).
        Those are logged for operator; auto-inserting without signal metadata
        would produce mis-managed positions.
        """
        if not self.ib.isConnected():
            return
        try:
            ibkr_pos = self.ib.positions()
        except Exception as exc:
            log.warning(j("reconcile_ib_positions_failed", err=str(exc)))
            return
        # Guard: IBKR can return empty list during connection blips.
        # Do NOT mass-close positions on an unverified empty response.
        if not ibkr_pos:
            log.warning(j("reconcile_empty_positions_skipped"))
            return
        ibkr_qty_by_symbol = {
            p.contract.symbol: float(p.position)
            for p in ibkr_pos
            if abs(float(p.position)) > 1e-9
        }

        async with self.pool.acquire() as c:
            db_rows = await c.fetch(
                "SELECT id, symbol, qty, current_price, entry_price "
                "FROM positions WHERE status IN ('open','opening','closing')"
            )

        closed = 0
        qty_adjusted = 0
        for r in db_rows:
            sym = r["symbol"]
            db_qty = float(r["qty"] or 0)
            ibkr_qty = ibkr_qty_by_symbol.get(sym, 0.0)
            if ibkr_qty <= 0 and db_qty > 0:
                # IBKR is flat but DB thinks we are long. The SELL(s) already
                # filled or the position was closed externally. Close in DB
                # at last known current_price (best estimate of actual exit).
                exit_px = float(r["current_price"] or r["entry_price"] or 0)
                async with self.pool.acquire() as c:
                    await c.execute(
                        "UPDATE positions SET status='closed', "
                        "exit_price=$1, closed_at=NOW() WHERE id=$2",
                        exit_px, r["id"],
                    )
                log.warning(j("reconcile_closed_db_position",
                               id=r["id"], symbol=sym,
                               db_qty=db_qty, ibkr_qty=ibkr_qty,
                               exit_price=exit_px))
                closed += 1
            elif 0 < ibkr_qty < db_qty:
                # Partial: IBKR has fewer shares than DB. Update DB qty to
                # reflect reality. Next monitor tick sees the smaller position.
                async with self.pool.acquire() as c:
                    await c.execute(
                        "UPDATE positions SET qty=$1 WHERE id=$2",
                        ibkr_qty, r["id"],
                    )
                log.warning(j("reconcile_adjusted_qty",
                               id=r["id"], symbol=sym,
                               db_qty=db_qty, ibkr_qty=ibkr_qty))
                qty_adjusted += 1

        # Orphan IBKR positions (informational — can't auto-manage without metadata)
        db_syms = {r["symbol"] for r in db_rows}
        orphans = [
            {"symbol": sym, "qty": qty}
            for sym, qty in ibkr_qty_by_symbol.items()
            if sym not in db_syms and qty > 0
        ]
        if orphans:
            log.error(j("orphan_ibkr_positions",
                          count=len(orphans), positions=orphans))
        if closed or qty_adjusted:
            log.info(j("reconcile_summary",
                        closed=closed, qty_adjusted=qty_adjusted,
                        ibkr_count=len(ibkr_qty_by_symbol),
                        db_count=len(db_rows)))

    async def tick(self) -> None:
        cfg = await self.read_config()
        ib_ok = await self.ensure_ib()
        account = await self.account_snapshot() if ib_ok else {}
        await self.heartbeat(cfg, ib_ok, account)
        if self.stopping:
            return

        # Reconciliation: every 5 min, compare IBKR positions to DB open positions
        # and loudly warn on orphans. Catches the wait_for_fill_or_cancel race
        # from the other direction — if a fix regresses, zombies surface here.
        if ib_ok:
            try:
                await self._reconcile_positions_authoritative()
            except Exception as exc:
                log.warning(j("reconcile_failed", err=str(exc)))
            try:
                await self._reconcile_orders_authoritative()
            except Exception as exc:
                log.warning(j("reconcile_orders_failed", err=str(exc)))

        if self.stopping:
            return

        equity_eur: float | None = None
        if "NetLiquidation" in account:
            try:
                equity_eur = float(account["NetLiquidation"]["value"])
            except Exception:
                equity_eur = None

        # First-tick baseline: record NetLiq on first successful IB connection.
        if ib_ok and equity_eur is not None:
            try:
                await strategy._ensure_initial_baseline(self.pool, equity_eur)
            except Exception as exc:
                log.warning(j("baseline_write_failed", err=str(exc)))

        # Circuit breakers — evaluate every tick even when trading is disabled, so
        # HWM and day-start snapshots keep advancing.
        breaker = await risk.evaluate(self.pool, cfg, equity_eur)
        # PR9: second-layer auto-kill (distinct from evaluate's circuit breaker).
        try:
            killed = await risk.check_auto_kill(self.pool, cfg, equity_eur)
            if killed:
                # BOT_ENABLED flipped to false already; reload cfg so downstream
                # scan-tick sees the new value without waiting a whole cycle.
                cfg["BOT_ENABLED"] = False
                cfg["AUTO_KILLED_REASON"] = killed
        except Exception as exc:
            log.exception(j("auto_kill_check_failed", err=str(exc)))

        # Inject live equity into cfg so strategy.sizing.vol_target can read it
        # without a second round-trip.
        if self.stopping:
            return

        cfg["_equity_eur"] = equity_eur
        # Set LLM module context so spend tracking + budget enforcement work
        # without changing every call site.
        llm.set_context(self.pool, cfg)

        enabled = cfg.get("BOT_ENABLED") is True
        log.info(j("tick", enabled=enabled, ib=ib_ok, mode=cfg.get("TRADING_MODE"),
                   breaker_tripped=breaker.get("tripped"),
                   breaker_reason=breaker.get("reason")))

        if not enabled or not ib_ok:
            return
        if breaker.get("tripped"):
            try:
                await notifications.notify_circuit_breaker(
                    breaker.get("reason") or "unknown",
                    equity_eur=equity_eur,
                )
            except Exception:
                pass
            # Still monitor exits on open positions; just block new entries.
            try:
                await strategy.monitor_open_positions(self.pool, self.ib, cfg)
            except Exception as exc:
                log.exception(j("monitor_error", err=str(exc)))
            return

        try:
            await strategy.run_once(self.pool, self.ib, cfg)
        except Exception as exc:
            log.exception(j("strategy_error", err=str(exc)))

        if self.stopping:
            return

        # Scheduled LLM jobs — cheap early-returns if not yet due.
        try:
            await jobs.maybe_check_llm_malformed_rate(self.pool)
            await jobs.maybe_backfill_hypothetical_outcomes(self.pool, self.ib)
            await jobs.maybe_sync_earnings(self.pool, self.ib)
            await jobs.maybe_daily_report(self.pool)
            await jobs.maybe_weekly_tuning(self.pool)
            await jobs.auto_apply_pending_tuning(self.pool)
            await jobs.maybe_briefing(self.pool)
            await jobs.maybe_news_watch(self.pool, self.ib)
            await jobs.maybe_notify_critical_findings(self.pool)
        except Exception as exc:
            log.exception(j("jobs_error", err=str(exc)))

        if self.stopping:
            return

        # Overnight Edge strategy — independently gated on OVERNIGHT_ENABLED.
        # Own scan windows (15:45 / 09:25 ET), never interferes with mean-rev.
        try:
            await overnight_strategy.run(self.pool, self.ib, cfg)
        except Exception as exc:
            log.exception(j("overnight_error", err=str(exc)))

    async def _flush_pre_restart_orders(self) -> None:
        """Cancel any open orders inherited from a previous process instance.
        Uses reqGlobalCancel for a clean slate — safe because this runs
        immediately after connect and before the first tick."""
        if not self.ib.isConnected():
            return
        open_orders = self.ib.openOrders()
        if open_orders:
            log.info(j("flush_pre_restart", count=len(open_orders)))
            self.ib.reqGlobalCancel()
            await asyncio.sleep(1)
        else:
            log.info(j("flush_pre_restart", count=0))


    async def _reconcile_orders_authoritative(self) -> None:
        """Sync DB orders in 'submitted'/'partial' to IBKR ground truth.
        Orders missing from both openOrders() and trades() are marked
        terminal — 'filled' if their position is already closed, otherwise
        'cancelled' after a 24h grace period."""
        if not self.ib.isConnected():
            return
        try:
            open_orders = self.ib.openOrders()
            trades = self.ib.trades()
        except Exception as exc:
            log.warning(j("reconcile_orders_ib_failed", err=str(exc)))
            return

        open_by_coid: dict[str, Any] = {}
        for o in open_orders:
            ref = getattr(o, "orderRef", None)
            if ref:
                open_by_coid[str(ref)] = o
        trade_by_coid: dict[str, Any] = {}
        for t in trades:
            ref = getattr(t.order, "orderRef", None)
            if ref:
                trade_by_coid[str(ref)] = t

        fixed = 0
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                """SELECT o.id, o.ib_order_id, o.client_order_id, o.side,
                          o.position_id, p.status AS position_status,
                          o.ts
                     FROM orders o
                     LEFT JOIN positions p ON p.id = o.position_id
                    WHERE o.status IN ('submitted','partial')"""
            )

            for r in rows:
                coid = str(r["client_order_id"]) if r["client_order_id"] else None
                open_ord = open_by_coid.get(coid) if coid else None
                trade = trade_by_coid.get(coid) if coid else None

                if open_ord is not None:
                    ib_status = getattr(getattr(open_ord, "orderStatus", None), "status", None)
                    if ib_status in ("Filled",):
                        continue
                    elif ib_status in ("Cancelled", "ApiCancelled", "Inactive"):
                        await c.execute(
                            "UPDATE orders SET status='cancelled' WHERE id=$1",
                            r["id"],
                        )
                        fixed += 1
                    continue

                if trade is not None:
                    ib_status = getattr(getattr(trade, "orderStatus", None), "status", None)
                    if ib_status == "Filled":
                        fill_px = float(trade.orderStatus.avgFillPrice)
                        fill_qty = float(trade.orderStatus.filled)
                        await c.execute(
                            "UPDATE orders SET status='filled', fill_price=$2, fill_qty=$3 WHERE id=$1",
                            r["id"], fill_px, fill_qty,
                        )
                        fixed += 1
                    elif ib_status in ("Cancelled", "ApiCancelled", "Inactive"):
                        await c.execute(
                            "UPDATE orders SET status='cancelled' WHERE id=$1",
                            r["id"],
                        )
                        fixed += 1
                    continue

                # Orphan order row — not found at IBKR
                if r["position_status"] == "closed":
                    await c.execute(
                        "UPDATE orders SET status='filled' WHERE id=$1",
                        r["id"],
                    )
                    fixed += 1
                elif r["position_status"] == "error":
                    await c.execute(
                        "UPDATE orders SET status='cancelled' WHERE id=$1",
                        r["id"],
                    )
                    fixed += 1
                elif r["position_status"] is None:
                    await c.execute(
                        "UPDATE orders SET status='cancelled' WHERE id=$1",
                        r["id"],
                    )
                    fixed += 1
                else:
                    from datetime import datetime, timezone, timedelta
                    age = datetime.now(timezone.utc) - r["ts"]
                    if age > timedelta(hours=24):
                        await c.execute(
                            "UPDATE orders SET status='cancelled' WHERE id=$1",
                            r["id"],
                        )
                        fixed += 1
        if fixed:
            log.info(j("reconcile_orders_summary", fixed=fixed))

    async def _validate_startup(self) -> None:
        """Run startup invariants. Raises ConfigError from config.validate on
        failure; the main() coroutine catches that and exits non-zero so
        systemd surfaces it instead of silently running a broken config."""
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                """SELECT slot, strategy, profile, target_profit_pct,
                          stop_loss_pct, sectors_allowed
                     FROM slot_profiles"""
            )
            cfg_rows = await c.fetch(
                """SELECT key, value FROM config
                    WHERE key IN ('SLOT_SIZE_EUR', 'CRYPTO_PAPER_SIM')"""
            )
        cfg_kv = {r["key"]: r["value"] for r in cfg_rows}
        slot_size_eur = float(cfg_kv.get("SLOT_SIZE_EUR", 1000) or 1000)
        crypto_paper_sim = bool(cfg_kv.get("CRYPTO_PAPER_SIM", True))
        profiles = [
            {
                "slot": r["slot"],
                "strategy": r["strategy"],
                "profile": r["profile"],
                "target_profit_pct": float(r["target_profit_pct"]),
                "stop_loss_pct": float(r["stop_loss_pct"]),
                "sectors_allowed": r["sectors_allowed"],
            }
            for r in rows
        ]
        cfg_validate.validate(
            profiles,
            slot_size_eur=slot_size_eur,
            crypto_paper_sim=crypto_paper_sim,
        )

    async def run(self) -> None:
        log.info(j("bot_start", pid=os.getpid()))
        await self._validate_startup()
        # Connect + flush inherited orders before the first tick so nothing
        # from the prior process can fill against us silently.
        await self.ensure_ib()
        try:
            await self._flush_pre_restart_orders()
        except Exception as exc:
            log.warning(j("startup_flush_failed", err=str(exc)))
        while not self.stopping:
            try:
                await self.tick()
            except Exception as exc:
                log.exception(j("tick_error", err=str(exc)))
            await asyncio.sleep(10)
        log.info(j("bot_stop"))
        if self.ib.isConnected():
            self.ib.disconnect()


async def main() -> None:
    dsn = os.environ["DATABASE_URL"]
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=4, init=_init_connection
    )
    bot = Bot(pool)

    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, lambda: setattr(bot, "stopping", True))

    try:
        await bot.run()
    except cfg_validate.ConfigError as exc:
        log.error(j("startup_validation_failed", err=str(exc)))
        raise SystemExit(2) from exc
    finally:
        with suppress(Exception):
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
