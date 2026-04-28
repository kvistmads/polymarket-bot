# PRD: Polymarket Copy-Trading Bot
**Version:** 2.0  
**Status:** Aktiv  
**Stack:** Python 3.11+, PostgreSQL 16, Docker, Hetzner CX22 (Ashburn VA)  
**ECC Foundation:** `.ecc/` (lokalt kopieret fra github.com/affaan-m/everything-claude-code)

---

## Overblik

Et system der automatisk kopierer trades fra manuelt udvalgte, højtperformende Polymarket-wallets. Systemet består af tre uafhængige komponenter der kommunikerer via en delt PostgreSQL-database.

**Komponenter:**
1. **Wallet Monitor** — overvåger udvalgte wallets i real-time (udvidelse af monitor.py)
2. **Trade Executor** — kopierer trades automatisk med konfigurerbar sizing og gates
3. **Filter Scanner** — manuelt CLI-værktøj til at score og udvælge wallets

Filter-systemet er bevidst adskilt fra den automatiske pipeline. Det bruges til at vedligeholde listen af fulgte wallets, ikke til runtime-beslutninger.

---

## Arkitektur

```
[Filter Scanner] ──writes──► [wallet_scores / wallet_score_snapshots]
                                      │
                              [followed_wallets config]
                                      │
[Polymarket APIs] ──► [Wallet Monitor] ──► [positions / trade_events]
                                      │         │
                                      │    pg_notify('new_trade')
                                      │         │
                              [Trade Executor] ──► [Polymarket CLOB API]
                                      │
                              [copy_orders / daily_stats]
                                      │
                              [Telegram Bot] ──► [dig]
```

Ingen direkte kommunikation mellem komponenter — alt går via databasen. `pg_notify` bruges til lav-latency signalering fra monitor til executor (sub-100ms reaktionstid).

---

## Design-beslutninger (interview-afklarede)

| Emne | Beslutning |
|------|-----------|
| followed_wallets | Separat tabel med fuld historik (follow/unfollow log) |
| trade_events immutability | Enforced på DB-niveau via trigger (DENY UPDATE/DELETE) |
| Executor signalering | pg_notify på `trade_events` INSERT |
| Position sizing | Fast % af tilgængeligt cash (CLOB API balance endpoint) |
| Sizing default | Global env var `POSITION_SIZE_PCT=0.05` med per-wallet override |
| Bankroll estimering | CLOB API `GET /balance` — ground truth, ikke DB-tracket |
| Paper trading | `DRY_RUN=true` env var — full pipeline, ingen CLOB-submit |
| Go-live gate | Performance-baseret: win_rate > 52% over ≥20 paper trades → Telegram approval |
| Telegram approval | Inline keyboard buttons: "✅ Klar" / "❌ Ikke klar" |
| Alerts | Telegram Bot (ny bot til dette projekt) |
| Alert triggers | Alle simulerede/live trades + kritiske fejl + daglig rapport |
| Deployment | Hetzner CX22, Ashburn VA (us-east-1 nærhed) |
| CI/CD | GitHub → Railway auto-deploy (alternativ: Hetzner git pull) |
| Historiske scores | `wallet_score_snapshots` tabel — snapshot ved hvert filter-scanner kørsel |
| Daily loss tracking | `daily_stats` tabel — opdateres atomisk ved hvert fill |

---

## Fase 1: Database-fundament

**ECC skills:** `.ecc/postgres-patterns`, `.ecc/database-migrations`  
**Fixes fra ECC-bugs-and-gaps:** #8 (.gitignore), #17 (db.py), #18 (Alembic setup)  
**Mål:** Stabilt dataskema alle tre komponenter kan læne sig op ad.

### Tabeller (9 total)

**`wallets`** — wallet-identitet, aldrig slettet
```sql
id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
address         TEXT NOT NULL UNIQUE,
label           TEXT,
added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
notes           TEXT
```
*Index: address*

---

**`followed_wallets`** — aktiv følge-konfiguration + historik
```sql
id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
wallet_id       BIGINT NOT NULL REFERENCES wallets(id),
position_size_pct NUMERIC(4,3),           -- NULL = brug global POSITION_SIZE_PCT
followed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
unfollowed_at   TIMESTAMPTZ,              -- NULL = aktuelt fulgt
reason          TEXT
```
*Aktive wallets: WHERE unfollowed_at IS NULL*  
*Index: (wallet_id, unfollowed_at)*

