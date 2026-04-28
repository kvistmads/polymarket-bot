# Fase 1 — Database-fundament
**Til:** Ny Cowork-session  
**Projekt:** Polymarket Copy-Trading Bot  
**Arbejdsmappe:** polymarket-bot/

---

## Din første handling — læs disse filer INDEN du skriver en eneste linje kode:

```
Read CLAUDE.md
Read PRD.md
Read .ecc/database-migrations/SKILL.md
Read .ecc/postgres-patterns/SKILL.md
Read .ecc/rules/python/coding-style.md
Read .ecc/rules/common/security.md
```

---

## Mål for denne session

Byg det komplette database-fundament som monitor, executor og filter-scanner alle læner sig op ad:

1. `requirements.txt` — alle Python-afhængigheder
2. `alembic.ini` + `alembic/env.py` — Alembic migration-setup
3. **11 migrations** i korrekt rækkefølge (se nedenfor)
4. `db.py` — delt asyncpg connection pool
5. `tests/conftest.py` — pytest fixtures
6. `faser/fase-1/RESULT.md` — kort dokumentation af hvad der blev bygget

**Commit efter hvert afsluttet punkt.** Kør pre-commit checks (ruff, black, mypy, pytest) inden hver commit. Push aldrig — brugeren gør det selv via GitHub Desktop.

---

## Trin 1 — requirements.txt

Opret `requirements.txt` i projektets rod:

```
asyncpg==0.29.0
psycopg2-binary==2.9.9
alembic==1.13.1
python-dotenv==1.0.1
httpx==0.27.0
websockets==12.0
requests==2.31.0
aiohttp==3.9.5
python-telegram-bot==21.3
ruff==0.4.4
black==24.4.2
mypy==1.10.0
pytest==8.2.1
pytest-asyncio==0.23.7
```

Commit: `chore(deps): add requirements.txt`

---

## Trin 2 — Alembic setup

### 2a — alembic.ini

Opret `alembic.ini` i projektets rod:

```ini
[alembic]
script_location = alembic
file_template = %%(rev)s_%%(slug)s
truncate_slug_length = 40
timezone = UTC

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %%H:%%M:%%S
```

### 2b — alembic/env.py

Opret `alembic/env.py`:

```python
"""Alembic environment — bruger psycopg2 (sync) til migrations."""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Hent DB URL fra environment — understøt både asyncpg og psycopg2 format
def get_sync_url() -> str:
    url = os.environ.get("DB_URL", "")
    # Konvertér asyncpg-URL til psycopg2-format til Alembic
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


def run_migrations_offline() -> None:
    url = get_sync_url()
    context.configure(
        url=url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_sync_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

Opret også den tomme `alembic/versions/` mappe med en `.gitkeep` fil.

Commit: `chore(db): add alembic setup (alembic.ini + env.py)`

---

## Trin 3 — 11 migrations

Alle migrations bruger **rå SQL** via `op.execute()` — ingen SQLAlchemy ORM-modeller.  
Navngivning: `{revision}_{beskrivelse}.py` — Alembic genererer revision-id automatisk.

Kør for at oprette hver migration-fil:
```bash
alembic revision -m "create_wallets"
# Rediger filen der oprettes i alembic/versions/
```

### Migration 001 — create_wallets

```python
"""create_wallets

Revision ID: (auto)
"""
from alembic import op

def upgrade() -> None:
    op.execute("""
        CREATE TABLE wallets (
            id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            address  TEXT NOT NULL,
            label    TEXT,
            added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            notes    TEXT,
            CONSTRAINT wallets_address_key UNIQUE (address)
        )
    """)
    op.execute("""
        CREATE INDEX idx_wallets_address ON wallets (address)
    """)

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS wallets CASCADE")
```

Commit: `feat(db): migration 001 — create wallets table`

---

### Migration 002 — create_followed_wallets

```python
"""create_followed_wallets"""
from alembic import op

