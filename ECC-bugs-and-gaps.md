# ECC + monitor.py — Fejl, mangler og fixes
**Til:** Cowork / Claude Code  
**Formål:** Komplet liste over kendte problemer i ECC-pakken og monitor.py der skal adresseres under projektudvikling  
**Prioritet:** 🔴 Kritisk → 🟠 Vigtig → 🟡 Nice-to-have

---

## DEL 1: ECC-pakken — kendte problemer og workarounds

### 🔴 1. `autonomous-loops` skill er deprecated under det navn

**Problem:** Skillen hedder `autonomous-loops` i repoen, men den kanoniske skill i Claude Code 2.x er omdøbt til `continuous-agent-loop`. Repoen selv skriver: *"autonomous-loops is retained for one release. The canonical skill name is now continuous-agent-loop."*

**Konsekvens:** Claude Code kan ignorere eller fejlindlæse skill-referencer der bruger det gamle navn.

**Fix:** Når du refererer til skillen i CLAUDE.md og prompts, brug altid:
```
skills/autonomous-loops   ← gammel reference, virker stadig men deprecated
```
Og tilføj eksplicit i din `CLAUDE.md`:
```
For loop-arkitektur: se skills/autonomous-loops/SKILL.md (alias: continuous-agent-loop)
```

---

### 🔴 2. Skills aktiveres semantisk — ikke deterministisk

**Problem:** Claude Code beslutter *selv* hvornår den loader en skill baseret på semantisk reasoning. Du kan ikke force-invoke dem. Det betyder at Claude kan ignorere `verification-loop` på et kritisk tidspunkt fordi den ikke "genkender" at det er relevant.

**Konsekvens for trading bot:** Gate-checks i Fase 3 (executor) kan springes over.

**Fix:** Tilføj explicit invocation i `CLAUDE.md` for de kritiske skills:
```markdown
## Mandatory Skills
Before writing ANY trade execution code, explicitly invoke:
- skills/verification-loop (gate checks before order submission)
- skills/security-review (secrets handling)

Run these with: /skills/verification-loop and /skills/security-review
```
Og brug slash-command i sessions: `/skills/verification-loop`

---

### 🔴 3. `memory-persistence` hook bridger IKKE kontekst mellem `-p` kald korrekt

**Problem:** Hook'en er designet til interaktive sessioner. Ved `claude -p` (non-interaktiv/headless) kald — som en trading bot ville bruge — opdateres memory-filen ikke altid korrekt ved session-afslutning fordi `Stop`-hook'en ikke altid fyres ved process-exit.

**Konsekvens:** Context der burde persistere (f.eks. "vi er i gang med Fase 2, migration kørt") går tabt.

**Fix:** I `scripts/session-end.js`, tilføj explicit flush ved SIGTERM:
```javascript
process.on('SIGTERM', async () => {
  await flushMemory();
  process.exit(0);
});
```
Og verificer at `hooks/memory-persistence/` er konfigureret med `"events": ["Stop", "SubagentStop"]` i `settings.json`.

---

### 🟠 4. `strategic-compact` hook kompakter for aggressivt ved >50% context

**Problem:** ECC sætter `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50` — dvs. compaction sker når context er halvt fyldt. Det er cost-optimalt men kan nuppe vigtig kontekst ved database-migrations og multi-fil refactors.

**Konsekvens:** Claude mister "hvad vi var i gang med" midt i en Alembic migration-session.

**Fix:** For dette projekt, skift til 65% i `.claude/settings.json`:
```json
{
  "env": {
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "65"
  }
}
```
Og tilføj i `CLAUDE.md`:
```markdown
## Compact Instructions
When compacting, always preserve:
- Current migration version and pending changes
- Active database schema decisions
- Gate logic in executor
- Which phase of PRD we are in
```

---

### 🟠 5. `hooks/strategic-compact` og `hooks/memory-persistence` er i konflikt

**Problem:** Begge hooks lytter på `Stop`-eventet. Rækkefølgen de kører i er ikke garanteret. Hvis `strategic-compact` kører før `memory-persistence`, kan den compactede output overskrive memory-state.

**Konsekvens:** Memory-fil kan ende med compactet (kortere) kontekst i stedet for fuld session-state.

**Fix:** I `settings.json`, definer eksplicit hook-rækkefølge:
```json
{
  "hooks": {
    "Stop": [
      { "script": "scripts/session-end.js" },     // memory-persistence først
      { "script": "scripts/pre-compact.js" }       // strategic-compact sidst
    ]
  }
}
```

---

### 🟠 6. `eval-harness` mangler Python-specifik fixture-støtte

