# Polymarket Copy-Trading Bot — Claude Code Instructions

## Projektoverblik

Python 3.11+ bot der kopierer trades fra højtperformende Polymarket-wallets.
Stack: PostgreSQL 16, asyncpg, Alembic, Docker Compose, Telegram Bot API.
Deployment: Hetzner CX22, Ashburn VA.

**Læs altid PRD.md før du starter på en ny fase.**

---

## Mandatory Skills

> ECC bug #2: Claude Code loader skills semantisk. Brug eksplicit invocation
> ved kritiske tasks ved at læse skill-filen direkte.

**Før du skriver migrations eller DB-kode:**
```
Read .ecc/database-migrations/SKILL.md
Read .ecc/postgres-patterns/SKILL.md
```

**Før du skriver executor/gate-logik:**
```
Read .ecc/verification-loop/SKILL.md
Read .ecc/security-review/SKILL.md
```

**Før du skriver monitor/loop-logik:**
```
Read .ecc/continuous-agent-loop/SKILL.md
Read .ecc/backend-patterns/SKILL.md
```

**Før du skriver tests:**
```
Read .ecc/tdd-workflow/SKILL.md
```

**Før du skriver Docker/deployment:**
```
Read .ecc/docker-patterns/SKILL.md
```

**Python regler (gælder altid):**
```
Read .ecc/rules/python/coding-style.md
Read .ecc/rules/common/security.md
```

---

## Projekt-struktur

```
polymarket-bot/
├── CLAUDE.md               ← denne fil
├── PRD.md                  ← kilde til sandhed for design
├── ECC-bugs-and-gaps.md    ← kendte problemer der skal fixes
├── .ecc/                   ← ECC skills (skjult mappe, se med Cmd+Shift+.)
├── monitor.py              ← eksisterende script (udgangspunkt Fase 2)
├── alembic/                ← migrations (oprettes i Fase 1)
├── alembic.ini             ← Alembic config (oprettes i Fase 1)
├── db.py                   ← delt connection pool (oprettes i Fase 1)
├── executor.py             ← trade executor (oprettes i Fase 3)
├── filter.py               ← filter CLI (oprettes i Fase 4)
├── tests/
│   └── conftest.py         ← pytest fixtures (asyncpg test session)
├── .env.example            ← template — aldrig .env i git
├── docker-compose.yml      ← (oprettes i Fase 5)
└── .gitignore
```

---

## Kode-konventioner

- **Type annotations** på alle funktioner — ingen undtagelser
- **asyncpg** til al DB-interaktion i runtime; psycopg2 kun til Alembic
- **logging** modul — aldrig `print()` i produktion
- **ruff** + **black** + **mypy** — kør inden commit
- **Dataclasses** til DTOs, **Protocol** til interfaces
- Funktioner max ~50 linjer, filer max ~300 linjer
- Secrets: KUN via env vars — aldrig hardcodet, aldrig i DB

---

## Database-regler

- Alle schema-ændringer via Alembic migration — aldrig manuel SQL mod DB
- Migrations er **immutable** når de er committed — opret ny migration i stedet
- `trade_events` tabellen må **aldrig** DELETE eller UPDATE — enforced via DB trigger
- Nye NOT NULL kolonner skal have DEFAULT eller være nullable
- Indexes skabes CONCURRENTLY på eksisterende tabeller
- Brug `TIMESTAMPTZ` — aldrig `TIMESTAMP` (timezone-naive)
- Priser: `NUMERIC(10,6)` — aldrig float
- Størrelser: `NUMERIC(18,4)` — aldrig float

---

## Miljøvariable (påkrævede)

```bash
DB_URL=postgresql+asyncpg://bot:password@localhost/polymarket
FOLLOWED_WALLETS=0x...,0x...
POLL_INTERVAL=30
LOG_LEVEL=INFO
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
POLYMARKET_PRIVATE_KEY=...          # Fase 3 — aldrig commit
MAX_DAILY_LOSS=50                   # USD
POSITION_SIZE_PCT=0.05              # 5% af tilgængeligt cash
DRY_RUN=true                        # Sæt til false ved go-live (Telegram-godkendt)
CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=65  # ECC bug #4 fix
```

---

## Git workflow

**Commit efter hvert gennemført trin** — automatisk, ingen undtagelser.

### Pre-commit checks (SKAL passere før commit)

```bash
# 1. Linting + formatering
ruff check . --fix
black .

# 2. Type checking
mypy . --ignore-missing-imports

# 3. Tests
pytest tests/ -x -q

# Hvis alle tre passerer → commit
```

Hvis tests fejler: stop, fiks problemet, kør checks igen. Commit aldrig kode der ikke passerer.

### Commit-format (conventional commits)

```
feat(db): add wallet_scores migration (#008)
feat(monitor): add db writes on position events
fix(monitor): replace ws full-restart with dynamic resubscribe
test(monitor): add retry logic tests with mock 429
```

### Push-politik

- **Commit** sker automatisk efter hvert trin
- **Push** til GitHub sker IKKE automatisk — brugeren reviewer og pusher selv via GitHub Desktop
- Undtagelse: hvis brugeren eksplicit beder om `git push`

### Branch-strategi

- `main` — stabil, kørende kode
- `fase-1-db`, `fase-2-monitor`, `fase-3-executor` osv.

---

## Context Compact Instructions

Når du compacter, bevar altid:
- Aktuel fase og hvilke migrations der er kørt
- Aktuelle schema-beslutninger (særligt triggers og constraints)
- Gate-logik i executor
- DRY_RUN status

---

## Fase-status

Opdater denne sektion når faser gennemføres:

- [x] **Fase 1:** Database-fundament (Alembic + db.py)
- [x] **Fase 2:** Monitor udvidelse (DB-writes + fixes)
- [ ] **Fase 3:** Trade Executor (gates + sizing + Telegram)
- [x] **Fase 4:** Filter Scanner CLI
- [ ] **Fase 5:** Docker + Hetzner deployment

---

## Sikkerhed — rød linje

1. `POLYMARKET_PRIVATE_KEY` må **aldrig** logges, printes eller gemmes i DB
2. `.env` og `.env.*` er i `.gitignore` — tjek dette inden første push
3. Kør AgentShield-scan inden første live deploy: `npx agentshield scan .`
4. Alle ordrer logges i `copy_orders` uanset outcome — ingen tavse fejl
