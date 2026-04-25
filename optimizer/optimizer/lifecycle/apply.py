"""Promote a passed canary to global config."""
from __future__ import annotations

import logging

import asyncpg

from ..config_store.versions import (
    propose_version, activate_version, _values_of, active_global_version,
    deactivate_version,
)

log = logging.getLogger("optimizer.apply")


async def apply_canary_globally(
    pool: asyncpg.Pool,
    *,
    canary_id: int,
    applied_by: str,
) -> int:
    """Take the canary's values, make a global config_version from them,
    activate it (auto-deactivates old global), deactivate the canary,
    record apply_events. Returns new global version id.
    """
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """SELECT proposal_id, canary_version_id
               FROM canary_assignments WHERE id=$1""",
            canary_id,
        )
        if row is None:
            raise ValueError(f"canary {canary_id} not found")

    values = await _values_of(pool, row["canary_version_id"])
    active = await active_global_version(pool)
    if active is None:
        raise RuntimeError("no active global version to transition from")

    new_id = await propose_version(
        pool,
        created_by=applied_by,
        source="canary",                  # source category: "canary" = promoted
        rationale=(
            f"Promoted canary #{canary_id} "
            f"(proposal #{row['proposal_id']}) to global"
        ),
        values=values,
        parent_id=active["id"],
        proposal_id=row["proposal_id"],
    )
    await activate_version(pool, new_id, activated_by=applied_by)
    # Retire the canary (slot-scoped) version — it's now redundant.
    await deactivate_version(
        pool, row["canary_version_id"],
        deactivated_by=applied_by, reason="promoted_to_global",
    )
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO apply_events
               (canary_id, from_version_id, to_version_id,
                applied_by, rationale)
               VALUES ($1,$2,$3,$4,$5)""",
            canary_id, active["id"], new_id, applied_by,
            f"Promote canary #{canary_id}",
        )
        await c.execute(
            """UPDATE tuning_proposals
               SET status='applied', applied_version_id=$2
               WHERE id=$1""",
            row["proposal_id"], new_id,
        )
    log.info("applied_globally", extra={
        "canary_id": canary_id, "new_version_id": new_id,
    })
    return new_id