**Problem:** `eval-harness` skillen er primært skrevet med TypeScript/Node.js i tankerne. Fixture-hjælperne i `tests/helpers.ts` virker ikke direkte for Python pytest-setups.

**Konsekvens:** Du skal bygge pytest-fixtures manuelt i stedet for at bruge ECC's eval-patterns.

**Fix:** Cowork skal oprette `tests/conftest.py` med:
```python
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

@pytest.fixture
def mock_positions():
    """Standard fixture matching eval-harness pattern"""
    return [...]  # kendte test-positions fra Polymarket

@pytest_asyncio.fixture
async def db_session():
    """Async test DB session"""
    ...
```
Og tilpas `eval-harness` referencer i `CLAUDE.md` til at pege på pytest-filen.

---

### 🟡 7. `search-first` skill kalder eksterne URLs der ikke længere eksisterer

**Problem:** Skillen refererer til `perplexity.ai` og `you.com` som search-backends. Begge API-formater har ændret sig siden skillen blev skrevet.

**Konsekvens:** Lav/ingen effekt for dette projekt da vi bruger Polymarket's egne APIs — ikke generel web-search.

**Fix:** Ingen handling nødvendig for trading bot. Ignorer search-first's URL-referencer.

---

### 🟡 8. `continuous-learning-v2` skriver til `.claude/memory.md` — konflikter med git

**Problem:** `memory.md` opdateres automatisk af learning-systemet og er ikke i `.gitignore` by default.

**Konsekvens:** Sensitiv session-kontekst (f.eks. hvilke wallets du følger, P&L-info) kan blive committed til git.

**Fix:** Tilføj til `.gitignore` i roden:
```
.claude/memory.md
.claude/sessions/
*.env
.env.*
```

---

## DEL 2: monitor.py — fejl og mangler

### 🔴 9. Ingen database-writes — scriptet printer kun

**Problem:** Al output går til `stdout`. Ingen persistering af positions, trade-events eller diff-resultater.

**Konsekvens:** Filter-systemet og executor har intet datagrundlag. Bot kan ikke eksekvere noget.

**Fix (Fase 2):** Tilføj `db.py` modul og writes ved alle events:
```python
# ved opened:
await db.insert_trade_event(wallet, pos, "opened")
await db.upsert_position(wallet, pos)

# ved closed:
await db.insert_trade_event(wallet, pos, "closed")
await db.mark_position_closed(wallet, condition_id, outcome)
```

---

### 🔴 10. WebSocket genstarter HELE forbindelsen ved nye tokens

**Problem:** Når en ny position detekteres, annulleres ws_task og genstartes fra scratch:
```python
ws_task.cancel()
...
ws_task = asyncio.create_task(ws_price_loop(...))
```
Dette giver 5+ sekunders blindspot på alle eksisterende subscriptions.

**Konsekvens:** Potentielle trades misses under reconnect-vinduet.

**Fix:** Brug dynamisk re-subscribe i stedet:
```python
# I stedet for at genstarte — send ny subscription besked:
if ws_needs_restart and ws_connection:
    new_sub = json.dumps({
        "assets_ids": new_ids,  # KUN de nye tokens
        "type": "market"
    })
    await ws_connection.send(new_sub)
    # ws_needs_restart = False — ingen restart nødvendig
```
Kræver at `ws_connection` eksponeres fra `ws_price_loop`.

---

### 🔴 11. Ingen retry-logik på `fetch_positions`

**Problem:** Hvis Polymarket API returnerer 429 (rate limit) eller 5xx, crasher poll-loopet med `RequestException` og logger kun fejlen. Næste poll sker om 30 sekunder.

**Konsekvens:** Trades kan missees i et 30s vindue efter en API-fejl.

**Fix:** Tilføj eksponentiel backoff:
```python
async def fetch_positions_with_retry(wallet: str, max_attempts: int = 3) -> list[dict]:
    for attempt in range(max_attempts):
        try:
            return fetch_positions(wallet)
        except requests.HTTPError as e:
            if e.response.status_code == 429:
                wait = 2 ** attempt  # 1s, 2s, 4s
                await asyncio.sleep(wait)
            else:
                raise
    return []  # returner tom liste efter max attempts, log warning
```

---

### 🟠 12. `fetch_positions` er synkron i en async context

**Problem:** `fetch_positions()` bruger synkron `requests` bibliotek og kaldes direkte i den async poll-loop. Det blokerer event-loopet under HTTP-kald.

**Konsekvens:** Mens positions hentes for wallet A, kan WebSocket-events fra wallet B/C bufferes op og ankommer forsinket.

**Fix:** Kør synkrone HTTP-kald i executor pool:
```python
current = await asyncio.get_event_loop().run_in_executor(
    None, fetch_positions, wallet
)
```
Eller migrer til `httpx` med async support (anbefalet langsigtet).

