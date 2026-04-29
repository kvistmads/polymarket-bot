# Fase 3 — Trade Executor

## Kontekst

Du arbejder i `polymarket-bot` mappen på branch `fase-3-executor` (opret den fra main).

Fase 1 (database) og Fase 2 (monitor) er færdige og mergede til main. Nu skal du bygge `executor.py` — den komponent der lytter på `pg_notify('new_trade')` og kopierer trades til Polymarket CLOB API.

**Læs disse filer FØR du skriver én linje kode:**
```
Read CLAUDE.md
Read PRD.md
Read .ecc/verification-loop/SKILL.md
Read .ecc/backend-patterns/SKILL.md
Read .ecc/security-review/SKILL.md
Read .ecc/tdd-workflow/SKILL.md
Read .ecc/rules/python/coding-style.md
Read .ecc/rules/common/security.md
Read db.py
Read monitor.py  (reference — se mønstrene herfra)
```

---

## Mål

Opret `executor.py` (max 300 linjer — split i moduler hvis nødvendigt) og `tests/test_executor.py`.

**Executor kører som separat process** ved siden af monitor.py. De kommunikerer KUN via databasen.

---

## Trin 1 — Branch

```bash
git checkout main
git checkout -b fase-3-executor
```

---

## Trin 2 — Dataklasser og interface

Opret følgende dataclasses øverst i `executor.py`:

```python
from dataclasses import dataclass
from decimal import Decimal

@dataclass
class TradeEvent:
    id: int
    wallet_id: int
    wallet_address: str
    wallet_label: str | None
    condition_id: str
    outcome: str
    event_type: str          # 'opened' | 'closed' | 'resized'
    new_size: Decimal
    price_at_event: Decimal | None

@dataclass
class OrderResult:
    status: str              # 'filled' | 'failed' | 'paper' | 'cancelled'
    size_filled: Decimal | None
    price: Decimal | None
    error_msg: str | None
```

---

## Trin 3 — pg_notify listener (hoved-loop)

Implementér `listen_loop()` der:

1. Opretter en dedikeret asyncpg connection (IKKE pool — LISTEN kræver dedicated connection)
2. Kalder `await conn.add_listener('new_trade', on_notify)` 
3. Kører `asyncio.sleep(1)` i en evig løkke som keepalive
4. Håndterer SIGTERM gracefully (luk connection, flush logs)

```python
async def listen_loop() -> None:
    conn = await asyncpg.connect(dsn=DB_DSN)
    await conn.add_listener("new_trade", on_notify)
    log.info("Listening for pg_notify('new_trade')...")
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await conn.remove_listener("new_trade", on_notify)
        await conn.close()
```

`on_notify` callback henter trade_event fra DB via payload (event id) og kalder `process_trade_event(event)`.

---

## Trin 4 — Gates (7 gates, alle skal passere)

Implementér `passes_gates(conn, event: TradeEvent) -> tuple[bool, str]`.
Returnér `(False, "årsag")` ved første fejl, `(True, "")` hvis alle passerer.

```
Gate 1: wallet aktuelt på followed_wallets (unfollowed_at IS NULL)?
         → SELECT 1 FROM followed_wallets WHERE wallet_id=$1 AND unfollowed_at IS NULL

Gate 2: Kun 'opened' events kopieres (ikke 'closed' eller 'resized')
         → if event.event_type != 'opened': return False, "not an open"

Gate 3: Ikke allerede eksponeret i dette marked?
         → SELECT 1 FROM positions WHERE wallet_id != $wallet AND condition_id=$1 AND status='open'
           (tjek OUR egne positioner — ikke kildewalletens)
           Bemærk: botten har ingen dedikeret "own wallet" tabel endnu — brug copy_orders til dette:
           SELECT 1 FROM copy_orders WHERE condition_id=$1 AND status IN ('submitted','filled','paper')

Gate 4: Markedet likvidt? bid-ask spread < 5%
         → Kald CLOB API GET /book?token_id=<token_id>
         → spread = (best_ask - best_bid) / best_ask
         → if spread >= 0.05: return False, f"spread {spread:.1%}"

Gate 5: Mere end 2 timer til markedets close?
         → Kald Gamma API GET /markets?condition_id=<condition_id>
         → if end_date_iso < now() + 2h: return False, "market closes soon"

Gate 6: Order-size inden for hard cap?
         → size = available_cash * position_size_pct
         → if size > available_cash * 0.20: size = available_cash * 0.20  (hard cap 20%)
         → if size < 1.0: return False, "size too small"

Gate 7: Daglig loss limit ikke nået?
         → SELECT realized_pnl FROM daily_stats WHERE date = CURRENT_DATE
         → if realized_pnl <= -MAX_DAILY_LOSS: return False, "daily loss limit"
```

