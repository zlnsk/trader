"""Position reconciliation: DB ↔ broker state machine.

States detected on each tick (when broker positions are non-empty):
  * Synced     — DB and broker agree on (symbol, sign, qty)
  * DBOnly     — DB has a position the broker doesn't
  * BrokerOnly — broker has a position the DB doesn't (orphan)
  * SignFlip   — DB long while broker short (or vice versa)
  * QtyMismatch — same sign, different size

Special cases (not state transitions):
  * EmptyBroker — broker returned [] (cache blip / startup); skip without
                  closing anything
  * IBDisconnected — bail without writing audit

Every action is persisted to `reconciliation_events` so the dashboard can
surface drift history. The class owns its own audit writer; callers don't
have to thread it.

Extracted from main.py 2026-05-08 (was ~180 lines inline in tick loop). The
logic is preserved bit-for-bit except:
  * sign-flip + close paths now fetch broker.latest_trade_price as exit
    fallback instead of using entry_price (which could mask losses);
  * orphan free-slot search uses cfg['MAX_SLOTS'] as upper bound, not 30;
  * each branch writes a row to reconciliation_events.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg
from ib_async import IB

from . import broker, entry_price_guard, signals

log = logging.getLogger("bot.reconciler")


def _j(msg: str, **fields) -> str:
    return json.dumps({"m": msg, **fields})


class Reconciler:
    def __init__(self, pool: asyncpg.Pool, ib: IB) -> None:
        self.pool = pool
        self.ib = ib



        self._last_state: str | None = None
        self._seen_short_orphans: set[tuple[str, float]] = set()


    async def _audit(
        self, state: str, action: str, *,
        symbol: str | None = None,
        db_qty: float | None = None,
        broker_qty: float | None = None,
        db_position_id: int | None = None,
        **detail: Any,
    ) -> None:
        try:
            async with self.pool.acquire() as c:
                await c.execute(
                    """INSERT INTO reconciliation_events
                       (state, action, symbol, db_qty, broker_qty,
                        db_position_id, detail)
                       VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)""",
                    state, action, symbol, db_qty, broker_qty,
                    db_position_id, json.dumps(detail) if detail else None,
                )
        except Exception as exc:

            log.warning(_j("reconcile_audit_failed", err=str(exc)))


    async def _exit_price_for_close(
        self, sym: str, current_price: float | None,
        entry_price: float | None,
    ) -> tuple[float, str]:
        """Pick the best available mark for a forced close. Order:
        1. live broker price (ib.reqMktData via broker.latest_trade_price)
        2. last cached current_price
        3. entry_price
        Returns (price, source). Source useful for audit; if all fail,
        returns (0.0, 'zero') which is loud in audit output."""
        try:
            live = await broker.latest_trade_price(self.ib, sym)
            if live and live > 0:
                return float(live), "live"
        except Exception as exc:
            log.warning(_j("exit_price_live_lookup_failed",
                           symbol=sym, err=str(exc)))
        if current_price and float(current_price) > 0:
            return float(current_price), "current_price"
        if entry_price and float(entry_price) > 0:
            return float(entry_price), "entry_price"
        return 0.0, "zero"


    async def _orphan_stop_target(
        self, sym: str, qty: float, avg_cost: float, cfg: dict | None,
    ) -> tuple[float | None, float | None]:
        """ATR(14)-based stop with MIN_STOP_WIDTH_PCT floor; RR=1.0 target.
        Sign-aware. Returns (None, None) on failure (caller logs)."""
        try:
            cfg_ = cfg or {}
            atr_mult = float(cfg_.get("POSITION_RISK_ATR_MULT", 1.5) or 1.5)
            min_width_pct = float(
                cfg_.get("MIN_STOP_WIDTH_PCT", 0.75) or 0.75) / 100.0
            hist = await broker.get_daily_closes(self.ib, sym, lookback_days=30)
            atr14 = (signals.atr(hist.highs, hist.lows, hist.closes, period=14)
                     if hist else None)
            base = float(avg_cost)
            if atr14 and atr14 > 0:
                stop_dist = max(atr_mult * atr14, base * min_width_pct)
            else:
                stop_dist = base * min_width_pct
            if qty > 0:
                return base - stop_dist, base + stop_dist
            return base + stop_dist, base - stop_dist
        except Exception as exc:
            log.warning(_j("orphan_stop_compute_failed",
                           symbol=sym, err=str(exc)))
            return None, None


    async def reconcile_positions(self, cfg: dict | None = None) -> None:
        """Sync DB open positions to IB positions. IB is source of truth.

        Runs every tick at the top. Prevents SELL re-entrancy runaways (DB
        thinks it still has a position that IB already filled out of).
        """
        if not self.ib.isConnected():
            return
        try:
            ibkr_pos = self.ib.positions()
        except Exception as exc:
            log.warning(_j("reconcile_ib_positions_failed", err=str(exc)))
            return





        if not ibkr_pos:
            async with self.pool.acquire() as c:
                db_open_count = await c.fetchval(
                    "SELECT count(*) FROM positions "
                    "WHERE status IN ('open','opening','closing')"
                )
            if int(db_open_count or 0) == 0:
                if self._last_state != "NoPositions":
                    log.info(_j("reconcile_no_open_positions"))
                self._last_state = "NoPositions"
                return

            log.warning(_j("reconcile_empty_positions_skipped"))


            if self._last_state != "EmptyBroker":
                await self._audit("EmptyBroker", "entered")
            self._last_state = "EmptyBroker"
            return
        if self._last_state == "EmptyBroker":
            await self._audit("EmptyBroker", "exited")
        self._last_state = "Active"

        ibkr_state = {
            p.contract.symbol: (float(p.position), float(p.avgCost))
            for p in ibkr_pos
            if abs(float(p.position)) > 1e-9
        }

        async with self.pool.acquire() as c:
            db_rows = await c.fetch(
                "SELECT id, symbol, qty, current_price, entry_price "
                "FROM positions WHERE status IN ('open','opening','closing')"
            )

        closed = qty_adjusted = sign_flipped = inserted = 0

        for r in db_rows:
            sym = r["symbol"]
            db_qty = float(r["qty"] or 0)
            broker_qty, _broker_avg = ibkr_state.get(sym, (0.0, 0.0))

            if abs(broker_qty) < 1e-9:

                exit_px, src = await self._exit_price_for_close(
                    sym, r["current_price"], r["entry_price"])
                if not await entry_price_guard.ensure_entry_price(
                    self.pool, r["id"], sym, context="reconcile_authoritative"
                ):
                    await entry_price_guard.mark_position_error_on_null_entry(
                        self.pool, r["id"], exit_px,
                    )
                    log.error(_j("reconcile_null_entry_marked_error",
                                 id=r["id"], symbol=sym, db_qty=db_qty,
                                 ibkr_qty=broker_qty, exit_price=exit_px))
                    await self._audit("DBOnly", "marked_error",
                                      symbol=sym, db_qty=db_qty,
                                      db_position_id=r["id"],
                                      exit_price=exit_px, exit_source=src)
                    closed += 1
                    continue
                async with self.pool.acquire() as c:
                    await c.execute(
                        "UPDATE positions SET status='closed', "
                        "exit_price=$1, closed_at=NOW() WHERE id=$2",
                        exit_px, r["id"],
                    )
                log.warning(_j("reconcile_closed_db_position",
                               id=r["id"], symbol=sym, db_qty=db_qty,
                               ibkr_qty=broker_qty, exit_price=exit_px))
                await self._audit("DBOnly", "closed", symbol=sym,
                                  db_qty=db_qty, db_position_id=r["id"],
                                  exit_price=exit_px, exit_source=src)
                closed += 1

            elif (db_qty > 0) != (broker_qty > 0):


                exit_px, src = await self._exit_price_for_close(
                    sym, r["current_price"], r["entry_price"])
                async with self.pool.acquire() as c:
                    await c.execute(
                        "UPDATE positions SET status='closed', "
                        "exit_price=$1, closed_at=NOW() WHERE id=$2",
                        exit_px, r["id"],
                    )
                log.warning(_j("reconcile_sign_flip_closed_db",
                               id=r["id"], symbol=sym, db_qty=db_qty,
                               broker_qty=broker_qty, exit_price=exit_px))
                await self._audit("SignFlip", "closed", symbol=sym,
                                  db_qty=db_qty, broker_qty=broker_qty,
                                  db_position_id=r["id"],
                                  exit_price=exit_px, exit_source=src)
                sign_flipped += 1

            elif abs(broker_qty - db_qty) > 1e-9:

                async with self.pool.acquire() as c:
                    await c.execute(
                        "UPDATE positions SET qty=$1 WHERE id=$2",
                        broker_qty, r["id"],
                    )
                log.warning(_j("reconcile_adjusted_qty",
                               id=r["id"], symbol=sym, db_qty=db_qty,
                               ibkr_qty=broker_qty))
                await self._audit("QtyMismatch", "adjusted", symbol=sym,
                                  db_qty=db_qty, broker_qty=broker_qty,
                                  db_position_id=r["id"])
                qty_adjusted += 1




        async with self.pool.acquire() as c:
            still_open = await c.fetch(
                "SELECT symbol FROM positions "
                "WHERE status IN ('open','opening','closing')"
            )
        db_syms = {r["symbol"] for r in still_open}






        slot_ceiling = max(int((cfg or {}).get("MAX_SLOTS", 30) or 30), 30)

        for sym, (qty, avg_cost) in ibkr_state.items():
            if sym in db_syms:
                continue
            if qty <= 0:
                key = (sym, qty)
                if key not in self._seen_short_orphans:
                    log.error(_j("reconcile_broker_short_orphan_skipped",
                                 symbol=sym, qty=qty, avg_cost=avg_cost))
                    await self._audit("BrokerOnly", "skipped_short_orphan",
                                      symbol=sym, broker_qty=qty,
                                      avg_cost=avg_cost)
                    self._seen_short_orphans.add(key)
                continue
            async with self.pool.acquire() as c:
                used = await c.fetch(
                    "SELECT slot FROM positions "
                    "WHERE status IN ('open','opening','closing')"
                )
            used_slots = {int(row["slot"]) for row in used}
            free_slot = next(
                (s for s in range(1, slot_ceiling + 1) if s not in used_slots),
                None,
            )
            if free_slot is None:
                log.error(_j("reconcile_no_free_slot_for_orphan",
                             symbol=sym, qty=qty, avg_cost=avg_cost,
                             slot_ceiling=slot_ceiling))
                await self._audit("BrokerOnly", "skipped_no_slot",
                                  symbol=sym, broker_qty=qty,
                                  avg_cost=avg_cost,
                                  slot_ceiling=slot_ceiling)
                continue

            stop_price, target_price = await self._orphan_stop_target(
                sym, qty, avg_cost, cfg)

            async with self.pool.acquire() as c:
                new_id = await c.fetchval(
                    "INSERT INTO positions "
                    "(symbol, strategy, status, qty, entry_price, opened_at, "
                    " slot, stop_price, target_price) "
                    "VALUES ($1, 'reconciled', 'open', $2, $3, NOW(), "
                    " $4, $5, $6) RETURNING id",
                    sym, qty, avg_cost, free_slot, stop_price, target_price,
                )









            expected_side = "BUY" if qty > 0 else "SELL"
            async with self.pool.acquire() as c:
                relinked = await c.fetch(
                    """UPDATE orders
                       SET position_id = $1, status = 'filled'
                       WHERE id IN (
                         SELECT id FROM orders
                         WHERE position_id IS NULL
                           AND side = $2
                           AND raw->>'symbol' = $3
                           AND ts > NOW() - INTERVAL '15 minutes'
                           AND status IN ('cancelled','rejected','submitted')
                           AND (commission IS NOT NULL OR fill_qty > 0)
                         ORDER BY ts DESC
                         LIMIT 5
                       )
                       RETURNING id, fees, commission""",
                    new_id, expected_side, sym,
                )
            if relinked:
                log.warning(_j("reconcile_relinked_unlinked_orders",
                               position_id=new_id, symbol=sym,
                               relinked_order_ids=[r["id"] for r in relinked],
                               total_commission=sum(
                                   float(r["commission"] or 0) for r in relinked)))

            log.warning(_j("reconcile_inserted_broker_orphan",
                           id=new_id, symbol=sym, qty=qty,
                           entry_price=avg_cost, slot=free_slot,
                           stop_price=stop_price, target_price=target_price,
                           relinked=len(relinked)))
            await self._audit("BrokerOnly", "inserted",
                              symbol=sym, broker_qty=qty,
                              db_position_id=new_id,
                              avg_cost=avg_cost, slot=free_slot,
                              stop_price=stop_price,
                              target_price=target_price,
                              relinked_orders=[r["id"] for r in relinked])
            inserted += 1








        try:
            async with self.pool.acquire() as c:
                healed = await c.fetch(
                    """
                    WITH candidates AS (
                      SELECT DISTINCT ON (p.id, o.id)
                        p.id AS pos_id, o.id AS order_id, o.commission
                      FROM positions p
                      JOIN orders o
                        ON o.position_id IS NULL
                       AND o.raw->>'symbol' = p.symbol
                       AND o.side = CASE WHEN p.qty > 0 THEN 'BUY' ELSE 'SELL' END
                       AND o.status IN ('cancelled','rejected','submitted')
                       AND (o.commission IS NOT NULL OR o.fill_qty > 0)
                       AND o.ts BETWEEN p.opened_at - INTERVAL '5 minutes'
                                    AND p.opened_at + INTERVAL '5 minutes'
                      WHERE p.status IN ('open','opening','closing')
                    )
                    UPDATE orders
                    SET position_id = candidates.pos_id, status = 'filled'
                    FROM candidates
                    WHERE orders.id = candidates.order_id
                    RETURNING orders.id, orders.position_id, orders.commission
                    """,
                )
            if healed:
                by_pos: dict[int, list[int]] = {}
                for r in healed:
                    by_pos.setdefault(r["position_id"], []).append(r["id"])
                log.warning(_j("reconcile_healed_unlinked_orders",
                               positions=len(by_pos),
                               orders=len(healed),
                               by_position=by_pos))
                for pos_id, oids in by_pos.items():
                    await self._audit("Heal", "relinked",
                                      db_position_id=pos_id,
                                      relinked_orders=oids)
        except Exception as exc:
            log.warning(_j("reconcile_heal_sweep_failed", err=str(exc)))

        if closed or qty_adjusted or sign_flipped or inserted:
            log.info(_j("reconcile_summary",
                        closed=closed, qty_adjusted=qty_adjusted,
                        sign_flipped=sign_flipped, inserted=inserted,
                        ibkr_count=len(ibkr_state),
                        db_count=len(db_rows)))
