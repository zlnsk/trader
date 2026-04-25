"""Versioned-config integration tests against trading_test DB."""
from __future__ import annotations

import json
import os
import unittest

import asyncpg

from optimizer.config_store import (
    propose_version, activate_version, deactivate_version,
    active_global_version, resolved_for_slot, rollback_to,
    list_active_canaries, trace_lineage,
)
from optimizer.config_store.versions import ConfigValidationError

DSN = os.environ.get("TRADING_TEST_DSN")


@unittest.skipUnless(DSN, "TRADING_TEST_DSN not set")
class ConfigStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        async with self.pool.acquire() as c:
            await c.execute(
                """TRUNCATE config_values, config_versions,
                     rollback_events, apply_events, canary_assignments
                     RESTART IDENTITY CASCADE"""
            )
            # Re-seed the bootstrap global version so tests start from a
            # known active baseline.
            await c.execute(
                """INSERT INTO config_versions
                   (created_by, source, rationale, activated_at, activated_by, scope)
                   VALUES ('test:setUp','bootstrap','test baseline',
                           NOW(),'test:setUp','{"kind":"global"}'::jsonb)"""
            )

    async def asyncTearDown(self):
        await self.pool.close()

    async def _bootstrap_id(self) -> int:
        async with self.pool.acquire() as c:
            return await c.fetchval(
                "SELECT id FROM config_versions WHERE source='bootstrap'"
            )

    async def test_propose_and_activate_global(self):
        parent = await self._bootstrap_id()
        vid = await propose_version(
            self.pool,
            created_by="test",
            source="numerical",
            rationale="test change",
            values={"TARGET_PROFIT_PCT": 1.5},
            parent_id=parent,
        )
        await activate_version(self.pool, vid, activated_by="test")

        active = await active_global_version(self.pool)
        self.assertEqual(active["id"], vid)

        # Previous bootstrap auto-deactivated.
        async with self.pool.acquire() as c:
            old = await c.fetchrow(
                "SELECT deactivated_at FROM config_versions WHERE id=$1",
                parent,
            )
            self.assertIsNotNone(old["deactivated_at"])

    async def test_rejects_forbidden_key(self):
        with self.assertRaises(ConfigValidationError):
            await propose_version(
                self.pool, created_by="test", source="numerical",
                rationale="", values={"BOT_ENABLED": False},
            )

    async def test_rejects_out_of_range(self):
        with self.assertRaises(ConfigValidationError):
            await propose_version(
                self.pool, created_by="test", source="numerical",
                rationale="", values={"TARGET_PROFIT_PCT": 99.0},  # > max 10
            )

    async def test_rejects_unknown_key(self):
        with self.assertRaises(ConfigValidationError):
            await propose_version(
                self.pool, created_by="test", source="numerical",
                rationale="", values={"NOT_A_KEY": 1},
            )

    async def test_structural_key_only_manual(self):
        with self.assertRaises(ConfigValidationError):
            await propose_version(
                self.pool, created_by="test", source="numerical",
                rationale="", values={"LLM_MODEL_VETO": "foo"},
            )

    async def test_canary_scope_and_resolved_for_slot(self):
        parent = await self._bootstrap_id()
        # Make the bootstrap version meaningful by upserting a value.
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO config_values (version_id, key, value) VALUES ($1, 'TARGET_PROFIT_PCT', '1.0'::jsonb) ON CONFLICT DO NOTHING",
                parent,
            )
        canary_id = await propose_version(
            self.pool, created_by="test", source="numerical",
            rationale="canary", values={"TARGET_PROFIT_PCT": 2.0},
            parent_id=parent, scope_kind="slots", slot_ids=[10, 11],
        )
        await activate_version(self.pool, canary_id, activated_by="test")
        canaries = await list_active_canaries(self.pool)
        self.assertEqual(len(canaries), 1)

        # Slot 10 resolves to canary.
        cfg10 = await resolved_for_slot(self.pool, 10)
        self.assertEqual(cfg10.get("TARGET_PROFIT_PCT"), 2.0)
        # Slot 15 resolves to bootstrap.
        cfg15 = await resolved_for_slot(self.pool, 15)
        self.assertEqual(cfg15.get("TARGET_PROFIT_PCT"), 1.0)

    async def test_canary_overlap_rejected(self):
        parent = await self._bootstrap_id()
        v1 = await propose_version(
            self.pool, created_by="test", source="numerical",
            rationale="c1", values={"TARGET_PROFIT_PCT": 1.2},
            parent_id=parent, scope_kind="slots", slot_ids=[10, 11],
        )
        v2 = await propose_version(
            self.pool, created_by="test", source="numerical",
            rationale="c2", values={"TARGET_PROFIT_PCT": 1.3},
            parent_id=parent, scope_kind="slots", slot_ids=[11, 12],
        )
        await activate_version(self.pool, v1, activated_by="test")
        with self.assertRaises(ValueError):
            await activate_version(self.pool, v2, activated_by="test")

    async def test_rollback_and_lineage(self):
        parent = await self._bootstrap_id()
        v1 = await propose_version(
            self.pool, created_by="test", source="numerical",
            rationale="bad change", values={"TARGET_PROFIT_PCT": 1.5},
            parent_id=parent,
        )
        await activate_version(self.pool, v1, activated_by="test")
        new_id = await rollback_to(
            self.pool, bad_version_id=v1, good_version_id=parent,
            trigger="pf_regression", triggered_by="test",
            evidence={"pf": 0.4},
        )
        # Lineage: new -> v1 -> parent
        lineage = await trace_lineage(self.pool, new_id)
        ids = [r["id"] for r in lineage]
        self.assertEqual(ids[0], new_id)
        self.assertIn(v1, ids)
        self.assertIn(parent, ids)

        # rollback_events row exists.
        async with self.pool.acquire() as c:
            rb = await c.fetchrow(
                "SELECT * FROM rollback_events WHERE bad_version_id=$1",
                v1,
            )
            self.assertIsNotNone(rb)
            self.assertEqual(rb["trigger"], "pf_regression")


if __name__ == "__main__":
    unittest.main()
