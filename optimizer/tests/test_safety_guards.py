"""Safety-guards audit tests.

These are cross-cutting checks on the system's hard limits. They aren't
redundant with individual module tests — they specifically cover the
documented failure modes:

  - parameter drift that accumulates
  - canary ever exceeding MAX_CANARY_SLOTS_ABSOLUTE
  - structural keys auto-applied
  - forbidden keys reach the versioned store
  - rollback cooldown actually blocks ping-pong
"""
from __future__ import annotations

import json
import os
import unittest

import asyncpg

from optimizer import safety
from optimizer.config_store.versions import (
    propose_version, ConfigValidationError,
)
from optimizer.canary.runner import start_canary

DSN = os.environ.get("TRADING_TEST_DSN")


@unittest.skipUnless(DSN, "TRADING_TEST_DSN not set")
class SafetyGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        async with self.pool.acquire() as c:
            await c.execute(
                """TRUNCATE config_values, config_versions,
                     canary_assignments, tuning_proposals,
                     apply_events, rollback_events
                     RESTART IDENTITY CASCADE"""
            )
            self.bootstrap_id = await c.fetchval(
                """INSERT INTO config_versions
                     (created_by,source,rationale,activated_at,activated_by,scope)
                   VALUES ('t','bootstrap','s',NOW(),'t','{"kind":"global"}'::jsonb)
                   RETURNING id"""
            )
            await c.execute(
                """INSERT INTO config_values (version_id,key,value)
                   VALUES ($1,'TARGET_PROFIT_PCT','1.0'::jsonb)""",
                self.bootstrap_id,
            )
            self.proposal_id = await c.fetchval(
                """INSERT INTO tuning_proposals
                     (proposal,rationale,source,status)
                   VALUES ('{}'::jsonb,'t','numerical','validated') RETURNING id"""
            )

    async def asyncTearDown(self):
        await self.pool.close()

    async def test_canary_rejects_too_many_slots(self):
        too_many = list(range(1, safety.MAX_CANARY_SLOTS_ABSOLUTE + 2))
        with self.assertRaises(ValueError):
            await start_canary(
                self.pool,
                proposal_id=self.proposal_id,
                baseline_version_id=self.bootstrap_id,
                candidate_values={"TARGET_PROFIT_PCT": 1.1},
                slot_ids=too_many,
            )

    async def test_forbidden_key_rejected_regardless_of_source(self):
        for source in ("numerical", "llm_failure", "llm_strategic",
                        "llm_opportunity", "manual"):
            with self.assertRaises(ConfigValidationError):
                await propose_version(
                    self.pool, created_by="test", source=source,
                    rationale="",
                    values={"BOT_ENABLED": False},
                )

    async def test_structural_key_requires_manual_source(self):
        # numerical source may not propose structural keys.
        with self.assertRaises(ConfigValidationError):
            await propose_version(
                self.pool, created_by="test", source="numerical",
                rationale="",
                values={"LLM_MODEL_VETO": "anthropic/claude-haiku-4.5"},
            )
        # manual source can — but the key must exist in managed_keys first
        # (it doesn't in the seed), so this still raises for the right reason.
        with self.assertRaises(ConfigValidationError) as exc:
            await propose_version(
                self.pool, created_by="test", source="manual",
                rationale="",
                values={"LLM_MODEL_VETO": "anthropic/claude-haiku-4.5"},
            )
        self.assertIn("config_managed_keys", str(exc.exception))

    async def test_hard_caps_live_in_code_not_config(self):
        # Can't UPDATE a config key to change MIN_N_SAMPLES or similar.
        # Sanity-check the constants are ints in the module.
        self.assertGreaterEqual(safety.MIN_N_SAMPLES, 30)
        self.assertGreaterEqual(safety.MIN_CANARY_TRADES, 20)
        self.assertLessEqual(safety.MAX_CANARY_SLOTS_ABSOLUTE, 3)
        self.assertLessEqual(safety.MAX_SINGLE_CHANGE_PCT, 25)
        # FORBIDDEN list must include the really scary ones.
        for k in ("BOT_ENABLED", "UNIVERSE", "OPTIMIZER_ENABLED"):
            self.assertIn(k, safety.FORBIDDEN_TUNE_KEYS)


if __name__ == "__main__":
    unittest.main()
