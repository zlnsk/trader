"""Versioned configuration CRUD.

A "version" is a named snapshot of managed-key values plus a scope
(global or slot-subset). Only ONE global-scope version may be active at
any time; slot-scoped canary versions may coexist with the active
global, and with each other only if slot_ids are disjoint.

Every version row carries:
  - source (where the change came from: numerical, llm_failure, manual, ...)
  - parent_id (previous version of the same scope — for lineage)
  - rationale (free text, human-readable)
  - proposal_id (FK to tuning_proposals once that pipeline picks it up)

Applying a version = set activated_at. Retiring a version = set
deactivated_at. Trader's bot reads the active config per-slot by calling
resolved_for_slot(slot_id).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

log = logging.getLogger("optimizer.config_store")


@dataclass
class ManagedKey:
    key: str
    dtype: str
    min_value: float | None
    max_value: float | None


async def get_managed_keys(pool: asyncpg.Pool) -> dict[str, ManagedKey]:
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT key, dtype, min_value, max_value FROM config_managed_keys"
        )
    out: dict[str, ManagedKey] = {}
    for r in rows:
        out[r["key"]] = ManagedKey(
            key=r["key"], dtype=r["dtype"],
            min_value=float(r["min_value"]) if r["min_value"] is not None else None,
            max_value=float(r["max_value"]) if r["max_value"] is not None else None,
        )
    return out


class ConfigValidationError(ValueError):
    pass


def _coerce_and_validate(mk: ManagedKey, value: Any) -> Any:
    if mk.dtype == "int":
        try:
            v = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError(f"{mk.key}: not an int ({value!r})") from exc
    elif mk.dtype == "float":
        try:
            v = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError(f"{mk.key}: not a float ({value!r})") from exc
    elif mk.dtype == "bool":
        if isinstance(value, bool):
            v = value
        else:
            raise ConfigValidationError(f"{mk.key}: not a bool ({value!r})")
    else:  # string
        if not isinstance(value, str):
            raise ConfigValidationError(f"{mk.key}: not a string ({value!r})")
        v = value
    if mk.dtype in ("int", "float"):
        if mk.min_value is not None and v < mk.min_value:
            raise ConfigValidationError(
                f"{mk.key}: {v} < min {mk.min_value}"
            )
        if mk.max_value is not None and v > mk.max_value:
            raise ConfigValidationError(
                f"{mk.key}: {v} > max {mk.max_value}"
            )
    return v


async def propose_version(
    pool: asyncpg.Pool,
    *,
    created_by: str,
    source: str,
    rationale: str,
    values: dict[str, Any],
    parent_id: int | None = None,
    proposal_id: int | None = None,
    scope_kind: str = "global",
    slot_ids: list[int] | None = None,
) -> int:
    """Create a config_versions row (not yet activated).

    Coerces and validates every key against config_managed_keys. Raises
    ConfigValidationError on any issue. Returns the new version id.

    Keys in FORBIDDEN_TUNE_KEYS are rejected here. Structural keys
    (STRUCTURAL_KEYS) are allowed but only if source == 'manual'.
    """
    from .. import safety

    if scope_kind not in ("global", "slots"):
        raise ValueError(f"scope_kind must be 'global' or 'slots', got {scope_kind!r}")
    if scope_kind == "slots" and not slot_ids:
        raise ValueError("scope_kind='slots' requires slot_ids")

    keys = await get_managed_keys(pool)
    coerced: dict[str, Any] = {}
    for k, v in values.items():
        if k in safety.FORBIDDEN_TUNE_KEYS:
            raise ConfigValidationError(f"{k}: forbidden key — cannot be versioned")
        if k in safety.STRUCTURAL_KEYS and source != "manual":
            raise ConfigValidationError(
                f"{k}: structural key, only source='manual' may propose it"
            )
        mk = keys.get(k)
        if mk is None:
            raise ConfigValidationError(
                f"{k}: not in config_managed_keys; add via migration first"
            )
        coerced[k] = _coerce_and_validate(mk, v)

    scope: dict[str, Any] = {"kind": scope_kind}
    if scope_kind == "slots":
        scope["slot_ids"] = sorted(slot_ids)

    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                """INSERT INTO config_versions
                   (created_by, source, parent_id, proposal_id,
                    rationale, scope)
                   VALUES ($1,$2,$3,$4,$5,$6::jsonb)
                   RETURNING id""",
                created_by, source, parent_id, proposal_id,
                rationale, json.dumps(scope),
            )
            vid = row["id"]
            for k, v in coerced.items():
                await c.execute(
                    """INSERT INTO config_values (version_id, key, value)
                       VALUES ($1,$2,$3::jsonb)""",
                    vid, k, json.dumps(v),
                )
    log.info("proposed_version", extra={"id": vid, "source": source})
    return vid


async def activate_version(
    pool: asyncpg.Pool, version_id: int, *, activated_by: str,
) -> None:
    """Flip activated_at=NOW() for a version. If the scope is global, the
    previous global version is auto-deactivated atomically. Slot-scoped
    versions check for disjoint slot_ids against other active slot-scoped
    versions — overlap raises ValueError.
    """
    async with pool.acquire() as c:
        async with c.transaction():
            v = await c.fetchrow(
                "SELECT scope, activated_at FROM config_versions WHERE id=$1",
                version_id,
            )
            if v is None:
                raise ValueError(f"no such version {version_id}")
            if v["activated_at"] is not None:
                raise ValueError(f"version {version_id} already active")

            scope = v["scope"] if isinstance(v["scope"], dict) else json.loads(v["scope"])
            kind = scope.get("kind", "global")

            if kind == "global":
                # Retire any currently-active global version.
                await c.execute(
                    """UPDATE config_versions
                       SET deactivated_at=NOW(), deactivated_by=$2,
                           deactivated_reason='superseded'
                       WHERE scope->>'kind'='global'
                         AND activated_at IS NOT NULL
                         AND deactivated_at IS NULL
                         AND id<>$1""",
                    version_id, activated_by,
                )
            else:
                # Slot-scope: reject overlap.
                my_slots = set(scope.get("slot_ids") or [])
                active_slotted = await c.fetch(
                    """SELECT id, scope FROM config_versions
                       WHERE scope->>'kind'='slots'
                         AND activated_at IS NOT NULL
                         AND deactivated_at IS NULL
                         AND id<>$1""",
                    version_id,
                )
                for r in active_slotted:
                    other_scope = r["scope"] if isinstance(r["scope"], dict) else json.loads(r["scope"])
                    other_slots = set(other_scope.get("slot_ids") or [])
                    overlap = my_slots & other_slots
                    if overlap:
                        raise ValueError(
                            f"slot overlap with active version {r['id']}: {sorted(overlap)}"
                        )

            await c.execute(
                """UPDATE config_versions
                   SET activated_at=NOW(), activated_by=$2
                   WHERE id=$1""",
                version_id, activated_by,
            )
    log.info("activated_version", extra={"id": version_id})


async def deactivate_version(
    pool: asyncpg.Pool, version_id: int, *,
    deactivated_by: str, reason: str,
) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """UPDATE config_versions
               SET deactivated_at=NOW(), deactivated_by=$2,
                   deactivated_reason=$3
               WHERE id=$1 AND deactivated_at IS NULL""",
            version_id, deactivated_by, reason,
        )
    log.info("deactivated_version", extra={"id": version_id, "reason": reason})


async def active_global_version(pool: asyncpg.Pool) -> dict | None:
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """SELECT id, activated_at, rationale, source, parent_id
               FROM config_versions
               WHERE scope->>'kind'='global'
                 AND activated_at IS NOT NULL
                 AND deactivated_at IS NULL
               ORDER BY activated_at DESC LIMIT 1"""
        )
    return dict(row) if row else None


async def list_active_canaries(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT id, scope, activated_at, rationale, source
               FROM config_versions
               WHERE scope->>'kind'='slots'
                 AND activated_at IS NOT NULL
                 AND deactivated_at IS NULL
               ORDER BY activated_at DESC"""
        )
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("scope"), str):
            d["scope"] = json.loads(d["scope"])
        out.append(d)
    return out