---

## Trin 5 — Position sizing

```python
async def calculate_size(conn: asyncpg.Connection, wallet_id: int) -> Decimal:
    # Per-wallet override fra followed_wallets
    row = await conn.fetchrow(
        "SELECT position_size_pct FROM followed_wallets "
        "WHERE wallet_id=$1 AND unfollowed_at IS NULL",
        wallet_id,
    )
    pct = Decimal(str(row["position_size_pct"])) if row and row["position_size_pct"] else Decimal(POSITION_SIZE_PCT)
    available_cash = await get_clob_balance()
    size = available_cash * pct
    # Hard cap
    return min(size, available_cash * Decimal("0.20"))
```

`get_clob_balance()` kalder CLOB API `GET /balance` med L2 auth header (se Trin 7).

---

## Trin 6 — DRY_RUN mode og paper trading

```python
async def process_trade_event(event: TradeEvent) -> None:
    async with acquire() as conn:
        ok, reason = await passes_gates(conn, event)
        if not ok:
            log.info("Gate rejected %s: %s", event.id, reason)
            return

        size = await calculate_size(conn, event.wallet_id)
        tag = f"[{event.wallet_label or event.wallet_address[:8]}]"

        if DRY_RUN:
            result = OrderResult(status="paper", size_filled=size, price=event.price_at_event, error_msg=None)
            await log_copy_order(conn, event, size, result)
            await send_telegram(f"📄 PAPER {tag} {event.outcome} {event.condition_id[:8]}… size={size:.2f}")
            await check_go_live_gate(conn)
            return

        # Live mode
        result = await submit_to_clob(event, size)
        await log_copy_order(conn, event, size, result)
        if result.status == "filled":
            await send_telegram(f"✅ LIVE {tag} filled {result.size_filled:.2f} @ {result.price}")
        else:
            await send_telegram(f"❌ LIVE {tag} FAILED: {result.error_msg}")
```

---

## Trin 7 — CLOB API integration

Implementér disse tre funktioner. Brug `httpx` (async) til alle CLOB-kald:

```python
async def get_clob_balance() -> Decimal:
    """GET https://clob.polymarket.com/balance — returnerer tilgængeligt USDC."""

async def get_clob_orderbook(token_id: str) -> dict:
    """GET https://clob.polymarket.com/book?token_id={token_id}"""

async def submit_to_clob(event: TradeEvent, size: Decimal) -> OrderResult:
    """POST https://clob.polymarket.com/order — L2 signeret ordre."""
```

**Vigtigt for submit_to_clob:**
- Brug `py-clob-client` biblioteket (tilføj til requirements.txt: `py-clob-client>=0.17`)
- Private key hentes fra `POLYMARKET_PRIVATE_KEY` env var — ALDRIG hardcodet
- Key logges ALDRIG — heller ikke ved fejl
- Brug `ClobClient` fra `py_clob_client.client`:

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,  # Polygon
    private_key=POLYMARKET_PRIVATE_KEY,
)
```

Opret en `MarketOrder` (ikke limit order) for simplicitets skyld i Fase 3.

---

## Trin 8 — Telegram integration

Implementér `send_telegram(text: str)` og `send_approval_request()`.

Brug `httpx` direkte mod Telegram Bot API (ikke python-telegram-bot biblioteket — for tungt):

```python
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

