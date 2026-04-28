# Fase 1 — RESULT

**Status:** ✅ Komplet
**Branch:** `fase-1-db`
**Dato:** 2026-04-28

---

## Oprettede filer

```
polymarket-bot/
├── requirements.txt                                         (14 deps)
├── alembic.ini                                              (Alembic config)
├── alembic/
│   ├── env.py                                               (psycopg2 sync env)
│   ├── script.py.mako                                       (revision-template)
│   └── versions/
│       ├── .gitkeep
│       ├── 001_create_wallets.py
│       ├── 002_create_followed_wallets.py
│       ├── 003_create_positions.py
│       ├── 004_create_trade_events.py
│       ├── 005_trade_events_immutability_trigger.py
│       ├── 006_trade_events_notify_trigger.py
│       ├── 007_create_copy_orders.py
│       ├── 008_create_wallet_scores.py
│       ├── 009_create_wallet_score_snapshots.py
│       ├── 010_create_market_metadata.py
│       └── 011_create_daily_stats.py
├── db.py                                                    (asyncpg pool)
├── tests/
│   ├── __init__.py
│   └── conftest.py                                          (pytest fixtures)
└── faser/
    └── fase-1/
        ├── PROMPT.md                                        (eksisterende)
        └── RESULT.md                                        (denne fil)
```

---

## Migrationsrækkefølge og indhold

Hver migration er kædet via `down_revision` så Alembic kører dem deterministisk:

| # | Navn | Opretter | Indeks/triggers |
|---|------|----------|-----------------|
| 001 | `create_wallets` | `wallets` (id, address UNIQUE, label, added_at, notes) | `idx_wallets_address` |
| 002 | `create_followed_wallets` | `followed_wallets` (FK → wallets, position_size_pct, followed_at, unfollowed_at, reason) | `idx_followed_wallets_active (wallet_id, unfollowed_at)` |
| 003 | `create_positions` | `positions` (live snapshot, NUMERIC(18,4) sizes, NUMERIC(10,6) priser, status open/closed) | `UNIQUE(wallet_id, condition_id, outcome)`, `idx_positions_wallet_status` |
| 004 | `create_trade_events` | `trade_events` (immutable log, event_type opened/closed/resized) | `idx_trade_events_wallet_ts`, `idx_trade_events_condition_ts` |
| 005 | `trade_events_immutability_trigger` | Trigger-funktion + `BEFORE UPDATE/DELETE` triggers der kaster exception | — |
| 006 | `trade_events_notify_trigger` | Trigger-funktion + `AFTER INSERT` der kalder `pg_notify('new_trade', NEW.id::text)` | — |
| 007 | `create_copy_orders` | `copy_orders` (side buy/sell, status pending/submitted/filled/failed/cancelled/paper) | `idx_copy_orders_wallet_ts`, `idx_copy_orders_status_ts` |
| 008 | `create_wallet_scores` | `wallet_scores` (én row per wallet, alle metrics i NUMERIC) | PK på `wallet_id` |
| 009 | `create_wallet_score_snapshots` | `wallet_score_snapshots` (historisk log, append-only via design) | `idx_wallet_score_snapshots_wallet_ts` |
| 010 | `create_market_metadata` | `market_metadata` (cache af Gamma API-data, JSONB outcomes/clob_token_ids) | PK på `condition_id` |
| 011 | `create_daily_stats` | `daily_stats` (PK = date, generated column `realized_pnl`) | — |

Alle penge-/størrelses-kolonner bruger `NUMERIC(18,4)`; alle priser `NUMERIC(10,6)`. Alle tidsstempler er `TIMESTAMPTZ`.

---

## Schema-beslutninger (vigtige)