---

**`positions`** — live snapshot fra monitor
```sql
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
status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed'))
```
*UNIQUE(wallet_id, condition_id, outcome)*  
*Index: (wallet_id, status)*

---

**`trade_events`** — immutable log, aldrig slet eller opdater
```sql
id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
wallet_id       BIGINT NOT NULL REFERENCES wallets(id),
condition_id    TEXT NOT NULL,
outcome         TEXT NOT NULL,
event_type      TEXT NOT NULL CHECK (event_type IN ('opened', 'closed', 'resized')),
old_size        NUMERIC(18,4),
new_size        NUMERIC(18,4) NOT NULL,
price_at_event  NUMERIC(10,6),
pnl_at_close    NUMERIC(18,4),
timestamp       TIMESTAMPTZ NOT NULL DEFAULT now()
```
*Immutability enforced via DB trigger (DENY UPDATE + DELETE)*  
*pg_notify trigger: AFTER INSERT → pg_notify('new_trade', NEW.id::text)*  
*Index: (wallet_id, timestamp), (condition_id, timestamp)*

---

**`copy_orders`** — hvad botten har (forsøgt) eksekveret
```sql
id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
source_wallet_id    BIGINT NOT NULL REFERENCES wallets(id),
trade_event_id      BIGINT REFERENCES trade_events(id),
condition_id        TEXT NOT NULL,
outcome             TEXT NOT NULL,
side                TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
size_requested      NUMERIC(18,4) NOT NULL,
size_filled         NUMERIC(18,4),
price               NUMERIC(10,6),
status              TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','submitted','filled','failed','cancelled','paper')),
error_msg           TEXT,
timestamp           TIMESTAMPTZ NOT NULL DEFAULT now()
```
*Index: (source_wallet_id, timestamp), (status, timestamp)*

---

**`wallet_scores`** — seneste beregnede metrics (opdateres af filter-scanner)
```sql
wallet_id           BIGINT PRIMARY KEY REFERENCES wallets(id),
trades_total        INTEGER NOT NULL DEFAULT 0,
trades_won          INTEGER NOT NULL DEFAULT 0,
win_rate            NUMERIC(6,4),
sortino_ratio       NUMERIC(8,4),
max_drawdown        NUMERIC(6,4),
bull_win_rate       NUMERIC(6,4),
bear_win_rate       NUMERIC(6,4),
consistency_score   NUMERIC(6,4),
sizing_entropy      NUMERIC(8,4),
estimated_bankroll  NUMERIC(18,2),        -- auto-beregnet: SUM(current_value)
annual_return_pct   NUMERIC(8,4),         -- ÅOP
last_scored_at      TIMESTAMPTZ
```

---

**`wallet_score_snapshots`** — historisk score-log (snapshot ved hvert filter-scanner kørsel)
```sql
id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
wallet_id           BIGINT NOT NULL REFERENCES wallets(id),
snapshot_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
trades_total        INTEGER NOT NULL DEFAULT 0,
trades_won          INTEGER NOT NULL DEFAULT 0,
win_rate            NUMERIC(6,4),
sortino_ratio       NUMERIC(8,4),
max_drawdown        NUMERIC(6,4),
bull_win_rate       NUMERIC(6,4),
bear_win_rate       NUMERIC(6,4),
consistency_score   NUMERIC(6,4),
sizing_entropy      NUMERIC(8,4),
annual_return_pct   NUMERIC(8,4)
```
*Index: (wallet_id, snapshot_at)*

---

**`market_metadata`** — cache af Gamma API-data
```sql
condition_id        TEXT PRIMARY KEY,
title               TEXT,
slug                TEXT,
outcomes            JSONB,               -- ['Yes', 'No'] — JSON array
clob_token_ids      JSONB,               -- ['123...', '456...'] — JSON array
fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now()
```

---

