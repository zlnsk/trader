"""End-to-end adversary tests against trading_test DB.

Seeds realistic signal_snapshots + trade_outcomes and runs
validate_proposal for:
  - deliberately bad proposal (should reject)
  - good proposal (should pass)
  - post-rollback cooldown (should reject)
  - sample-size under-supply (should reject)
"""
from __future__ import annotations

import json
import os
import random
import unittest
from datetime import datetime, timedelta, timezone

import asyncpg

from optimizer.validator.adversary import (
    validate_proposal, PASS, REJECT, MARGINAL,
)

DSN = os.environ.get("TRADING_TEST_DSN")


@unittest.skipUnless(DSN, "TRADING_TEST_DSN not set")
class ValidatorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        async with self.pool.acquire() as c:
            await c.execute(
                """TRUNCATE signal_snapshots, trade_outcomes,
                     rollback_events, tuning_proposals,
                     canary_assignments, apply_events,
                     config_values, config_versions
                     RESTART IDENTITY CASCADE"""
            )
            # Bootstrap config_version so FK chain works.
            await c.execute(
                """INSERT INTO config_versions
                   (created_by, source, rationale, activated_at, activated_by, scope)
                   VALUES ('test','bootstrap','seed',NOW(),'test','{"kind":"global"}'::jsonb)"""
            )

    async def asyncTearDown(self):
        await self.pool.close()

    async def _insert_snapshot(self, *, i: int, score: float, rsi: float,
                                 outcome_pct: float, regime: str = "momentum",
                                 ts: datetime | None = None):
        ts = ts or datetime.now(timezone.utc) - timedelta(hours=i + 1)
        async with self.pool.acquire() as c:
            await c.execute(
                """INSERT INTO signal_snapshots
                   (symbol, strategy, slot_id, snapshot_ts, score, rsi,
                    sigma_below_sma20, gate_outcome, stock_regime,
                    hypothetical_outcome_pct)
                   VALUES ($1,'intraday',10,$2,$3,$4,1.5,'skip',$5,$6)""",
                f"SYM{i}", ts, score, rsi, regime, outcome_pct,
            )

    async def _create_proposal(self) -> int:
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                """INSERT INTO tuning_proposals (proposal, rationale, source, status)
                   VALUES ($1::jsonb,'test','numerical','pending') RETURNING id""",
                json.dumps({"proposals": [{"key": "QUANT_SCORE_MIN", "to": 55}]}),
            )
        return int(row["id"])

    async def test_rejects_on_low_sample(self):
        # Only 10 snapshots — below MIN_N_SAMPLES=50.
        for i in range(10):
            await self._insert_snapshot(
                i=i, score=60, rsi=20, outcome_pct=1.0,
            )
        pid = await self._create_proposal()
        v = await validate_proposal(
            self.pool, proposal_id=pid,
            baseline={"QUANT_SCORE_MIN": 50},
            candidate={"QUANT_SCORE_MIN": 55},
        )
        self.assertEqual(v.overall, REJECT)
        self.assertEqual(v.reason, "sample_size")

    async def test_rejects_bad_proposal_no_improvement(self):
        rng = random.Random(1)
        # 120 snapshots, mix of winners and losers uncorrelated with score.
        for i in range(120):
            await self._insert_snapshot(
                i=i, score=55 + rng.uniform(-5, 5), rsi=20,
                outcome_pct=rng.choice([1.0, -1.0, 0.5, -0.5]),
            )
        pid = await self._create_proposal()
        # Candidate raises QUANT_SCORE_MIN from 50 -> 55, but outcomes aren't
        # correlated with score. Validator should reject at replay_improves.
        v = await validate_proposal(
            self.pool, proposal_id=pid,
            baseline={"QUANT_SCORE_MIN": 50},
            candidate={"QUANT_SCORE_MIN": 55},
        )
        self.assertEqual(v.overall, REJECT)

    async def test_accepts_good_proposal(self):
        # Construct a dataset where low-score trades lose and high-score
        # trades win. Candidate raises QUANT_SCORE_MIN -> filters losers.
        # Big effect size (outcome strongly tracks score) so the 95% CI
        # excludes zero.
        rng = random.Random(2)
        # Alternate regimes so regime gate sees both, with enough per-regime.
        regimes = ["momentum", "mean_reversion"]
        for i in range(400):
            score = rng.uniform(40, 80)
            base_pct = (score - 50) / 2.0        # 50 -> 0, 80 -> 15
            outcome = base_pct + rng.uniform(-0.25, 0.25)
            await self._insert_snapshot(
                i=i, score=score, rsi=20, outcome_pct=outcome,
                regime=regimes[i % 2],
                ts=datetime.now(timezone.utc) - timedelta(hours=i + 1),
            )
        pid = await self._create_proposal()
        v = await validate_proposal(
            self.pool, proposal_id=pid,
            baseline={"QUANT_SCORE_MIN": 50},
            candidate={"QUANT_SCORE_MIN": 55},   # within 15% of 50? 55/50=10% cap
        )
        if v.overall != PASS:
            for g in v.gates:
                print("GATE", g.name, g.verdict, g.detail)
            print("REASON", v.reason, "n_b", v.n_baseline, "n_c", v.n_candidate)
        self.assertEqual(v.overall, PASS)

    async def test_post_rollback_cooldown_blocks(self):
        # Seed a recent rollback event that rolled back a version
        # changing QUANT_SCORE_MIN.
        async with self.pool.acquire() as c:
            bad = await c.fetchval(
                """INSERT INTO config_versions (created_by,source,rationale,scope)
                   VALUES ('test','numerical','bad','{"kind":"global"}'::jsonb)
                   RETURNING id"""
            )
            good = await c.fetchval(
                """INSERT INTO config_versions (created_by,source,rationale,scope)
                   VALUES ('test','rollback','good','{"kind":"global"}'::jsonb)
                   RETURNING id"""
            )
            await c.execute(
                """INSERT INTO config_values (version_id,key,value)
                   VALUES ($1,'QUANT_SCORE_MIN','60'::jsonb)""", bad,
            )
            await c.execute(
                """INSERT INTO rollback_events (bad_version_id,
                     rolled_back_to_id, trigger, triggered_by)
                   VALUES ($1,$2,'pf_regression','test')""",
                bad, good,
            )
        # Seed snapshots so sample_size + replay don't reject first.
        rng = random.Random(3)
        for i in range(200):
            await self._insert_snapshot(
                i=i, score=55, rsi=20,
                outcome_pct=rng.choice([1.0, -1.0, 0.5, -0.5]),
            )
        pid = await self._create_proposal()
        v = await validate_proposal(
            self.pool, proposal_id=pid,
            baseline={"QUANT_SCORE_MIN": 50},
            candidate={"QUANT_SCORE_MIN": 55},
        )
        self.assertEqual(v.overall, REJECT)
        self.assertEqual(v.reason, "cooldown")

    async def test_persists_verdict_and_flips_status(self):
        for i in range(60):
            await self._insert_snapshot(i=i, score=60, rsi=20, outcome_pct=1.0)
        pid = await self._create_proposal()
        await validate_proposal(
            self.pool, proposal_id=pid,
            baseline={"QUANT_SCORE_MIN": 50},
            candidate={"QUANT_SCORE_MIN": 55},
        )
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT status, adversary_result, adversary_ts FROM tuning_proposals WHERE id=$1",
                pid,
            )
        self.assertIn(row["status"], ("validated", "rejected", "awaiting_human"))
        self.assertIsNotNone(row["adversary_result"])
        self.assertIsNotNone(row["adversary_ts"])


if __name__ == "__main__":
    unittest.main()