---

### 🟠 13. `_w()` wallet-tag er for kort til fejlfinding

**Problem:** `[0x0b7a…86cf]` er svær at skelne ved hurtig log-scanning, særligt med 5+ wallets.

**Fix:** Brug konfigureret label fra DB:
```python
def _w(wallet: str, label: str = "") -> str:
    if label:
        return f"[{label}]"
    return f"[{wallet[:6]}…{wallet[-4:]}]"
```

---

### 🟠 14. Ingen health-check endpoint

**Problem:** Docker-container har ingen måde at rapportere "jeg lever og poller korrekt" på.

**Konsekvens:** Container kan sidde i en zombie-tilstand (ingen crash men ingen polling) uden at orchestratoren opdager det.

**Fix:** Tilføj simpel HTTP health server:
```python
from aiohttp import web

async def health_handler(request):
    age = time.time() - last_successful_poll
    if age > interval * 3:  # 3 missede polls = unhealthy
        return web.Response(status=503, text="stale")
    return web.Response(text="ok")

# Start ved siden af main loop:
app = web.Application()
app.router.add_get('/health', health_handler)
runner = web.AppRunner(app)
await runner.setup()
site = web.TCPSite(runner, '0.0.0.0', 8080)
await site.start()
```

---

### 🟡 15. Ingen konfiguration via environment variables

**Problem:** `DEFAULT_WALLETS` og `POLL_INTERVAL` er hardcodet. Wallet-listen styres via CLI-argument, ikke env vars.

**Konsekvens:** Docker Compose kan ikke konfigurere wallets via environment.

**Fix:**
```python
import os
DEFAULT_WALLETS = os.getenv("FOLLOWED_WALLETS", "").split(",") or ["0x..."]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
```

---

### 🟡 16. `display_positions()` printer til stdout — ingen log levels

**Problem:** Alt output er på samme niveau. Fejl, info og price-updates er umulige at filtrere.

**Fix:** Skift til Python `logging` modul:
```python
import logging
log = logging.getLogger("monitor")
log.info(f"{tag} Found {len(positions)} open position(s)")
log.warning(f"{tag} [POLL] Request error: {e}")
log.debug(f"{tag} price update: {prev:.3f} -> {price:.3f}")
```

---

## DEL 3: Mangler der skal bygges fra bunden (ikke i ECC)

### 🔴 17. Ingen `db.py` modul overhovedet

Hverken ECC eller scriptet har et database-lag. Skal bygges i Fase 1.

### 🔴 18. Ingen Alembic migration setup

Skal oprettes: `alembic.ini`, `alembic/env.py`, og migrations for alle 6 tabeller fra PRD.

### 🔴 19. Ingen trade executor

Fase 3 i PRD. Ingen del af ECC eller scriptet dækker CLOB-ordre-submission til Polymarket.

### 🔴 20. Ingen secret-håndtering

Private key til Polymarket proxy wallet eksisterer ikke endnu i projektet. Skal håndteres via `.env` + `python-dotenv`. AgentShield skal scannes inden deploy.

### 🟠 21. Ingen `docker-compose.yml`

Fase 5. Monitor og executor skal containeriseres med korrekt restart-politik og health-checks.

### 🟠 22. Ingen filter-scanner CLI

Fase 4. `filter.py` med `scan`, `list`, `follow`, `unfollow`, `recalculate` kommandoer.

### 🟠 23. Ingen backfill-script til historiske trades

Filter-systemets Sortino og konsistens-metrics kræver historisk data. Skal scrapes fra Polymarket Data API med rate-limit-respekt.

---

## Prioriteret arbejdsrækkefølge til Cowork

```
SPRINT 1 (Fase 1):
  Fix #18 → Alembic setup
  Fix #17 → db.py modul (asyncpg pool)
  Fix #8  → .gitignore

SPRINT 2 (Fase 2 — monitor):
  Fix #9  → database writes
  Fix #11 → retry logik
  Fix #12 → async HTTP
  Fix #15 → env var config
  Fix #16 → logging

SPRINT 3 (Fase 2 — optimering):
  Fix #10 → WS dynamic re-subscribe
  Fix #14 → health endpoint
  Fix #2  → explicit skill invocation i CLAUDE.md
  Fix #3  → memory-persistence SIGTERM

SPRINT 4 (Fase 3):
  Fix #19 → trade executor + gates
  Fix #20 → secret håndtering

SPRINT 5 (Fase 4+5):
  Fix #22 → filter CLI
  Fix #23 → backfill script
  Fix #21 → Docker Compose
```
