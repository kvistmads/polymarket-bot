"""
db.py — Delt asyncpg connection pool.

Bruges af: monitor.py, executor.py, filter.py
Import:    from db import get_pool, acquire, close_pool
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import asyncpg

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Returnér den globale connection pool — opret den hvis den ikke findes."""
    global _pool
    if _pool is None:
        db_url = os.environ["DB_URL"]
        # asyncpg bruger postgresql:// — ikke postgresql+asyncpg://
        dsn = db_url.replace("postgresql+asyncpg://", "postgresql://")
        _pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        log.info("Database connection pool created (min=2, max=10)")
    return _pool


async def close_pool() -> None:
    """Luk connection pool gracefully ved shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("Database connection pool closed")


@asynccontextmanager
async def acquire() -> AsyncGenerator[asyncpg.Connection, None]:
    """Context manager til at hente én forbindelse fra poolen.

    Brug:
        async with acquire() as conn:
            await conn.execute("SELECT 1")
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