def upgrade() -> None:
    op.execute("""
        CREATE TABLE followed_wallets (
            id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            wallet_id         BIGINT NOT NULL REFERENCES wallets(id),
            position_size_pct NUMERIC(4,3),
            followed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            unfollowed_at     TIMESTAMPTZ,
            reason            TEXT
        )
    """)
    op.execute("""
        CREATE INDEX idx_followed_wallets_active
            ON followed_wallets (wallet_id, unfollowed_at)
    """)

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS followed_wallets CASCADE")
```

Commit: `feat(db): migration 002 — create followed_wallets table`

---

### Migration 003 — create_positions

```python
"""create_positions"""
from alembic import op

def upgrade() -> None:
    op.execute("""
        CREATE TABLE positions (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            wallet_id       BIGINT NOT NULL REFERENCES wallets(id),
            condition_id    TEXT NOT NULL,
            outcome         TEXT NOT NULL,
            size            NUMERIC(18,4) NOT NULL DEFAULT 0,
            avg_price       NUMERIC(10,6),
            cur_price       NUMERIC(10,6),
            current_value   NUMERIC(18,4),
            cash_pnl        NUMERIC(18,4),
            percent_pnl     NUMERIC(10,4),
            token_id        TEXT,
            title           TEXT,
            first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            status          TEXT NOT NULL DEFAULT 'open',
            CONSTRAINT positions_status_check
                CHECK (status IN ('open', 'closed')),
            CONSTRAINT positions_unique
                UNIQUE (wallet_id, condition_id, outcome)
        )
    """)
    op.execute("""
        CREATE INDEX idx_positions_wallet_status
            ON positions (wallet_id, status)
    """)

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS positions CASCADE")
```

Commit: `feat(db): migration 003 — create positions table`

---

### Migration 004 — create_trade_events

```python
"""create_trade_events"""
from alembic import op

def upgrade() -> None:
    op.execute("""
        CREATE TABLE trade_events (
            id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            wallet_id      BIGINT NOT NULL REFERENCES wallets(id),
            condition_id   TEXT NOT NULL,
            outcome        TEXT NOT NULL,
            event_type     TEXT NOT NULL,
            old_size       NUMERIC(18,4),
            new_size       NUMERIC(18,4) NOT NULL,
            price_at_event NUMERIC(10,6),
            pnl_at_close   NUMERIC(18,4),
            timestamp      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT trade_events_event_type_check
                CHECK (event_type IN ('opened', 'closed', 'resized'))
        )
    """)
    op.execute("""
        CREATE INDEX idx_trade_events_wallet_ts
            ON trade_events (wallet_id, timestamp)
    """)
    op.execute("""
        CREATE INDEX idx_trade_events_condition_ts
            ON trade_events (condition_id, timestamp)
    """)

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trade_events CASCADE")
```

Commit: `feat(db): migration 004 — create trade_events table`

---

### Migration 005 — trade_events immutability trigger

```python
"""trade_events_immutability_trigger"""
from alembic import op

def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION deny_trade_events_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'trade_events is immutable — UPDATE and DELETE are forbidden';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trade_events_deny_update
            BEFORE UPDATE ON trade_events
            FOR EACH ROW EXECUTE FUNCTION deny_trade_events_mutation()
    """)
    op.execute("""
        CREATE TRIGGER trade_events_deny_delete
            BEFORE DELETE ON trade_events
            FOR EACH ROW EXECUTE FUNCTION deny_trade_events_mutation()
    """)

def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trade_events_deny_update ON trade_events")
    op.execute("DROP TRIGGER IF EXISTS trade_events_deny_delete ON trade_events")
    op.execute("DROP FUNCTION IF EXISTS deny_trade_events_mutation")
```

Commit: `feat(db): migration 005 — trade_events immutability trigger`

---

### Migration 006 — trade_events pg_notify trigger

```python
"""trade_events_notify_trigger"""
from alembic import op

def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_new_trade_event()
        RETURNS trigger AS $$
        BEGIN
            PERFORM pg_notify('new_trade', NEW.id::text);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trade_events_notify
            AFTER INSERT ON trade_events
            FOR EACH ROW EXECUTE FUNCTION notify_new_trade_event()
    """)

