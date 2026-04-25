"""Scheduler + anomaly integration tests."""
from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone

import asyncpg

from optimizer.anomaly.detector import scan

DSN = os.environ.get("TRADING_TEST_DSN")


@unittest.skipUnless(DSN, "TRADING_TEST_DSN not set")
class AnomalyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        async with self.pool.acquire() as c:
            await c.execute(
                """TRUNCATE metrics_slot_rolling, metrics_regime_rolling,
                     signal_snapshots, optimizer_findings
                     RESTART IDENTITY CASCADE"""
            )

    async def asyncTearDown(self):
        await self.pool.close()

    async def _seed_slot_metric(self, *, slot, window, n, pf, dd):
        async with self.pool.acquire() as c:
            await c.execute(
                """INSERT INTO metrics_slot_rolling
                   (slot_id, window_days, as_of_date, config_version_id,
                    defn_version, n_samples, win_rate, profit_factor,
                    expectancy_bps, fees_eur, gross_pnl_eur, net_pnl_eur,
                    max_dd_pct)
                   VALUES ($1,$2,CURRENT_DATE,0,1,$3,0.5,$4,0,0,0,0,$5)""",
                slot, window, n, pf, dd,
            )

    async def test_drawdown_triggers_finding(self):
        await self._seed_slot_metric(
            slot=10, window=7, n=12, pf=0.5, dd=8.0,
        )
        await scan(self.pool)
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT detector, severity FROM optimizer_findings"
            )
        names = {r["detector"] for r in rows}
        self.assertIn("drawdown_breach", names)

    async def test_pf_regression_finding(self):
        await self._seed_slot_metric(slot=11, window=7,  n=30, pf=0.6, dd=2.0)
        await self._seed_slot_metric(slot=11, window=30, n=90, pf=1.5, dd=3.0)
        await scan(self.pool)
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT detector FROM optimizer_findings"
            )
        self.assertIn("pf_regression", {r["detector"] for r in rows})

    async def test_data_quality_stale_snapshots(self):
        # No signal_snapshots rows -> detector does nothing (nothing to
        # be stale about). Insert one 10h old.
        async with self.pool.acquire() as c:
            await c.execute(
                """INSERT INTO signal_snapshots
                   (symbol, strategy, slot_id, snapshot_ts, gate_outcome)
                   VALUES ('X','intraday',10,$1,'skip')""",
                datetime.now(timezone.utc) - timedelta(hours=10),
            )
        await scan(self.pool)
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT subject FROM optimizer_findings WHERE detector='data_quality'"
            )
        self.assertTrue(any("stale" in r["subject"] for r in rows))

    async def test_dedup_within_window(self):
        await self._seed_slot_metric(slot=10, window=7, n=12, pf=0.5, dd=8.0)
        await scan(self.pool)
        await scan(self.pool)   # second call shouldn't create duplicates
        async with self.pool.acquire() as c:
            count = await c.fetchval(
                "SELECT COUNT(*) FROM optimizer_findings WHERE detector='drawdown_breach'"
            )
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