async def send_telegram(text: str) -> None:
    async with httpx.AsyncClient() as client:
        await client.post(
            TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage"),
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        )
```

Implementér `check_go_live_gate(conn)` der kører efter hver paper trade:

```python
async def check_go_live_gate(conn: asyncpg.Connection) -> None:
    row = await conn.fetchrow(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status='paper' THEN 1 ELSE 0 END) AS paper_count,
               -- win = paper order where kilde-wallet lukkede med positivt PnL
               -- approksimation: tæl paper orders med pris < 0.5 (yes) der stadig er åbne
               COUNT(*) FILTER (WHERE size_filled IS NOT NULL) AS filled
        FROM copy_orders
        WHERE status = 'paper'
        """
    )
    total = row["total"] or 0
    if total < 20:
        return

    # Beregn win_rate via trade_events join — paper orders matchet mod lukkede positions
    win_row = await conn.fetchrow(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE te.pnl_at_close > 0) AS won
        FROM copy_orders co
        JOIN trade_events te ON te.condition_id = co.condition_id
            AND te.event_type = 'closed'
        WHERE co.status = 'paper'
        """
    )
    if not win_row or not win_row["total"]:
        return

    win_rate = win_row["won"] / win_row["total"]
    if win_rate > 0.52:
        await send_approval_request(win_rate, win_row["total"])

async def send_approval_request(win_rate: float, total: int) -> None:
    """Sender Telegram inline keyboard med go-live godkendelse."""
    async with httpx.AsyncClient() as client:
        await client.post(
            TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage"),
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": (
                    f"🚀 <b>Bot klar til live trading!</b>\n"
                    f"Win rate: {win_rate:.1%} over {total} paper trades\n\n"
                    f"Vil du aktivere live trading?"
                ),
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "✅ Klar — gå live", "callback_data": "go_live"},
                        {"text": "❌ Ikke klar", "callback_data": "stay_paper"},
                    ]]
                },
            },
        )
```

Implementér en simpel Telegram webhook/polling loop til at håndtere `callback_data`:
- `go_live` → sæt `DRY_RUN = False` (runtime, ingen restart)
- `stay_paper` → send bekræftelsesbesked

---

## Trin 9 — DB-logging

```python
async def log_copy_order(
    conn: asyncpg.Connection,
    event: TradeEvent,
    size_requested: Decimal,
    result: OrderResult,
) -> None:
    await conn.execute(
        """
        INSERT INTO copy_orders
            (source_wallet_id, trade_event_id, condition_id, outcome, side,
             size_requested, size_filled, price, status, error_msg)
        VALUES ($1, $2, $3, $4, 'buy', $5, $6, $7, $8, $9)
        """,
        event.wallet_id, event.id, event.condition_id, event.outcome,
        size_requested, result.size_filled, result.price,
        result.status, result.error_msg,
    )
```

Opdater også `daily_stats` atomisk ved hvert fill:

```python
await conn.execute(
    """
    INSERT INTO daily_stats (date, total_spent, orders_count, paper_orders_count)
    VALUES (CURRENT_DATE, $1, 1, $2)
    ON CONFLICT (date) DO UPDATE SET
        total_spent = daily_stats.total_spent + $1,
        orders_count = daily_stats.orders_count + 1,
        paper_orders_count = daily_stats.paper_orders_count + $2,
        last_updated_at = now()
    """,
    size_requested,
    1 if result.status == "paper" else 0,
)
```

---

## Trin 10 — Health endpoint (port 8081)

Eksponér `/health` på port 8081 via aiohttp (samme mønster som monitor.py):

```python
async def health_handler(request: web.Request) -> web.Response:
    age = time.time() - _last_processed
    if age > 300:  # 5 minutter uden aktivitet = OK (executor er event-drevet)
        return web.Response(text="ok")
    return web.Response(text="ok")
```

Executor er event-drevet så "stale" giver ikke mening — `/health` returnerer altid `ok` så længe processen kører.

---

## Trin 11 — Miljøvariable

Alle hentes via `os.getenv()` øverst i filen:

```python
DB_DSN: str = os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")
POLYMARKET_PRIVATE_KEY: str = os.environ["POLYMARKET_PRIVATE_KEY"]  # aldrig log denne
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID: str = os.environ["TELEGRAM_CHAT_ID"]
MAX_DAILY_LOSS: Decimal = Decimal(os.getenv("MAX_DAILY_LOSS", "50"))
POSITION_SIZE_PCT: str = os.getenv("POSITION_SIZE_PCT", "0.05")
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
```

---

## Trin 12 — requirements.txt opdatering

Tilføj til requirements.txt:
```
httpx>=0.27
py-clob-client>=0.17
```

---

## Trin 13 — Tests (tests/test_executor.py)

Skriv minimum 12 tests. Brug `unittest.mock` og `pytest-asyncio`. Ingen rigtig DB eller API-kald i unit tests.

Grupper:

**Gate tests (7 tests — én per gate):**
```python
@pytest.mark.asyncio
async def test_gate_1_wallet_not_followed():
    """Gate 1 afviser event hvis wallet ikke er på followed_wallets."""