def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trade_events_notify ON trade_events")
    op.execute("DROP FUNCTION IF EXISTS notify_new_trade_event")
```

Commit: `feat(db): migration 006 — trade_events pg_notify trigger`

---

### Migration 007 — create_copy_orders

```python
"""create_copy_orders"""
from alembic import op

def upgrade() -> None:
    op.execute("""
        CREATE TABLE copy_orders (
            id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source_wallet_id BIGINT NOT NULL REFERENCES wallets(id),
            trade_event_id   BIGINT REFERENCES trade_events(id),
            condition_id     TEXT NOT NULL,
            outcome          TEXT NOT NULL,
            side             TEXT NOT NULL,
            size_requested   NUMERIC(18,4) NOT NULL,
            size_filled      NUMERIC(18,4),
            price            NUMERIC(10,6),
            status           TEXT NOT NULL DEFAULT 'pending',
            error_msg        TEXT,
            timestamp        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT copy_orders_side_check
                CHECK (side IN ('buy', 'sell')),
            CONSTRAINT copy_orders_status_check
                CHECK (status IN (
                    'pending', 'submitted', 'filled',
                    'failed', 'cancelled', 'paper'
                ))
        )
    """)
    op.execute("""
        CREATE INDEX idx_copy_orders_wallet_ts
            ON copy_orders (source_wallet_id, timestamp)
    """)
    op.execute("""
        CREATE INDEX idx_copy_orders_status_ts
            ON copy_orders (status, timestamp)
    """)

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS copy_orders CASCADE")
```

Commit: `feat(db): migration 007 — create copy_orders table`

---

### Migration 008 — create_wallet_scores

```python
"""create_wallet_scores"""
from alembic import op

def upgrade() -> None:
    op.execute("""
        CREATE TABLE wallet_scores (
            wallet_id          BIGINT PRIMARY KEY REFERENCES wallets(id),
            trades_total       INTEGER NOT NULL DEFAULT 0,
            trades_won         INTEGER NOT NULL DEFAULT 0,
            win_rate           NUMERIC(6,4),
            sortino_ratio      NUMERIC(8,4),
            max_drawdown       NUMERIC(6,4),
            bull_win_rate      NUMERIC(6,4),
            bear_win_rate      NUMERIC(6,4),
            consistency_score  NUMERIC(6,4),
            sizing_entropy     NUMERIC(8,4),
            estimated_bankroll NUMERIC(18,2),
            annual_return_pct  NUMERIC(8,4),
            last_scored_at     TIMESTAMPTZ
        )
    """)

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS wallet_scores CASCADE")
```

Commit: `feat(db): migration 008 — create wallet_scores table`

---

### Migration 009 — create_wallet_score_snapshots

```python
"""create_wallet_score_snapshots"""
from alembic import op

def upgrade() -> None:
    op.execute("""
        CREATE TABLE wallet_score_snapshots (
            id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            wallet_id         BIGINT NOT NULL REFERENCES wallets(id),
            snapshot_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            trades_total      INTEGER NOT NULL DEFAULT 0,
            trades_won        INTEGER NOT NULL DEFAULT 0,
            win_rate          NUMERIC(6,4),
            sortino_ratio     NUMERIC(8,4),
            max_drawdown      NUMERIC(6,4),
            bull_win_rate     NUMERIC(6,4),
            bear_win_rate     NUMERIC(6,4),
            consistency_score NUMERIC(6,4),
            sizing_entropy    NUMERIC(8,4),
            annual_return_pct NUMERIC(8,4)
        )
    """)
    op.execute("""
        CREATE INDEX idx_wallet_score_snapshots_wallet_ts
            ON wallet_score_snapshots (wallet_id, snapshot_at)
    """)

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS wallet_score_snapshots CASCADE")
```

Commit: `feat(db): migration 009 — create wallet_score_snapshots table`

---

### Migration 010 — create_market_metadata

```python
"""create_market_metadata"""
from alembic import op

