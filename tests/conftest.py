"""
tests/conftest.py — pytest fixtures til Polymarket bot.

Tilpasset ECC eval-harness pattern til Python/pytest (ECC bug #6 fix).
Alle DB-tests kører i en transaction der rulles tilbage — ingen persistent state.
"""
from __future__ import annotations

import os
from typing import AsyncGenerator
from unittest.mock import patch

import asyncpg
import pytest
import pytest_asyncio

TEST_DB_URL = os.getenv(
    "TEST_DB_URL",
    "postgresql://bot:password@localhost/polymarket_test",
)


@pytest_asyncio.fixture(scope="session")
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Session-scoped pool mod test-databasen."""
    dsn = TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=3)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def db_conn(
    db_pool: asyncpg.Pool,
) -> AsyncGenerator[asyncpg.Connection, None]:
    """Per-test connection i en transaction der rulles tilbage efter testen."""
    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        yield conn
        await tr.rollback()


@pytest.fixture
def mock_positions() -> list[dict]:
    """Standard Polymarket Data API position-fixtures til unit tests."""
    return [
        {
            "conditionId": "0xabc123000000000000000000000000000000000000000000000000000000000",
            "outcome": "Yes",
            "size": "100.0",
            "avgPrice": "0.650000",
            "curPrice": "0.720000",
            "currentValue": "72.00",
            "cashPnl": "7.00",
            "percentPnl": "10.77",
            "asset": "0xtoken123000000000000000000000000000000000000000000000000000000001",
            "title": "Test Market A — vil dette ske?",
        },
        {
            "conditionId": "0xdef456000000000000000000000000000000000000000000000000000000000",
            "outcome": "No",
            "size": "50.0",
            "avgPrice": "0.400000",
            "curPrice": "0.350000",
            "currentValue": "17.50",
            "cashPnl": "-2.50",
            "percentPnl": "-12.50",
            "asset": "0xtoken456000000000000000000000000000000000000000000000000000000002",
            "title": "Test Market B — vil det andet ske?",
        },
    ]


@pytest.fixture
def mock_fetch_positions(mock_positions: list[dict]):
    """Patch fetch_positions til at returnere fixtures uden HTTP-kald."""
    with patch("monitor.fetch_positions", return_value=mock_positions) as m:
        yield m
