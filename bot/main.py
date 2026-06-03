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

from . import broker, config as cfg_validate, entry_price_guard, llm, jobs, reconciler as reconciler_mod, risk, signals, strategy, notifications
from .strategies import overnight as overnight_strategy

load_dotenv("/opt/trader/infra/.env")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":%(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%S%z",
    stream=sys.stdout,
)
log = logging.getLogger("bot")


logging.getLogger('ib_async').setLevel(logging.WARNING)


def j(msg: str, **fields) -> str:
    return json.dumps({"m": msg, **fields}, default=str)


LIVE_ACK = "I_ACCEPT_REAL_MONEY_RISK_100_EUR"


def _cfg_float(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _account_float(account: dict, key: str) -> float | None:
    try:
        return float(account[key]["value"])
    except Exception:
        return None


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
        self.reconciler = reconciler_mod.Reconciler(pool, self.ib)

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
            broker.register_fill_callbacks(self.ib, self.pool)
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

    def live_entry_preflight(self, cfg: dict, account: dict) -> tuple[bool, str | None]:
        """Fail closed for real-money entries.

        Exits/monitoring still run when this returns false; only new BUY scans
        are blocked. This intentionally makes live mode opt-in with a narrow
        100 EUR canary envelope.
        """
        if str(cfg.get("TRADING_MODE") or "paper").lower() != "live":
            return True, None

        if cfg.get("LIVE_TRADING_ACK") != LIVE_ACK:
            return False, "missing_live_ack"
        if cfg.get("MANUAL_APPROVAL_MODE") is not True:
            return False, "manual_approval_required"

        max_order_eur = _cfg_float(cfg, "LIVE_MAX_ORDER_EUR", 100.0)
        slot_size_eur = _cfg_float(cfg, "SLOT_SIZE_EUR", 0.0)
        if max_order_eur <= 0 or max_order_eur > 100:
            return False, "live_max_order_must_be_1_to_100_eur"
        if slot_size_eur <= 0 or slot_size_eur > max_order_eur:
            return False, "slot_size_exceeds_live_max_order"

        max_gross_eur = _cfg_float(cfg, "LIVE_MAX_GROSS_EUR", 100.0)
        gross = _account_float(account, "GrossPositionValue")
        if gross is not None and gross > max_gross_eur:
            return False, f"gross_exposure_exceeds_live_cap:{gross:.2f}>{max_gross_eur:.2f}"

        return True, None

    async def _reconcile_positions_authoritative(self, cfg: dict | None = None) -> None:
        """Thin delegate to bot.reconciler.Reconciler. Logic was moved out
        of main.py 2026-05-08 — see reconciler.py for the state machine.
        Kept here for back-compat with the tick loop and any external
        callers that still hold a reference."""
        await self.reconciler.reconcile_positions(cfg)

    async def tick(self) -> None:
        cfg = await self.read_config()
        ib_ok = await self.ensure_ib()
        account = await self.account_snapshot() if ib_ok else {}
        await self.heartbeat(cfg, ib_ok, account)
        if self.stopping:
            return




        if ib_ok:
            try:
                await self._reconcile_positions_authoritative(cfg)
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


        if ib_ok and equity_eur is not None:
            try:
                await strategy._ensure_initial_baseline(self.pool, equity_eur)
            except Exception as exc:
                log.warning(j("baseline_write_failed", err=str(exc)))



        breaker = await risk.evaluate(self.pool, cfg, equity_eur)
        live_entries_ok, live_block_reason = self.live_entry_preflight(cfg, account)

        try:
            killed = await risk.check_auto_kill(self.pool, cfg, equity_eur)
            if killed:


                cfg["BOT_ENABLED"] = False
                cfg["AUTO_KILLED_REASON"] = killed
        except Exception as exc:
            log.exception(j("auto_kill_check_failed", err=str(exc)))



        if self.stopping:
            return

        cfg["_equity_eur"] = equity_eur


        llm.set_context(self.pool, cfg)

        enabled = cfg.get("BOT_ENABLED") is True and live_entries_ok
        log.info(j("tick", enabled=enabled, ib=ib_ok, mode=cfg.get("TRADING_MODE"),
                   breaker_tripped=breaker.get("tripped"),
                   breaker_reason=breaker.get("reason"),
                   live_block_reason=live_block_reason))

        if not ib_ok:
            return
        if not enabled:






            try:
                await strategy.monitor_open_positions(self.pool, self.ib, cfg)
            except Exception as exc:
                log.exception(j("monitor_error", err=str(exc)))
            return
        if breaker.get("tripped"):
            try:
                await notifications.notify_circuit_breaker(
                    breaker.get("reason") or "unknown",
                    equity_eur=equity_eur,
                )
            except Exception:
                pass

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


        try:
            await jobs.maybe_check_llm_malformed_rate(self.pool)
            await jobs.maybe_refresh_observed_slippage(self.pool)
            await jobs.maybe_resolve_error_positions(self.pool)
            await jobs.maybe_backfill_hypothetical_outcomes(self.pool, self.ib)
            await jobs.maybe_sync_earnings(self.pool, self.ib)
            await jobs.maybe_daily_report(self.pool)
            await jobs.maybe_weekly_tuning(self.pool)
            await jobs.maybe_briefing(self.pool)
            await jobs.maybe_news_watch(self.pool, self.ib)
            await jobs.maybe_notify_critical_findings(self.pool)
        except Exception as exc:
            log.exception(j("jobs_error", err=str(exc)))

        if self.stopping:
            return



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
