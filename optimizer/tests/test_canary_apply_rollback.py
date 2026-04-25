"""Canary + apply + rollback end-to-end."""
from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone

import asyncpg

from optimizer.canary import (
    start_canary, evaluate_canary, CanaryConfig,
    PASS as CANARY_PASS, FAIL as CANARY_FAIL, RUNNING as CANARY_RUNNING,
)
from optimizer.lifecycle.apply import apply_canary_globally
from optimizer.lifecycle.rollback import check_and_maybe_rollback, rollback_global
from optimizer.config_store.versions import (
    active_global_version, list_active_canaries,
)

DSN = os.environ.get("TRADING_TEST_DSN")


@unittest.skipUnless(DSN, "TRADING_TEST_DSN not set")
class CanaryApplyRollbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        async with self.pool.acquire() as c:
            await c.execute(
                """TRUNCATE trade_outcomes, positions, canary_assignments,
                     apply_events, rollback_events, tuning_proposals,
                     config_values, config_versions,
                     signal_snapshots
                     RESTART IDENTITY CASCADE"""
            )
            # Bootstrap baseline version
            self.baseline_id = await c.fetchval(
                """INSERT INTO config_versions
                   (created_by, source, rationale, activated_at,
                    activated_by, scope)
                   VALUES ('test','bootstrap','seed',NOW(),'test',
                           '{"kind":"global"}'::jsonb)
                   RETURNING id"""
            )
            await c.execute(
                """INSERT INTO config_values (version_id, key, value)
                   VALUES ($1,'QUANT_SCORE_MIN','50'::jsonb),
                          ($1,'TARGET_PROFIT_PCT','1.0'::jsonb)""",
                self.baseline_id,
            )
            # Seed proposal
            self.proposal_id = await c.fetchval(
                """INSERT INTO tuning_proposals
                   (proposal, rationale, source, status)
                   VALUES ('{}'::jsonb,'t','numerical','validated')
                   RETURNING id"""
            )

    async def asyncTearDown(self):
        await self.pool.close()

    async def _insert_trade(self, *, pos_id, slot, net_pct, net_eur,
                              cfg_vid, opened_at=None, closed_at=None):
        opened_at = opened_at or (datetime.now(timezone.utc) - timedelta(hours=2))
        closed_at = closed_at or (datetime.now(timezone.utc) - timedelta(hours=1))
        async with self.pool.acquire() as c:
            await c.execute(
                """INSERT INTO positions
                   (id, symbol, slot, status, entry_price, exit_price, qty, opened_at, closed_at)
                   VALUES ($1,$2,$3,'closed',100,$4,1,$5,$6)
                   ON CONFLICT (id) DO NOTHING""",
                pos_id, f"S{pos_id}", slot, 100 + net_eur, opened_at, closed_at,
            )
            await c.execute(
                """INSERT INTO trade_outcomes
                   (position_id, symbol, slot_id, strategy, entry_price,
                    exit_price, qty, opened_at, closed_at, hold_seconds,
                    gross_pnl_eur, fees_eur, net_pnl_eur, net_pnl_pct,
                    exit_reason, config_version_id, defn_version)
                   VALUES ($1,$2,$3,'intraday',100,$4,1,$5,$6,3600,
                           $7,0.5,$8,$9,'target',$10,1)
                   ON CONFLICT (position_id) DO NOTHING""",
                pos_id, f"S{pos_id}", slot,
                100 + net_eur, opened_at, closed_at,
                net_eur + 0.5, net_eur, net_pct, cfg_vid,
            )

    async def test_start_canary_then_pass_then_apply(self):
        cid = await start_canary(
            self.pool,
            proposal_id=self.proposal_id,
            baseline_version_id=self.baseline_id,
            candidate_values={"QUANT_SCORE_MIN": 55},
            slot_ids=[10, 11],
        )
        # Simulate 40 winning trades on canary slots, 40 losing on baseline.
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT canary_version_id, started_at FROM canary_assignments WHERE id=$1",
                cid,
            )
            canary_vid = row["canary_version_id"]
            started = row["started_at"]
        for i in range(40):
            await self._insert_trade(
                pos_id=1000 + i, slot=10, net_pct=1.0, net_eur=10,
                cfg_vid=canary_vid,
                opened_at=started + timedelta(minutes=i),
                closed_at=started + timedelta(minutes=i + 10),
            )
        for i in range(40):
            await self._insert_trade(
                pos_id=2000 + i, slot=15, net_pct=-0.5, net_eur=-5,
                cfg_vid=self.baseline_id,
                opened_at=started + timedelta(minutes=i),
                closed_at=started + timedelta(minutes=i + 10),
            )
        verdict = await evaluate_canary(self.pool, canary_id=cid)
        self.assertEqual(verdict.status, CANARY_PASS)

        new_global = await apply_canary_globally(
            self.pool, canary_id=cid, applied_by="test",
        )
        active = await active_global_version(self.pool)
        self.assertEqual(active["id"], new_global)
        # Canary version deactivated.
        canaries = await list_active_canaries(self.pool)
        self.assertEqual(canaries, [])
        # apply_events row present.
        async with self.pool.acquire() as c:
            ap = await c.fetchrow(
                "SELECT * FROM apply_events WHERE canary_id=$1", cid,
            )
            self.assertIsNotNone(ap)

    async def test_canary_failed_on_drawdown(self):
        cid = await start_canary(
            self.pool,
            proposal_id=self.proposal_id,
            baseline_version_id=self.baseline_id,
            candidate_values={"QUANT_SCORE_MIN": 55},
            slot_ids=[12],
        )
        async with self.pool.acquire() as c:
            canary_vid = await c.fetchval(
                "SELECT canary_version_id FROM canary_assignments WHERE id=$1",
                cid,
            )
            started = await c.fetchval(
                "SELECT started_at FROM canary_assignments WHERE id=$1", cid,
            )
        # 40 catastrophic losses on canary slot -> DD breach.
        for i in range(40):
            await self._insert_trade(
                pos_id=3000 + i, slot=12, net_pct=-2.0, net_eur=-20,
                cfg_vid=canary_vid,
                opened_at=started + timedelta(minutes=i),
                closed_at=started + timedelta(minutes=i + 10),
            )
        # Baseline mildly positive.
        for i in range(30):
            await self._insert_trade(
                pos_id=4000 + i, slot=15, net_pct=0.1, net_eur=1,
                cfg_vid=self.baseline_id,
                opened_at=started + timedelta(minutes=i),
                closed_at=started + timedelta(minutes=i + 10),
            )
        verdict = await evaluate_canary(self.pool, canary_id=cid)
        self.assertEqual(verdict.status, CANARY_FAIL)

    async def test_rollback_on_pf_regression(self):
        # Apply a new global, then simulate bad trades under it and assert
        # check_and_maybe_rollback flips back to baseline.
        # 1) Baseline period — already bootstrapped in setUp
        # 2) Promote a new global
        cid = await start_canary(
            self.pool,
            proposal_id=self.proposal_id,
            baseline_version_id=self.baseline_id,
            candidate_values={"QUANT_SCORE_MIN": 55},
            slot_ids=[13],
        )
        async with self.pool.acquire() as c:
            canary_vid = await c.fetchval(
                "SELECT canary_version_id FROM canary_assignments WHERE id=$1",
                cid,
            )
            started = await c.fetchval(
                "SELECT started_at FROM canary_assignments WHERE id=$1", cid,
            )
        # Winning canary + losing baseline -> canary passes.
        for i in range(40):
            await self._insert_trade(
                pos_id=5000 + i, slot=13, net_pct=0.5, net_eur=5,
                cfg_vid=canary_vid,
                opened_at=started + timedelta(minutes=i),
                closed_at=started + timedelta(minutes=i + 10),
            )
        for i in range(30):
            await self._insert_trade(
                pos_id=6000 + i, slot=15, net_pct=-0.1, net_eur=-1,
                cfg_vid=self.baseline_id,
                opened_at=started + timedelta(minutes=i),
                closed_at=started + timedelta(minutes=i + 10),
            )
        verdict = await evaluate_canary(self.pool, canary_id=cid)
        self.assertEqual(verdict.status, CANARY_PASS)
        new_global = await apply_canary_globally(
            self.pool, canary_id=cid, applied_by="test",
        )

        # 3) Now simulate trades under new_global: catastrophic PF.
        # Baseline period (parent) had +0.5 wins / -0.1 losses -> PF ~ (0.5*40)/(0.1*30)=6.7
        # Post-apply window: all losses -> PF = 0 -> >25% drop -> rollback.
        for i in range(20):
            await self._insert_trade(
                pos_id=7000 + i, slot=10, net_pct=-1.0, net_eur=-10,
                cfg_vid=new_global,
                opened_at=datetime.now(timezone.utc) - timedelta(hours=2),
                closed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
        # ALSO seed the parent with enough trades across the same window so
        # baseline metric isn't zero. Use _actual parent_id_, which is the
        # old baseline_id.
        for i in range(20):
            await self._insert_trade(
                pos_id=8000 + i, slot=15, net_pct=0.5, net_eur=5,
                cfg_vid=self.baseline_id,
                opened_at=datetime.now(timezone.utc) - timedelta(hours=6),
                closed_at=datetime.now(timezone.utc) - timedelta(hours=5),
            )

        rolled = await check_and_maybe_rollback(self.pool)
        self.assertIsNotNone(rolled)
        active = await active_global_version(self.pool)
        self.assertEqual(active["id"], rolled)
        async with self.pool.acquire() as c:
            ev = await c.fetchrow(
                "SELECT * FROM rollback_events ORDER BY id DESC LIMIT 1"
            )
            self.assertIsNotNone(ev)


if __name__ == "__main__":
    unittest.main()
