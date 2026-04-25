"""Tests for LLM hypothesis sources that DO NOT actually call OpenRouter.

Patches optimizer.llm.chat to return canned responses. Verifies shape
handling + DB insertion + validation.
"""
from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock

import asyncpg

DSN = os.environ.get("TRADING_TEST_DSN")


@unittest.skipUnless(DSN, "TRADING_TEST_DSN not set")
class LLMHypothesisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        async with self.pool.acquire() as c:
            await c.execute(
                """TRUNCATE trade_outcomes, positions, tuning_proposals,
                     optimizer_findings, config_values, config_versions
                     RESTART IDENTITY CASCADE"""
            )
            vid = await c.fetchval(
                """INSERT INTO config_versions
                   (created_by, source, rationale, activated_at, activated_by, scope)
                   VALUES ('t','bootstrap','s',NOW(),'t','{"kind":"global"}'::jsonb)
                   RETURNING id"""
            )
            for k, v in (("QUANT_SCORE_MIN", 50),
                          ("RSI_BUY_THRESHOLD", 30),
                          ("SIGMA_BELOW_SMA20", 1.0),
                          ("TARGET_PROFIT_PCT", 1.0)):
                await c.execute(
                    "INSERT INTO config_values (version_id,key,value) VALUES ($1,$2,$3::jsonb)",
                    vid, k, json.dumps(v),
                )

    async def asyncTearDown(self):
        await self.pool.close()

    async def _seed_losers(self, n=15):
        async with self.pool.acquire() as c:
            for i in range(n):
                await c.execute(
                    """INSERT INTO positions
                       (id, symbol, slot, status, entry_price, exit_price, qty, opened_at, closed_at)
                       VALUES ($1,$2,10,'closed',100,99,1,$3,$4)""",
                    10000 + i, f"L{i}",
                    datetime.now(timezone.utc) - timedelta(hours=i + 2),
                    datetime.now(timezone.utc) - timedelta(hours=i + 1),
                )
                await c.execute(
                    """INSERT INTO trade_outcomes
                       (position_id, symbol, slot_id, strategy, entry_price,
                        exit_price, qty, opened_at, closed_at, hold_seconds,
                        gross_pnl_eur, fees_eur, net_pnl_eur, net_pnl_pct,
                        exit_reason, config_version_id, defn_version,
                        entry_rsi, entry_score, entry_regime)
                       VALUES ($1,$2,10,'intraday',100,99,1,$3,$4,3600,
                               -1,0.5,-1.5,-1.0,'stop',0,1,22,52,'risk_off')""",
                    10000 + i, f"L{i}",
                    datetime.now(timezone.utc) - timedelta(hours=i + 2),
                    datetime.now(timezone.utc) - timedelta(hours=i + 1),
                )

    async def test_failure_cluster_valid_proposal(self):
        await self._seed_losers()
        canned = {
            "cluster_summary": "15 losers with score around 52 in risk_off",
            "proposals": [{"key": "QUANT_SCORE_MIN", "from": 50, "to": 55,
                            "reason": "reject this cluster"}],
        }
        with patch("optimizer.hypothesis.llm_failure.chat",
                    new=AsyncMock(return_value=canned)):
            from optimizer.hypothesis.llm_failure import propose
            ids = await propose(self.pool)
        self.assertEqual(len(ids), 1)
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT source, status, proposal FROM tuning_proposals WHERE id=$1", ids[0],
            )
        self.assertEqual(row["source"], "llm_failure")
        self.assertEqual(row["status"], "pending")

    async def test_failure_cluster_drops_unknown_keys(self):
        await self._seed_losers()
        canned = {
            "cluster_summary": "…",
            "proposals": [{"key": "BOGUS_KEY", "from": 1, "to": 2,
                            "reason": "x"}],
        }
        with patch("optimizer.hypothesis.llm_failure.chat",
                    new=AsyncMock(return_value=canned)):
            from optimizer.hypothesis.llm_failure import propose
            ids = await propose(self.pool)
        self.assertEqual(ids, [])

    async def test_strategic_review_emits_finding_and_proposal(self):
        # seed enough trades for summary.n >= 20
        async with self.pool.acquire() as c:
            for i in range(25):
                await c.execute(
                    """INSERT INTO positions
                       (id, symbol, slot, status, entry_price, exit_price, qty, opened_at, closed_at)
                       VALUES ($1,$2,11,'closed',100,101,1,$3,$4)""",
                    20000 + i, f"W{i}",
                    datetime.now(timezone.utc) - timedelta(hours=i + 2),
                    datetime.now(timezone.utc) - timedelta(hours=i + 1),
                )
                await c.execute(
                    """INSERT INTO trade_outcomes
                       (position_id, symbol, slot_id, strategy, entry_price,
                        exit_price, qty, opened_at, closed_at, hold_seconds,
                        gross_pnl_eur, fees_eur, net_pnl_eur, net_pnl_pct,
                        exit_reason, config_version_id, defn_version,
                        entry_rsi, entry_score, entry_regime,
                        entry_day_of_week)
                       VALUES ($1,$2,11,'intraday',100,101,1,$3,$4,3600,
                               1,0.5,0.5,1.0,'target',0,1,22,60,'momentum',2)""",
                    20000 + i, f"W{i}",
                    datetime.now(timezone.utc) - timedelta(hours=i + 2),
                    datetime.now(timezone.utc) - timedelta(hours=i + 1),
                )
        canned = {
            "findings": [{"subject": "dow_skew", "body": "only trades on Wed",
                            "severity": "info"}],
            "proposals": [{"key": "TARGET_PROFIT_PCT", "from": 1.0, "to": 1.1,
                            "reason": "momentum runs further"}],
        }
        with patch("optimizer.hypothesis.llm_strategic.chat",
                    new=AsyncMock(return_value=canned)):
            from optimizer.hypothesis.llm_strategic import propose
            out = await propose(self.pool)
        self.assertEqual(len(out["findings"]), 1)
        self.assertEqual(len(out["proposals"]), 1)


if __name__ == "__main__":
    unittest.main()