### `trade_events` immutability (#005)
Enforced på DB-niveau via `BEFORE UPDATE` og `BEFORE DELETE` triggers der kalder en plpgsql-funktion `deny_trade_events_mutation()`. Funktionen kaster `RAISE EXCEPTION` så ingen ORM eller raw SQL kan omgå den. Dette er PRD-krav (#04 og CLAUDE.md "Database-regler").

### `pg_notify` på trade_events (#006)
`AFTER INSERT` trigger sender notifikationen `new_trade` med ID som payload. Executor (Fase 3) lytter på denne kanal via `LISTEN new_trade` for sub-100 ms reaktion.

### Generated column i `daily_stats` (#011)
`realized_pnl` er `GENERATED ALWAYS AS (total_returned - total_spent) STORED` — kan aldrig blive uoverensstemmende med kilde-kolonnerne.

### UNIQUE constraint på positions
`UNIQUE(wallet_id, condition_id, outcome)` muliggør UPSERT (`ON CONFLICT DO UPDATE`) fra monitor.

---

## db.py — connection pool design

- Modul-global `_pool: asyncpg.Pool | None` initialiseres lazy ved første `get_pool()` kald
- Pool-størrelse: min=2, max=10, command_timeout=30s — passer Hetzner CX22 og forventet load
- DSN-konvertering: `postgresql+asyncpg://` → `postgresql://` (asyncpg native format)
- `acquire()` context manager wrapper for ergonomisk brug i monitor/executor/filter
- `close_pool()` til graceful shutdown ved SIGTERM (ECC bug #3 fix kommer i Fase 2)

---

## tests/conftest.py — fixture design

- `db_pool` (session-scoped): én pool deles på tværs af alle tests i sessionen
- `db_conn` (per-test): hver test får sin egen forbindelse, alt rulles tilbage efter testen — ingen lækage mellem tests
- `mock_positions`: list[dict] der matcher Polymarket Data API position-format (Yes/No, NUMERIC-strings, conditionId)
- `mock_fetch_positions`: `unittest.mock.patch` af `monitor.fetch_positions` (klar til Fase 2)

---

## Verifikation

| Check | Status | Note |
|-------|--------|------|
| `ruff check db.py alembic/ tests/` | ✅ | All checks passed |
| `black db.py alembic/ tests/` | ✅ | 14 filer reformatteret automatisk |
| `mypy db.py --ignore-missing-imports` | ✅ | No issues |
| `python3 -c "import alembic"` | ✅ | alembic 1.13.1 |
| Migrations chain-validation | ✅ | 001 → 002 → … → 011 (no cycles) |
| `pytest tests/ -x -q` | ✅ | "no tests ran" — forventet, kun fixtures i Fase 1 |

---

## Afvigelser fra PROMPT.md

1. **`alembic/script.py.mako` tilføjet** — ikke nævnt i prompt, men nødvendig hvis brugeren senere kører `alembic revision -m "..."` til at generere ny migration. Standard Alembic-template uden ændringer.
2. **Branch `fase-1-db` oprettet** — promptet specificerede ikke branch-skift, men CLAUDE.md "Branch-strategi" foreskriver `fase-1-db`. Brugeren merger til main via GitHub Desktop.
3. **`monitor.py` urørt** — ruff foreslog at fjerne `from typing import Any` i monitor.py, men det er udenfor Fase 1's scope; reverteret. Cleanup hører til Fase 2.
4. **Conventional commit-format med em-dash** — alle commits bruger `—` (U+2014) i deres beskeder for at matche prompts eksempler.

---

## Næste skridt (Fase 2)

Fase 2 udvider `monitor.py` med:
- DB-writes via `db.py` på opened/closed/resized events
- Dynamisk WS re-subscribe (fix #10)
- Retry-logik på `fetch_positions` (fix #11)
- Async HTTP via executor pool (fix #12)
- Health-check endpoint på port 8080 (fix #14)
- Logging via `logging` modul (fix #16)
- Tests der bruger `mock_positions` og `mock_fetch_positions` fixtures

Kør `python3 -m alembic upgrade head` (med `DB_URL` sat) når Postgres-instansen er klar. Brug en lokal Docker postgres for udvikling — Fase 5 leverer `docker-compose.yml`.
