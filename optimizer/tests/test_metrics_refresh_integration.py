"""Integration test: refresh rolling metrics against a real Postgres.

Requires TRADING_TEST_DSN env var pointing at a disposable DB that has
had the full migration chain applied (up to 034). Skips otherwise.
Writes synthetic trade_outcomes rows + runs refresh + asserts the
bookkeeping and numeric results.
"""
from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone

import asyncpg

from optimizer.metrics.refresh import (
    refresh_slot_rolling, refresh_regime_rolling, refresh_llm_rolling,
)

DSN = os.environ.get("TRADING_TEST_DSN")


def _init(pool_conn):
    import json
    return pool_conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


@unittest.skipUnless(DSN, "TRADING_TEST_DSN not set")
class RefreshIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        async with self.pool.acquire() as c:
            # Fully reset optimizer-owned tables. Positions/orders we also
            # clear because the trigger would otherwise fire on reuse.
            await c.execute("TRUNCATE trade_outcomes, metrics_slot_rolling, "
                            "metrics_regime_rolling, metrics_tod_rolling, "
                            "metrics_symbol_rolling, metrics_llm_rolling, "
                            "metrics_refresh_state RESTART IDENTITY CASCADE")

    async def asyncTearDown(self):
        await self.pool.close()

    async def _insert_trade(self, *, pos_id: int, slot: int, closed_at,
                             net_pct: float, net_eur: float,
                             regime: str = "momentum"):
        async with self.pool.acquire() as c:
            # Create a minimal position row first (FK parent).
            await c.execute(
                """INSERT INTO positions
                   (id, symbol, slot, status, entry_price, exit_price, qty,
                    opened_at, closed_at)
                   VALUES ($1,$2,$3,'closed',100.0,$4,1.0,$5,$6)
                   ON CONFLICT (id) DO NOTHING""",
                pos_id, f"SYM{pos_id}", slot, 100.0 + net_eur,
                closed_at - timedelta(hours=1), closed_at,
            )
            await c.execute(
                """INSERT INTO trade_outcomes
                   (position_id, symbol, slot_id, strategy, entry_price,
                    exit_price, qty, opened_at, closed_at, hold_seconds,
                    gross_pnl_eur, fees_eur, net_pnl_eur, net_pnl_pct,
                    exit_reason, entry_regime, config_version_id, defn_version)
                   VALUES ($1,$2,$3,'intraday',100.0,$4,1.0,$5,$6,3600,
                           $7,0.5,$8,$9,'target',$10,0,1)
                   ON CONFLICT (position_id) DO NOTHING""",
                pos_id, f"SYM{pos_id}", slot, 100.0 + net_eur,
                closed_at - timedelta(hours=1), closed_at,
                net_eur + 0.5, net_eur, net_pct, regime,
            )

    async def test_refresh_writes_state_and_rows(self):
        now = datetime.now(timezone.utc)
        # 5 wins + 3 losses for slot 10 within last 7d
        for i in range(5):
            await self._insert_trade(
                pos_id=100 + i, slot=10, closed_at=now - timedelta(days=1),
                net_pct=1.0, net_eur=10.0,
            )
        for i in range(3):
            await self._insert_trade(
                pos_id=200 + i, slot=10, closed_at=now - timedelta(days=2),
                net_pct=-0.5, net_eur=-5.0,
            )

        written = await refresh_slot_rolling(self.pool, as_of=now)
        self.assertGreater(written, 0)

        async with self.pool.acquire() as c:
            state = await c.fetchrow(
                "SELECT * FROM metrics_refresh_state WHERE table_name='metrics_slot_rolling'"
            )
            self.assertIsNotNone(state)
            self.assertIsNone(state["last_error"])

            row = await c.fetchrow(
                """SELECT * FROM metrics_slot_rolling
                   WHERE slot_id=10 AND window_days=7
                     AND config_version_id=0"""
            )
            self.assertIsNotNone(row)
            self.assertEqual(row["n_samples"], 8)
            self.assertAlmostEqual(float(row["win_rate"]), 5.0 / 8, places=3)
            # 5*1 gross win = 5 / 3*0.5 gross loss = 1.5 -> PF ~ 3.33
            self.assertAlmostEqual(float(row["profit_factor"]), 5.0 / 1.5, places=2)

    async def test_refresh_idempotent(self):
        now = datetime.now(timezone.utc)
        await self._insert_trade(pos_id=300, slot=11,
                                   closed_at=now - timedelta(hours=12),
                                   net_pct=0.5, net_eur=5.0)
        await refresh_slot_rolling(self.pool, as_of=now)
        await refresh_slot_rolling(self.pool, as_of=now)
        async with self.pool.acquire() as c:
            count = await c.fetchval(
                "SELECT COUNT(*) FROM metrics_slot_rolling WHERE slot_id=11"
            )
            # One row per window_days (3) at same as_of_date, config=0 -> 3 rows
            self.assertEqual(count, 3)

    async def test_regime_refresh(self):
        now = datetime.now(timezone.utc)
        for i in range(4):
            await self._insert_trade(
                pos_id=400 + i, slot=12, closed_at=now - timedelta(days=1),
                net_pct=1.0, net_eur=10.0, regime="momentum",
            )
        for i in range(2):
            await self._insert_trade(
                pos_id=500 + i, slot=12, closed_at=now - timedelta(days=1),
                net_pct=-1.0, net_eur=-10.0, regime="risk_off",
            )
        written = await refresh_regime_rolling(self.pool, as_of=now)
        self.assertGreater(written, 0)
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                """SELECT regime, n_samples, win_rate FROM metrics_regime_rolling
                   WHERE slot_id=12 AND window_days=7 AND config_version_id=0
                   ORDER BY regime"""
            )
        regimes = {r["regime"]: (r["n_samples"], float(r["win_rate"])) for r in rows}
        self.assertEqual(regimes["momentum"], (4, 1.0))
        self.assertEqual(regimes["risk_off"], (2, 0.0))


if __name__ == "__main__":
    unittest.main()