async def test_gate_2_only_opened_events():
    """Gate 2 afviser 'closed' og 'resized' event_types."""

async def test_gate_3_already_exposed():
    """Gate 3 afviser hvis copy_orders allerede har aktiv ordre på condition_id."""

async def test_gate_4_spread_too_wide():
    """Gate 4 afviser hvis bid-ask spread >= 5%."""

async def test_gate_5_market_closes_soon():
    """Gate 5 afviser hvis markedet lukker inden for 2 timer."""

async def test_gate_6_size_hard_cap():
    """Gate 6 capper position til 20% af available_cash."""

async def test_gate_7_daily_loss_limit():
    """Gate 7 afviser hvis daily realized_pnl <= -MAX_DAILY_LOSS."""
```

**Paper trading tests (3 tests):**
```python
async def test_dry_run_logs_paper_order():
    """DRY_RUN=True → status='paper' i copy_orders, ingen CLOB-kald."""

async def test_go_live_gate_not_triggered_below_20():
    """check_go_live_gate sender ikke Telegram hvis < 20 paper trades."""

async def test_go_live_gate_triggers_at_threshold():
    """check_go_live_gate sender approval request ved win_rate > 52% over ≥20 trades."""
```

**Security tests (2 tests):**
```python
async def test_private_key_not_in_logs(caplog):
    """POLYMARKET_PRIVATE_KEY må aldrig optræde i log output."""

async def test_daily_stats_updated_atomically():
    """log_copy_order opdaterer daily_stats med korrekt ON CONFLICT DO UPDATE."""
```

---

## Trin 14 — Pre-commit checks

```bash
ruff check . --fix
black .
mypy executor.py db.py --ignore-missing-imports
pytest tests/test_executor.py -x -q
```

Alle 4 skal være grønne inden commit.

---

## Trin 15 — Commits

Commit efter hvert trin der tilføjer kørbar kode. Minimum commits:

```
feat(executor): add TradeEvent/OrderResult dataclasses and listen_loop
feat(executor): implement 7-gate verification (passes_gates)
feat(executor): add position sizing and DRY_RUN paper trading mode
feat(executor): add CLOB API integration (balance, orderbook, submit)
feat(executor): add Telegram alerts and go-live approval keyboard
feat(executor): add health endpoint on :8081 and daily_stats logging
feat(deps): add httpx and py-clob-client to requirements.txt
test(executor): add 12 unit tests covering gates, paper mode, security
```

---

## Trin 16 — RESULT.md

Opret `faser/fase-3/RESULT.md` med:
- Liste af oprettede filer og linjeantal
- Gate-oversigt (Gate 1-7 med SQL/logik)
- Test-oversigt (12+ tests, alle grønne)
- Verifikationstabel (ruff/black/mypy/pytest)
- Afvigelser fra denne prompt

Opdater også `CLAUDE.md` Fase-status: sæt `[x]` på Fase 3.

---

## Vigtige constraints

- `POLYMARKET_PRIVATE_KEY` må **aldrig** logges, printes eller gemmes i DB
- Brug `NUMERIC`/`Decimal` — aldrig `float` til penge
- Brug `TIMESTAMPTZ` — aldrig `TIMESTAMP`
- Executor skriver KUN til `copy_orders` og `daily_stats` — aldrig til `trade_events` eller `positions`
- Funktioner max ~50 linjer — split i hjælpefunktioner
- `executor.py` max 300 linjer — opret `executor_gates.py` og `executor_clob.py` hvis nødvendigt
- Brug `log.exception()` (ikke `log.error()`) ved exceptions så traceback logges