**`daily_stats`** — daglig P&L tracker (bruges til max daily loss gate)
```sql
date                DATE PRIMARY KEY DEFAULT CURRENT_DATE,
total_spent         NUMERIC(18,4) NOT NULL DEFAULT 0,
total_returned      NUMERIC(18,4) NOT NULL DEFAULT 0,
realized_pnl        NUMERIC(18,4) GENERATED ALWAYS AS (total_returned - total_spent) STORED,
orders_count        INTEGER NOT NULL DEFAULT 0,
paper_orders_count  INTEGER NOT NULL DEFAULT 0,
last_updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
```

---

### Migrations
Alembic (Python). Én migration per tabel + separate migrations for triggers. Rækkefølge:
```
001_create_wallets
002_create_followed_wallets
003_create_positions
004_create_trade_events
005_create_trade_events_immutability_trigger
006_create_trade_events_notify_trigger
007_create_copy_orders
008_create_wallet_scores
009_create_wallet_score_snapshots
010_create_market_metadata
011_create_daily_stats
```

Driver: `asyncpg` (async). Alembic bruger `psycopg2` til selve migrations. `db.py` deles af alle komponenter.

---

## Fase 2: Wallet Monitor (udvidelse af monitor.py)

**ECC skills:** `.ecc/continuous-agent-loop` (alias: autonomous-loops), `.ecc/backend-patterns`, `.ecc/tdd-workflow`  
**Fixes fra ECC-bugs-and-gaps:** #9, #10, #11, #12, #13, #14, #15, #16

### Ændringer fra nuværende script

**Database-writes på alle events:**
```python
# På opened:
await db.insert_trade_event(wallet, pos, event_type="opened")
await db.upsert_position(wallet, pos)

# På closed:
await db.insert_trade_event(wallet, pos, event_type="closed")
await db.mark_position_closed(wallet, condition_id, outcome)

# På resized:
await db.insert_trade_event(wallet, old_pos, new_pos, event_type="resized")
await db.update_position_size(wallet, condition_id, new_size)
```

