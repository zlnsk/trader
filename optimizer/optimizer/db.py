"""Shared asyncpg pool + connection init.

One pool per optimizer process. Init codec turns JSONB into native dicts/lists
on read — parity with the bot's pool so the two can share helpers without
surprise double-encode.
"""
from __future__ import annotations

import json
import os

import asyncpg


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def open_pool(dsn: str | None = None,
                     min_size: int = 1, max_size: int = 4) -> asyncpg.Pool:
    dsn = dsn or os.environ["DATABASE_URL"]
    return await asyncpg.create_pool(
        dsn, min_size=min_size, max_size=max_size, init=_init_connection,
    )
