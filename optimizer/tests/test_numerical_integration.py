"""End-to-end: numerical search over seeded snapshots should produce
a tuning_proposals row with sensible content."""
from __future__ import annotations

import json
import os
import random
import unittest
from datetime import datetime, timedelta, timezone

import asyncpg

from optimizer.hypothesis.numerical import search, propose

DSN = os.environ.get("TRADING_TEST_DSN")


@unittest.skipUnless(DSN, "TRADING_TEST_DSN not set")
class NumericalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        async with self.pool.acquire() as c:
            await c.execute(
                """TRUNCATE signal_snapshots, tuning_proposals,
                     config_values, config_versions
                     RESTART IDENTITY CASCADE"""
            )
            # Bootstrap config_versions row with baseline thresholds.
            vid = await c.fetchval(
                """INSERT INTO config_versions
                   (created_by, source, rationale, activated_at, activated_by, scope)
                   VALUES ('test','bootstrap','seed',NOW(),'test','{"kind":"global"}'::jsonb)
                   RETURNING id"""
            )
            for k, v in (("QUANT_SCORE_MIN", 50),
                          ("RSI_BUY_THRESHOLD", 30),
                          ("SIGMA_BELOW_SMA20", 1.0)):
                await c.execute(
                    """INSERT INTO config_values (version_id, key, value)
                       VALUES ($1,$2,$3::jsonb)""",
                    vid, k, json.dumps(v),
                )

    async def asyncTearDown(self):
        await self.pool.close()

    async def _seed_good_dataset(self, n=400):
        rng = random.Random(7)
        async with self.pool.acquire() as c:
            for i in range(n):
                score = rng.uniform(40, 80)
                # High-score trades win, low-score trades lose.
                outcome = (score - 50) / 2.0 + rng.uniform(-0.25, 0.25)
                await c.execute(
                    """INSERT INTO signal_snapshots
                       (symbol, strategy, slot_id, snapshot_ts, score, rsi,
                        sigma_below_sma20, gate_outcome, stock_regime,
                        hypothetical_outcome_pct)
                       VALUES ($1,'intraday',10,$2,$3,22,1.5,'skip','momentum',$4)""",
                    f"SYM{i}",
                    datetime.now(timezone.utc) - timedelta(hours=i + 1),
                    score, outcome,
                )

    async def test_search_finds_improvement(self):
        await self._seed_good_dataset()
        result = await search(self.pool, lookback_days=60, n_trials=40)
        self.assertIsNotNone(result)
        self.assertGreater(result["best_value"], 0)
        # Candidate should raise QUANT_SCORE_MIN (from 50) to filter losers.
        self.assertGreater(
            float(result["candidate"]["QUANT_SCORE_MIN"]),
            float(result["baseline"]["QUANT_SCORE_MIN"]),
        )

    async def test_propose_writes_row(self):
        await self._seed_good_dataset()
        pid = await propose(self.pool, lookback_days=60, n_trials=40)
        self.assertIsNotNone(pid)
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT source, status, proposal, rationale FROM tuning_proposals WHERE id=$1",
                pid,
            )
        self.assertEqual(row["source"], "numerical")
        self.assertEqual(row["status"], "pending")
        proposal = row["proposal"] if isinstance(row["proposal"], dict) else json.loads(row["proposal"])
        self.assertEqual(proposal["generator"], "numerical.tpe")
        self.assertGreater(len(proposal["proposals"]), 0)

    async def test_search_returns_none_on_insufficient_data(self):
        # Empty snapshots
        result = await search(self.pool, lookback_days=30, n_trials=20)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