**Dynamisk WebSocket re-subscribe** (fix #10 — ingen fuld reconnect ved nye tokens):
```python
# Send ny SUBSCRIBE-besked til eksisterende forbindelse
await ws_connection.send(json.dumps({
    "assets_ids": new_token_ids,
    "type": "market"
}))
```

**Retry-logik på fetch_positions** (fix #11 — eksponentiel backoff, max 3 forsøg):
```python
async def fetch_positions_with_retry(wallet: str, max_attempts: int = 3) -> list[dict]:
    for attempt in range(max_attempts):
        try:
            return await asyncio.get_event_loop().run_in_executor(None, fetch_positions, wallet)
        except requests.HTTPError as e:
            if e.response.status_code == 429:
                await asyncio.sleep(2 ** attempt)
            else:
                raise
    return []
```

**Async HTTP via executor pool** (fix #12):
```python
current = await asyncio.get_event_loop().run_in_executor(None, fetch_positions, wallet)
```

**Health-check endpoint** (fix #14 — port 8080):
```python
# Returnerer 503 hvis > 3 polls er missede
async def health_handler(request):
    age = time.time() - last_successful_poll
    if age > POLL_INTERVAL * 3:
        return web.Response(status=503, text="stale")
    return web.Response(text="ok")
```

**Konfiguration via environment variables** (fix #15):
```
FOLLOWED_WALLETS=0x...,0x...   (eller læs aktive wallets fra DB)
POLL_INTERVAL=30
DB_URL=postgresql+asyncpg://...
LOG_LEVEL=INFO
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

**Logging via Python logging modul** (fix #16 — ikke print):
```python
import logging
log = logging.getLogger("monitor")
```

**Wallet label i log** (fix #13):
```python
def _w(wallet: str, label: str = "") -> str:
    return f"[{label}]" if label else f"[{wallet[:6]}…{wallet[-4:]}]"
```

### Tests (TDD-workflow fra `.ecc/tdd-workflow`)
- `test_diff_positions` — unit tests med kendte fixtures
- `test_fetch_positions_retry` — mock HTTP 429, verify eksponentiel backoff
- `test_db_writes` — verify korrekt event_type ved opened/closed/resized
- `test_ws_dynamic_subscribe` — verify at ny token tilføjes uden reconnect
- `tests/conftest.py` — pytest fixtures (fix #6, tilpasset Python i stedet for TS)

---

## Fase 3: Trade Executor

**ECC skills:** `.ecc/verification-loop`, `.ecc/backend-patterns`, `.ecc/security-review`  
**Fixes fra ECC-bugs-and-gaps:** #2 (explicit skill invocation), #19, #20

### Arkitektur

Executor kører som separat process. Lytter på `pg_notify('new_trade')` og reagerer sub-100ms.

```python
async def on_new_trade_event(event: TradeEvent):
    if not await passes_gates(event):
        return
    order = await size_order(event)
    if DRY_RUN:
        await db.log_paper_order(event, order)
        await telegram.send(f"📄 PAPER: {order}")
        return
    result = await submit_to_clob(order)
    await db.log_copy_order(event, order, result)
    await telegram.send(f"✅ LIVE: {order} → {result.status}")
```

### Gates (verification-loop mønster fra `.ecc/verification-loop`)

Alle gates skal passere — ét nej stopper eksekveringen:

```
Gate 1: Er wallet aktuelt på followed_wallets (unfollowed_at IS NULL)?
Gate 2: Er markedet likvidt? (bid-ask spread < 5% via CLOB orderbook)
Gate 3: Er vi ikke allerede eksponeret i dette marked? (CHECK positions tabel)
Gate 4: Er order-size inden for max risiko? (size <= available_cash * 0.20 hard cap)
Gate 5: Er der > 2 timer til markedets close?
Gate 6: Er dette en åbning eller scale-up (ikke resize ned)?
Gate 7: Er daglig loss limit ikke nået? (CHECK daily_stats.realized_pnl > -MAX_DAILY_LOSS)
```

### Position sizing

```python
# available_cash hentes fra CLOB API GET /balance ved hver trade-decision
available_cash = await clob.get_balance()

# position_size_pct: per-wallet override ELLER global default
pct = wallet.position_size_pct or float(os.getenv("POSITION_SIZE_PCT", "0.05"))

copy_size = available_cash * pct
```

### Paper trading mode

`DRY_RUN=true` i env → alle ordrer logges i `copy_orders` med `status='paper'`, ingen CLOB-submit.

### Performance-baseret go-live gate

```python
# Checker automatisk efter hver paper trade
paper_stats = await db.get_paper_stats()
if paper_stats.total >= 20 and paper_stats.win_rate > 0.52:
    await telegram.send_approval_request(
        text=f"🚀 Bot klar til live trading!\n"
             f"Win rate: {paper_stats.win_rate:.1%} over {paper_stats.total} trades\n"
             f"Vil du aktivere live trading?",
        buttons=[("✅ Klar — gå live", "go_live"), ("❌ Ikke klar", "stay_paper")]
    )
```

Telegram inline keyboard callback opdaterer `DRY_RUN` runtime (ingen restart nødvendig).

### Sikkerhed (kritisk — fix #20)

- Private key gemmes KUN som env var `POLYMARKET_PRIVATE_KEY`, aldrig i kode eller DB
- Brug Polymarket's L2 proxy wallet-mønster (vejledning leveres i Fase 3)
- `.env` + `python-dotenv` til lokal udvikling
- `.env` og `.env.*` i `.gitignore` (fix #8)
- AgentShield-scan inden første live deploy

### Polymarket Proxy Wallet Setup (ny bruger-vejledning)

Trin-for-trin vejledning leveres som del af Fase 3-implementeringen:
1. Opret Polymarket-konto og forbind Polygon wallet
2. Aktiver L2 proxy wallet i Polymarket UI
3. Eksportér private key til `.env` som `POLYMARKET_PRIVATE_KEY`
4. Test med `DRY_RUN=true` inden live

---

## Fase 4: Filter Scanner (manuelt CLI-værktøj)

**ECC skills:** `.ecc/eval-harness` (Python-tilpasset, fix #6), `.ecc/postgres-patterns`  
**Fixes fra ECC-bugs-and-gaps:** #22, #23

### Kommandoer

```bash
python filter.py scan 0xABC...           # Scan og score en wallet
python filter.py list --min-sortino 1.5  # List top wallets
python filter.py follow 0xABC... --label "whale-001" --size-pct 0.07
python filter.py unfollow 0xABC... --reason "inaktiv 30 dage"
python filter.py recalculate             # Genberegn alle scores + gem snapshot
```

### Score-beregning

| Metric | Vægt | Minimum |
|--------|------|---------|
| Win rate (min 100 trades) | 25% | > 55% |
| Sortino ratio | 30% | > 1.2 |
| Konsistens bull/bear | 20% | Forskel < 20% |
| Position sizing entropy | 15% | Ikke ensartet |
| Max drawdown | 10% | < 50% |
| ÅOP (annual_return_pct) | Tiebreaker | — |

`recalculate` gemmer altid et nyt snapshot i `wallet_score_snapshots`.

### Historisk backfill (fix #23)

```python
# Kør én gang per wallet ved første scan
# Rate-limit: 0.5 req/s (Polymarket Data API limit: ~2 req/s, vi er konservative)
async def backfill_wallet_history(wallet: str):
    ...
```

Kræver minimum 18 måneders data for konsistens-metrik. Wallets uden tilstrækkelig historik scores "ukendt" på konsistens — ikke diskvalificeret, men flagget.

---

## Fase 5: Deployment

**ECC skills:** `.ecc/docker-patterns`  
**Fixes fra ECC-bugs-and-gaps:** #21

### Docker Compose

```yaml
services:
  postgres:
    image: postgres:16
    volumes: [postgres_data:/var/lib/postgresql/data]
    environment:
      POSTGRES_DB: polymarket
      POSTGRES_USER: bot
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U bot"]
      interval: 10s

  monitor:
    build: .
    command: python monitor.py
    environment:
      DB_URL: ${DB_URL}
      FOLLOWED_WALLETS: ${FOLLOWED_WALLETS}
      POLL_INTERVAL: ${POLL_INTERVAL:-30}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID}
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      retries: 3

  executor:
    build: .
    command: python executor.py
    environment:
      DB_URL: ${DB_URL}
      POLYMARKET_PRIVATE_KEY: ${POLYMARKET_PRIVATE_KEY}
      MAX_DAILY_LOSS: ${MAX_DAILY_LOSS:-50}
      POSITION_SIZE_PCT: ${POSITION_SIZE_PCT:-0.05}
      DRY_RUN: ${DRY_RUN:-true}
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID}
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8081/health"]
      interval: 30s
      retries: 3
```

### Server

**Hetzner CX22** — Ashburn, Virginia datacenter (us-east-1, tæt på Polymarket's CLOB-infrastruktur)
- 2 vCPU, 4GB RAM, 40GB SSD
- ~€5/måned
- Setup: Docker + Docker Compose + ufw firewall

### Git + Claude Code workflow

```
Lokal: claude (Claude Code kører i projekt-mappen)
         │
         ▼
    GitHub (privat repo)
         │
         ▼
   Railway auto-deploy  ─── ELLER ─── Hetzner: git pull + docker compose up -d
```

Claude Code opretter filer direkte i projektet og committer til git. Du reviewer på GitHub.

### Health monitoring

- Monitor + executor eksponerer `/health` endpoint
- PostgreSQL backup dagligt (Hetzner Snapshots eller Backblaze)
- Telegram alert ved service-nedbrud (3 missede health checks = alert)
- Daglig Telegram-rapport: antal trades, P&L, aktive wallets

---

## Teknisk stack

| Komponent | Teknologi |
|-----------|-----------|
| Sprog | Python 3.11+ |
| Database | PostgreSQL 16 |
| Migrations | Alembic |
| Async | asyncio + websockets |
| HTTP | httpx (async) |
| DB driver | asyncpg (async) / psycopg2 (Alembic) |
| Notifications | Telegram Bot API (python-telegram-bot) |
| Deployment | Docker Compose, Hetzner CX22 Ashburn |
| CI/CD | GitHub → Railway (eller Hetzner git pull) |
| Tests | pytest + pytest-asyncio |
| Config | python-dotenv + env vars |
| Linting | ruff + black + mypy |

---

## ECC Skills brugt

| Fase | ECC Skill |
|------|-----------|
| Alle | `.ecc/rules/common/` + `.ecc/rules/python/` |
| 1, 2, 4 | `.ecc/postgres-patterns` |
| 1 | `.ecc/database-migrations` |
| 2 | `.ecc/continuous-agent-loop` (alias: autonomous-loops) |
| 2, 3 | `.ecc/backend-patterns` |
| 2, 3, 4 | `.ecc/tdd-workflow` |
| 3 | `.ecc/verification-loop` |
| 3 | `.ecc/security-review` |
| 4 | `.ecc/eval-harness` (Python-tilpasset) |
| 5 | `.ecc/docker-patterns` |

> **Vigtigt (ECC bug #2):** Claude Code loader skills semantisk, ikke deterministisk.
> Brug eksplicit slash-invocation i kritiske sessions: `/skills/verification-loop` før executor-kode.

---

## ECC + Monitor bugs integreret

Alle 23 punkter fra `ECC-bugs-and-gaps.md` er adresseret:

| # | Prioritet | Status | Fase |
|---|-----------|--------|------|
| 1 | 🔴 | autonomous-loops → continuous-agent-loop | CLAUDE.md |
| 2 | 🔴 | Explicit skill invocation i CLAUDE.md | CLAUDE.md |
| 3 | 🔴 | SIGTERM flush i session-end | CLAUDE.md |
| 4 | 🟠 | CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=65 | CLAUDE.md |
| 5 | 🟠 | Hook-rækkefølge i settings.json | CLAUDE.md |
| 6 | 🟠 | pytest fixtures i conftest.py | Fase 2 |
| 7 | 🟡 | Ignorer search-first URL-refs | N/A |
| 8 | 🟡 | .gitignore (memory.md, .env) | Fase 1 |
| 9 | 🔴 | DB-writes i monitor | Fase 2 |
| 10 | 🔴 | WS dynamic re-subscribe | Fase 2 |
| 11 | 🔴 | Retry-logik fetch_positions | Fase 2 |
| 12 | 🟠 | Async HTTP via executor pool | Fase 2 |
| 13 | 🟠 | Wallet label i log | Fase 2 |
| 14 | 🟠 | Health-check endpoint | Fase 2 |
| 15 | 🟡 | Env var konfiguration | Fase 2 |
| 16 | 🟡 | Python logging modul | Fase 2 |
| 17 | 🔴 | db.py modul | Fase 1 |
| 18 | 🔴 | Alembic migration setup | Fase 1 |
| 19 | 🔴 | Trade executor | Fase 3 |
| 20 | 🔴 | Secret-håndtering + proxy wallet | Fase 3 |
| 21 | 🟠 | Docker Compose | Fase 5 |
| 22 | 🟠 | Filter-scanner CLI | Fase 4 |
| 23 | 🟠 | Backfill-script | Fase 4 |

---

## Rækkefølge og dependencies

```
Fase 1 (DB + db.py) 
  → Fase 2 (Monitor udvidelse)
  → Fase 4 (Filter Scanner, parallel med 2)
  → Fase 3 (Executor — starter efter Fase 2 har kørt 1 uge paper)
  → Go-live gate (Telegram approval når win_rate > 52% over ≥20 paper trades)
  → Fase 5 (Deployment — når 2+3 er stabile)
```

---

## Risici

| Risiko | Sandsynlighed | Mitigering |
|--------|--------------|------------|
| Wallet stopper med at trade | Høj | Auto-unfollow ved 0 trades i 30 dage |
| Polymarket API-ændringer | Medium | Abstraher API-kald bag interface-lag |
| Rate limiting | Medium | Eksponentiel backoff + cache market_metadata |
| Privat nøgle kompromitteret | Lav men kritisk | Env vars only, aldrig i kode/DB, rotér jævnligt |
| Kopieret wallet er faktisk dårlig | Medium | Filter-systemet + lille position_size_pct til at starte |
| Paper-live divergens | Medium | Gate-checks er identiske i DRY_RUN og live |

---

## Hvad dette projekt IKKE er

- Ikke en arbitrage-bot (for langsom)
- Ikke en MEV-bot (forkert kæde)  
- Ikke en AI-drevet prediktor
- Filter-systemet er ikke automatisk — det er et manuelt analyse-værktøj

Edge-window: 30-120 sekunder fra whale-trade detekteres til markedspris adjusterer. pg_notify giver sub-100ms reaktion fra monitor → executor.
