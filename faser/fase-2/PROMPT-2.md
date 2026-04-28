# Fase 2 — Wallet Monitor (udvidelse af monitor.py)
**Til:** Ny Cowork-session  
**Projekt:** Polymarket Copy-Trading Bot  
**Arbejdsmappe:** polymarket-bot/  
**Branch:** opret `fase-2-monitor` fra `main`

---

## Din første handling — læs disse filer INDEN du skriver en eneste linje kode:

```
Read CLAUDE.md
Read PRD.md
Read ECC-bugs-and-gaps.md
Read .ecc/continuous-agent-loop/SKILL.md
Read .ecc/backend-patterns/SKILL.md
Read .ecc/tdd-workflow/SKILL.md
Read .ecc/rules/python/coding-style.md
Read .ecc/rules/common/security.md
```

Læs derefter den eksisterende kode du skal udvide:
```
Read monitor.py
Read db.py
Read tests/conftest.py
```

---

## Kontekst

`monitor.py` er et eksisterende script der overvåger Polymarket-wallets via REST polling + WebSocket. Det virker, men har 8 kritiske mangler der skal fixes i denne fase (dokumenteret i `ECC-bugs-and-gaps.md` som fix #9-#16).

**Vigtigt:** Du omskriver IKKE scriptet fra bunden. Du udvider og forbedrer det eksisterende. Al eksisterende logik (diff_positions, ws_price_loop, poll-loop, resolve_token_ids) bevares og forbedres på plads.

---

## Mål for denne session

Udvid `monitor.py` så det:
1. Skriver alle position-events til databasen via `db.py`
2. Bruger dynamisk WebSocket re-subscribe i stedet for fuld restart
3. Har retry-logik med eksponentiel backoff på `fetch_positions`
4. Kører HTTP i async executor pool (blokerer ikke event-loopet)
5. Konfigureres via environment variables
6. Logger via Python `logging` modul — ingen `print()` i produktion
7. Eksponerer `/health` endpoint på port 8080
8. Bruger wallet-label i loglinjer når tilgængeligt

Plus tests til alle nye funktioner.

**Branch-strategi:** Opret `fase-2-monitor` fra `main` ved sessionstart:
```bash
git checkout main
git pull
git checkout -b fase-2-monitor
```

---

## Trin 1 — Konfiguration via environment variables (fix #15)

Erstat hardkodede konstanter øverst i `monitor.py`. Nye imports:

```python
import logging
import os
from dotenv import load_dotenv

load_dotenv()

# ── konfiguration via env vars ─────────────────────────────────────────────────
DEFAULT_WALLETS: list[str] = [
    w.strip()
    for w in os.getenv("FOLLOWED_WALLETS", "0x0b7a6030507efe5db145fbb57a25ba0c5f9d86cf").split(",")
    if w.strip()
]
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "30"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
DB_URL: str = os.getenv("DB_URL", "")
```

Commit: `feat(monitor): add env var configuration (fix #15)`

---

## Trin 2 — Logging via Python logging modul (fix #16)

Erstat AL brug af `print()` og `_safe()` med structured logging.

**Slet** `_safe()` funktionen.

Tilføj øverst i filen (efter imports):
```python
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")
```

Erstatningsregler:
- `_safe(f"[{_ts()}] ...")` → `log.info("...")`
- `_safe(f"[{_ts()}] [WS] ...")` → `log.debug("...")` (price-updates er debug-niveau)
- `_safe(f"[{_ts()}] {tag} [POLL] Request error: {e}")` → `log.warning("%s poll error: %s", tag, e)`
- `_safe(f"[{_ts()}] {tag} [POLL] Error: {e}")` → `log.exception("%s unexpected error", tag)`

**`_ts()` funktionen slettes** — logging-modulet håndterer timestamps automatisk.

Opdater `_w()` til at tage en optional label (fix #13):
```python
def _w(wallet: str, label: str = "") -> str:
    """Kort wallet-tag til loglinjer."""
    if label:
        return f"[{label}]"
    return f"[{wallet[:6]}…{wallet[-4:]}]"
```

Commit: `feat(monitor): replace print() with logging module (fix #16, #13)`

---

## Trin 3 — Database-writes på alle events (fix #9)

### 3a — DB-hjælpefunktioner

Tilføj et nyt afsnit `# ── database helpers ──` i `monitor.py` efter imports:

```python
async def _db_upsert_position(conn: asyncpg.Connection, wallet_id: int, pos: dict) -> None:
    """Upsert én position fra Polymarket API-response til positions-tabellen."""
    await conn.execute(
        """
        INSERT INTO positions (
            wallet_id, condition_id, outcome, size, avg_price, cur_price,
            current_value, cash_pnl, percent_pnl, token_id, title,
            first_seen_at, last_updated_at, status
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,now(),now(),'open')
        ON CONFLICT (wallet_id, condition_id, outcome)
        DO UPDATE SET
            size            = EXCLUDED.size,
            avg_price       = EXCLUDED.avg_price,
            cur_price       = EXCLUDED.cur_price,
            current_value   = EXCLUDED.current_value,
            cash_pnl        = EXCLUDED.cash_pnl,
            percent_pnl     = EXCLUDED.percent_pnl,
            last_updated_at = now()
        """,
        wallet_id,
        pos.get("conditionId", ""),
        pos.get("outcome", ""),
        _decimal(pos.get("size")),
        _decimal(pos.get("avgPrice") or pos.get("buyAvg")),
        _decimal(pos.get("curPrice")),
        _decimal(pos.get("currentValue")),
        _decimal(pos.get("cashPnl")),
        _decimal(pos.get("percentPnl")),
        pos.get("asset", ""),
        pos.get("title") or pos.get("slug", ""),
    )


async def _db_mark_closed(conn: asyncpg.Connection, wallet_id: int, pos: dict) -> None:
    """Marker en position som closed i databasen."""
    await conn.execute(
        """
        UPDATE positions SET status = 'closed', last_updated_at = now()
        WHERE wallet_id = $1
          AND condition_id = $2
          AND outcome = $3
          AND status = 'open'
        """,
        wallet_id,
        pos.get("conditionId", ""),
        pos.get("outcome", ""),
    )


async def _db_insert_trade_event(
    conn: asyncpg.Connection,
    wallet_id: int,
    event_type: str,
    new_pos: dict,
    old_pos: dict | None = None,
) -> None:
    """Indsæt én immutable trade_event-række."""
    await conn.execute(
        """
        INSERT INTO trade_events (
            wallet_id, condition_id, outcome, event_type,
            old_size, new_size, price_at_event, pnl_at_close
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """,
        wallet_id,
        new_pos.get("conditionId", ""),
        new_pos.get("outcome", ""),
        event_type,
        _decimal(old_pos.get("size")) if old_pos else None,
        _decimal(new_pos.get("size")),
        _decimal(new_pos.get("curPrice")),
        _decimal(new_pos.get("cashPnl")) if event_type == "closed" else None,
    )


def _decimal(value: object) -> float | None:
    """Konvertér API-streng til float — returnér None ved None/tom streng."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
```

### 3b — Wallet-ID lookup

Tilføj hjælpefunktion til at hente (eller oprette) wallet_id fra DB:

```python
async def _get_or_create_wallet_id(conn: asyncpg.Connection, address: str) -> int:
    """Returnér wallet.id — indsæt wallets-rækken hvis den ikke findes."""
    row = await conn.fetchrow(
        "SELECT id FROM wallets WHERE address = $1", address
    )
    if row:
        return row["id"]
    new_id = await conn.fetchval(
        "INSERT INTO wallets (address) VALUES ($1) RETURNING id", address
    )
    log.info("Inserted new wallet: %s → id=%s", address[:10], new_id)
    return new_id
```

### 3c — Integrer DB-writes i poll-loopet

I `main()`-funktionens poll-loop, erstat den eksisterende diff-håndtering:

```python
# ERSTAT det eksisterende opened/closed/changed-blok med:
if opened or closed or changed:
    if DB_URL:
        try:
            async with acquire() as conn:
                wallet_id = await _get_or_create_wallet_id(conn, wallet)
                for p in opened:
                    await _db_insert_trade_event(conn, wallet_id, "opened", p)
                    await _db_upsert_position(conn, wallet_id, p)
                for p in closed:
                    await _db_insert_trade_event(conn, wallet_id, "closed", p)
                    await _db_mark_closed(conn, wallet_id, p)
                for old_p, new_p in changed:
                    await _db_insert_trade_event(conn, wallet_id, "resized", new_p, old_p)
                    await _db_upsert_position(conn, wallet_id, new_p)
        except Exception:
            log.exception("%s DB write failed — continuing without persistence", tag)
    # Bevar eksisterende log-output for opened/closed/changed her...
```

Commit: `feat(monitor): add db writes on all position events (fix #9)`

---

## Trin 4 — Async HTTP via executor pool (fix #12)

`fetch_positions()` er synkron (`requests`-bibliotek) og blokerer event-loopet.

Wrap alle kald til `fetch_positions()` og `fetch_user_stats()` i executor:

```python
# I stedet for:
positions = fetch_positions(wallet)

# Brug:
positions = await asyncio.get_event_loop().run_in_executor(
    None, fetch_positions, wallet
)
```

Gælder ALLE steder i `main()` og startup-koden hvor disse funktioner kaldes.

Commit: `feat(monitor): run blocking HTTP in executor pool (fix #12)`

---

## Trin 5 — Retry-logik på fetch_positions (fix #11)

Tilføj ny async wrapper-funktion:

```python
async def fetch_positions_with_retry(
    wallet: str,
    max_attempts: int = 3,
) -> list[dict]:
    """Hent positioner med eksponentiel backoff ved 429/5xx.
    
    Returnerer tom liste efter max_attempts mislykkede forsøg.
    """
    loop = asyncio.get_event_loop()
    for attempt in range(max_attempts):
        try:
            return await loop.run_in_executor(None, fetch_positions, wallet)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 429 or status >= 500:
                wait = 2 ** attempt  # 1s, 2s, 4s
                log.warning(
                    "%s HTTP %s — retry %d/%d in %ds",
                    _w(wallet), status, attempt + 1, max_attempts, wait,
                )
                await asyncio.sleep(wait)
            else:
                log.error("%s HTTP error %s — giving up", _w(wallet), status)
                return []
        except requests.RequestException as exc:
            log.warning("%s request error attempt %d: %s", _w(wallet), attempt + 1, exc)
            if attempt < max_attempts - 1:
                await asyncio.sleep(2 ** attempt)
    log.warning("%s all %d attempts failed — skipping this poll", _w(wallet), max_attempts)
    return []
```

Erstat alle kald til `fetch_positions(wallet)` i poll-loopet med `fetch_positions_with_retry(wallet)`.

Commit: `feat(monitor): add exponential backoff retry on fetch_positions (fix #11)`

---

## Trin 6 — Dynamisk WebSocket re-subscribe (fix #10)

**Problem:** Når nye tokens opdages, annulleres hele `ws_task` og genstartes. Det giver et 5+ sekunders blindspot.

**Løsning:** Eksponér WebSocket-forbindelsen så nye tokens kan tilføjes dynamisk uden reconnect.

Tilføj en `asyncio.Queue` til at sende nye token-IDs til den kørende WS-forbindelse:

```python
async def ws_price_loop(
    token_ids: list[str],
    token_map: dict[str, dict],
    token_wallet: dict[str, str],
    last_prices: dict[str, float],
    new_tokens_queue: asyncio.Queue[list[str]],   # ← NY PARAMETER
) -> None:
    """WebSocket loop med dynamisk re-subscribe support."""
    ...
    while True:
        try:
            async with websockets.connect(CLOB_WS, ping_interval=None, close_timeout=5) as ws:
                # Initial subscription
                await ws.send(json.dumps({"assets_ids": token_ids, "type": "market"}))
                log.info("[WS] subscribed to %d token(s)", len(token_ids))

                async def keepalive() -> None:
                    while True:
                        await asyncio.sleep(10)
                        try:
                            await ws.send("PING")
                        except Exception:
                            break

                async def drain_new_tokens() -> None:
                    """Lyt på køen og send ny SUBSCRIBE-besked — ingen reconnect."""
                    while True:
                        new_ids = await new_tokens_queue.get()
                        try:
                            await ws.send(json.dumps({
                                "assets_ids": new_ids,
                                "type": "market",
                            }))
                            log.info("[WS] dynamically subscribed to %d new token(s)", len(new_ids))
                        except Exception as exc:
                            log.warning("[WS] failed to send dynamic subscribe: %s", exc)

                ka = asyncio.create_task(keepalive())
                dt = asyncio.create_task(drain_new_tokens())

                try:
                    async for raw in ws:
                        # ... eksisterende message-handling uændret ...
                        pass
                except websockets.ConnectionClosed:
                    log.warning("[WS] connection closed, reconnecting...")
                finally:
                    ka.cancel()
                    dt.cancel()
        except Exception as exc:
            log.error("[WS] error: %s", exc)
        await asyncio.sleep(5)
```

I `main()`: opret køen og send nye tokens via den i stedet for at genstarte ws_task:

```python
# Opret køen
new_tokens_queue: asyncio.Queue[list[str]] = asyncio.Queue()

# Start WS med køen
ws_task = asyncio.create_task(
    ws_price_loop(token_ids, combined_token_map, combined_token_wallet,
                  last_prices, new_tokens_queue)
)

# I poll-loopet — ERSTAT ws_needs_restart-logikken med:
if new_tmap:
    combined_token_map.update(new_tmap)
    for tid in new_tmap:
        combined_token_wallet[tid] = wallet
    new_ids = list(new_tmap.keys())
    token_ids.extend(new_ids)
    await new_tokens_queue.put(new_ids)   # ← ingen restart
    log.info("[WS] queued %d new token(s) from %s", len(new_ids), tag)
```

**Slet** al `ws_needs_restart`-logik og `ws_task.cancel()`-kald fra poll-loopet.

Commit: `feat(monitor): dynamic ws re-subscribe — no reconnect on new tokens (fix #10)`

---

## Trin 7 — Health-check endpoint (fix #14)

Tilføj en simpel aiohttp-server der eksponerer `/health` på port 8080:

```python
import time
from aiohttp import web

_last_successful_poll: float = 0.0


async def _start_health_server(poll_interval: int) -> web.AppRunner:
    """Start /health endpoint på port 8080."""
    async def health_handler(request: web.Request) -> web.Response:
        age = time.time() - _last_successful_poll
        if _last_successful_poll == 0.0:
            return web.Response(status=503, text="not_started")
        if age > poll_interval * 3:
            return web.Response(
                status=503,
                text=f"stale:{age:.0f}s",
            )
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    log.info("Health endpoint started on :8080/health")
    return runner
```

I poll-loopet: opdater `_last_successful_poll = time.time()` efter hvert vellykket poll for én wallet.

I `main()`:
```python
# Start health server
health_runner = await _start_health_server(interval)

try:
    # ... eksisterende poll-loop ...
finally:
    ws_task.cancel()
    await health_runner.cleanup()   # ← shutdown health server
```

Commit: `feat(monitor): add /health endpoint on port 8080 (fix #14)`

---

## Trin 8 — Tests (TDD-workflow fra .ecc/tdd-workflow)

Opret `tests/test_monitor.py` med følgende tests. Kør dem løbende under implementeringen.

```python
"""
tests/test_monitor.py — Unit tests for monitor.py

Bruger fixtures fra conftest.py.
Ingen rigtig database eller HTTP-kald.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import requests


# ── test_diff_positions ────────────────────────────────────────────────────────

def test_diff_positions_opened(mock_positions):
    """Ny position der ikke fandtes i old → opened."""
    from monitor import diff_positions
    opened, closed, changed = diff_positions([], mock_positions)
    assert len(opened) == 2
    assert len(closed) == 0
    assert len(changed) == 0


def test_diff_positions_closed(mock_positions):
    """Position der fandtes i old men ikke i new → closed."""
    from monitor import diff_positions
    opened, closed, changed = diff_positions(mock_positions, [])
    assert len(opened) == 0
    assert len(closed) == 2
    assert len(changed) == 0


def test_diff_positions_resized(mock_positions):
    """Position med ændret størrelse → changed."""
    from monitor import diff_positions
    old = mock_positions.copy()
    new = [dict(p) for p in mock_positions]
    new[0]["size"] = "150.0"   # ændret fra 100.0
    opened, closed, changed = diff_positions(old, new)
    assert len(opened) == 0
    assert len(closed) == 0
    assert len(changed) == 1
    assert changed[0][1]["size"] == "150.0"


def test_diff_positions_no_change(mock_positions):
    """Identiske lister → ingen events."""
    from monitor import diff_positions
    opened, closed, changed = diff_positions(mock_positions, mock_positions)
    assert opened == []
    assert closed == []
    assert changed == []


# ── test_fetch_positions_retry ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_positions_retry_on_429():
    """429-svar skal trigge 3 retry-forsøg med backoff."""
    mock_response = MagicMock()
    mock_response.status_code = 429
    http_error = requests.HTTPError(response=mock_response)

    call_count = 0

    def flaky_fetch(wallet: str):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise http_error
        return []   # succes på 3. forsøg

    with patch("monitor.fetch_positions", side_effect=flaky_fetch):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            from monitor import fetch_positions_with_retry
            result = await fetch_positions_with_retry("0xtest", max_attempts=3)

    assert call_count == 3
    assert result == []


@pytest.mark.asyncio
async def test_fetch_positions_retry_gives_up_after_max():
    """Alle forsøg fejler → returnér tom liste, ingen exception."""
    mock_response = MagicMock()
    mock_response.status_code = 429
    http_error = requests.HTTPError(response=mock_response)

    with patch("monitor.fetch_positions", side_effect=http_error):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            from monitor import fetch_positions_with_retry
            result = await fetch_positions_with_retry("0xtest", max_attempts=3)

    assert result == []


# ── test_db_writes ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_db_insert_trade_event_opened(mock_positions):
    """opened-event indsætter korrekte værdier i trade_events."""
    from monitor import _db_insert_trade_event
    mock_conn = AsyncMock()
    pos = mock_positions[0]

    await _db_insert_trade_event(mock_conn, wallet_id=1, event_type="opened", new_pos=pos)

    mock_conn.execute.assert_called_once()
    call_args = mock_conn.execute.call_args[0]
    # event_type er 4. parameter ($4)
    assert call_args[4] == "opened"
    # pnl_at_close skal være None for opened-event ($8)
    assert call_args[8] is None


@pytest.mark.asyncio
async def test_db_insert_trade_event_closed_has_pnl(mock_positions):
    """closed-event skal inkludere cashPnl som pnl_at_close."""
    from monitor import _db_insert_trade_event
    mock_conn = AsyncMock()
    pos = mock_positions[0]  # cashPnl = "7.00"

    await _db_insert_trade_event(mock_conn, wallet_id=1, event_type="closed", new_pos=pos)

    call_args = mock_conn.execute.call_args[0]
    assert call_args[4] == "closed"
    assert call_args[8] == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_db_upsert_position(mock_positions):
    """upsert_position kalder conn.execute med INSERT...ON CONFLICT."""
    from monitor import _db_upsert_position
    mock_conn = AsyncMock()
    pos = mock_positions[0]

    await _db_upsert_position(mock_conn, wallet_id=1, pos=pos)

    mock_conn.execute.assert_called_once()
    sql = mock_conn.execute.call_args[0][0]
    assert "ON CONFLICT" in sql
    assert "DO UPDATE" in sql


# ── test_decimal_conversion ────────────────────────────────────────────────────

def test_decimal_handles_none():
    from monitor import _decimal
    assert _decimal(None) is None


def test_decimal_handles_empty_string():
    from monitor import _decimal
    assert _decimal("") is None


def test_decimal_converts_string():
    from monitor import _decimal
    assert _decimal("0.65") == pytest.approx(0.65)


def test_decimal_converts_float():
    from monitor import _decimal
    assert _decimal(0.65) == pytest.approx(0.65)
```

Kør tests:
```bash
pytest tests/test_monitor.py -v
```

Alle tests skal være grønne inden du fortsætter.

Commit: `test(monitor): add unit tests for diff, retry, db writes (fase-2)`

---

## Trin 9 — Verifikation og afslutning

```bash
# 1. Linting
ruff check . --fix
black .

# 2. Type checking
mypy monitor.py db.py --ignore-missing-imports

# 3. Tests
pytest tests/ -x -q

# Forventet output:
# tests/test_monitor.py ............ passed
# No issues found (mypy)
# All checks passed (ruff)
```

Hvis alle checks er grønne:

Commit: `test(monitor): verify fase-2 — all checks passing`

---

## Trin 10 — Dokumentation

Opdater `CLAUDE.md`: marker Fase 2 som ✅

Opret `faser/fase-2/RESULT.md` med:
- Liste over alle ændrede/oprettede filer
- Hvilke ECC-bugs der er fixet (#9, #10, #11, #12, #13, #14, #15, #16)
- Test-resultater
- Eventuelle afvigelser fra denne prompt

Commit: `docs(fase-2): add RESULT.md and mark fase 2 complete in CLAUDE.md`

---

## Slutstatus

Når alle trin er gennemført skal `monitor.py` have følgende nye egenskaber:

| Feature | Fix # | Status |
|---------|-------|--------|
| DB-writes (opened/closed/resized) | #9 | ✅ |
| Dynamisk WS re-subscribe | #10 | ✅ |
| Retry med backoff på fetch | #11 | ✅ |
| Async HTTP (non-blocking) | #12 | ✅ |
| Wallet-label i log | #13 | ✅ |
| /health endpoint port 8080 | #14 | ✅ |
| Env var konfiguration | #15 | ✅ |
| Python logging (ingen print) | #16 | ✅ |

**Fase 2 er komplet når alle 10 trin er gennemført, alle tests er grønne, og alle commits er lavet.**  
Brugeren pusher `fase-2-monitor` til GitHub og merger til `main` via GitHub Desktop / pull request.