async def resolved_for_slot(pool: asyncpg.Pool, slot_id: int) -> dict[str, Any]:
    """Return the effective key/value dict for a slot. Slot-scoped active
    version wins over global. Returns {} if no active global and no
    slot-scoped active version covers the slot."""
    canaries = await list_active_canaries(pool)
    for v in canaries:
        slots = set(v["scope"].get("slot_ids") or [])
        if slot_id in slots:
            return await _values_of(pool, v["id"])
    g = await active_global_version(pool)
    if g is None:
        return {}
    return await _values_of(pool, g["id"])


async def _values_of(pool: asyncpg.Pool, version_id: int) -> dict[str, Any]:
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT key, value FROM config_values WHERE version_id=$1",
            version_id,
        )
    out: dict[str, Any] = {}
    for r in rows:
        val = r["value"]
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                pass
        out[r["key"]] = val
    return out


async def rollback_to(
    pool: asyncpg.Pool,
    *,
    bad_version_id: int,
    good_version_id: int,
    trigger: str,
    triggered_by: str,
    evidence: dict | None = None,
) -> int:
    """Create a new version that mirrors `good_version_id`'s values,
    activate it (auto-deactivating `bad_version_id`), and record a
    rollback_events row. Returns new version id.

    The new version is not literally good_version_id reactivated —
    intentional: keeps linear history, every apply/rollback is a new
    node.
    """
    good_vals = await _values_of(pool, good_version_id)
    new_id = await propose_version(
        pool,
        created_by=triggered_by,
        source="rollback",
        rationale=f"Rollback from #{bad_version_id} to known-good #{good_version_id}: {trigger}",
        values=good_vals,
        parent_id=bad_version_id,
    )
    await deactivate_version(
        pool, bad_version_id, deactivated_by=triggered_by,
        reason=f"rolled_back:{trigger}",
    )
    await activate_version(pool, new_id, activated_by=triggered_by)
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO rollback_events
               (bad_version_id, rolled_back_to_id, trigger,
                triggered_by, evidence)
               VALUES ($1,$2,$3,$4,$5::jsonb)""",
            bad_version_id, new_id, trigger, triggered_by,
            json.dumps(evidence or {}),
        )
    return new_id


async def trace_lineage(pool: asyncpg.Pool, version_id: int) -> list[dict]:
    """Walk back through parent_id chain, returning one dict per node.
    First element = requested version; last element = oldest ancestor.
    Single query reconstructs full causal chain."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            """WITH RECURSIVE lineage(id, depth) AS (
                 SELECT id, 0 FROM config_versions WHERE id=$1
                 UNION ALL
                 SELECT cv.parent_id, l.depth + 1
                   FROM config_versions cv
                   JOIN lineage l ON cv.id = l.id
                  WHERE cv.parent_id IS NOT NULL
               )
               SELECT cv.id, cv.created_at, cv.source, cv.rationale,
                      cv.proposal_id, cv.scope, cv.activated_at,
                      cv.deactivated_at, cv.deactivated_reason,
                      tp.rationale AS proposal_rationale,
                      of.detector AS finding_detector,
                      of.subject AS finding_subject
                 FROM lineage l
                 JOIN config_versions cv ON cv.id = l.id
            LEFT JOIN tuning_proposals tp ON tp.id = cv.proposal_id
            LEFT JOIN optimizer_findings of ON of.proposal_id = cv.proposal_id
             ORDER BY l.depth""",
            version_id,
        )
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("scope"), str):
            d["scope"] = json.loads(d["scope"])
        out.append(d)
    return out