def upgrade() -> None:
    op.execute("""
        CREATE TABLE market_metadata (
            condition_id   TEXT PRIMARY KEY,
            title          TEXT,
            slug           TEXT,
            outcomes       JSONB,
            clob_token_ids JSONB,
            fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS market_metadata CASCADE")
```

Commit: `feat(db): migration 010 — create market_metadata table`

---

### Migration 011 — create_daily_stats

```python
"""create_daily_stats"""
from alembic import op

def upgrade() -> None:
    op.execute("""
        CREATE TABLE daily_stats (
            date               DATE PRIMARY KEY DEFAULT CURRENT_DATE,
            total_spent        NUMERIC(18,4) NOT NULL DEFAULT 0,
            total_returned     NUMERIC(18,4) NOT NULL DEFAULT 0,
            realized_pnl       NUMERIC(18,4)
                               GENERATED ALWAYS AS (total_returned - total_spent)
                               STORED,
            orders_count       INTEGER NOT NULL DEFAULT 0,
            paper_orders_count INTEGER NOT NULL DEFAULT 0,
            last_updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS daily_stats CASCADE")
```

Commit: `feat(db): migration 011 — create daily_stats table`

---

## Trin 4 — db.py

Opret `db.py` i projektets rod:

```python
"""
db.py — Delt asyncpg connection pool.

Bruges af: monitor.py, executor.py, filter.py
Import:    from db import get_pool, acquire, close_pool
"""
from __future__ import annotations

import asyncpg
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

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
```

Commit: `feat(db): add db.py — shared asyncpg connection pool`

---

## Trin 5 — tests/conftest.py

Opret `tests/__init__.py` (tom) og `tests/conftest.py`:

```python
"""
tests/conftest.py — pytest fixtures til Polymarket bot.

Tilpasset ECC eval-harness pattern til Python/pytest (ECC bug #6 fix).
Alle DB-tests kører i en transaction der rulles tilbage — ingen persistent state.
"""
from __future__ import annotations

import os
import pytest
import pytest_asyncio
import asyncpg
from typing import AsyncGenerator
from unittest.mock import patch

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
```

Commit: `test(db): add conftest.py with asyncpg fixtures and mock positions`

---

## Trin 6 — Verifikation

Kør følgende checks og ret eventuelle fejl inden du afslutter:

```bash
# 1. Linting
ruff check . --fix
black .

# 2. Type checking
mypy db.py --ignore-missing-imports

# 3. Bekræft at alle migrations kan importeres
python -c "import alembic; print('alembic ok')"

# 4. Kør tests (kun fixtures og import-checks — ingen DB krævet endnu)
pytest tests/ -x -q
```

Hvis alle checks er grønne:

Commit: `test(db): verify fase-1 — all checks passing`

---

## Trin 7 — Dokumentation

Opret `faser/fase-1/RESULT.md` med:
- Liste over alle oprettede filer
- Migrationsrækkefølge og hvad hver gør
- Eventuelle afvigelser fra denne prompt og hvorfor
- Status: ✅ Fase 1 komplet

Commit: `docs(fase-1): add RESULT.md`

---

## Slutstatus

Når alle trin er gennemført skal følgende filer eksistere:

```
polymarket-bot/
├── requirements.txt
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│       ├── 001_create_wallets.py
│       ├── 002_create_followed_wallets.py
│       ├── 003_create_positions.py
│       ├── 004_create_trade_events.py
│       ├── 005_trade_events_immutability_trigger.py
│       ├── 006_trade_events_notify_trigger.py
│       ├── 007_create_copy_orders.py
│       ├── 008_create_wallet_scores.py
        ├── 009_create_wallet_score_snapshots.py
│       ├── 010_create_market_metadata.py
│       └── 011_create_daily_stats.py
├── db.py
├── tests/
│   ├── __init__.py
│   └── conftest.py
└── faser/
    └── fase-1/
        ├── PROMPT.md  (denne fil)
        └── RESULT.md  (oprettes af Claude)
```

**Fase 1 er komplet når alle 7 trin er gennemført og alle commits er lavet.**  
Brugeren pusher til GitHub via GitHub Desktop.
