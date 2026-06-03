"""Meta-learner test: writes a row, summary references the right numbers."""
from __future__ import annotations

import os
import unittest

import asyncpg

from optimizer.meta.report import generate_weekly

DSN = os.environ.get("TRADING_TEST_DSN")


@unittest.skipUnless(DSN, "TRADING_TEST_DSN not set")
class MetaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        async with self.pool.acquire() as c:
            await c.execute(
                """TRUNCATE optimizer_meta_reports, tuning_proposals,
                     rollback_events, llm_spend, trade_outcomes
                     RESTART IDENTITY CASCADE"""
            )

    async def asyncTearDown(self):
        await self.pool.close()

    async def test_empty_week_produces_row(self):
        rid = await generate_weekly(self.pool)
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT summary, report FROM optimizer_meta_reports WHERE id=$1", rid,
            )
        self.assertIsNotNone(row)
        self.assertIn("No applied proposals", row["summary"])

    async def test_idempotent_same_week(self):
        r1 = await generate_weekly(self.pool)
        r2 = await generate_weekly(self.pool)
        self.assertEqual(r1, r2)


if __name__ == "__main__":
    unittest.main()
