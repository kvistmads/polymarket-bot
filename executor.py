"""
executor.py — Polymarket Copy-Trading Bot: Trade Executor.

Kører som separat process ved siden af monitor.py.
Kommunikerer KUN via databasen (pg_notify + copy_orders + daily_stats).

Lytter på pg_notify('new_trade') og kopierer åbnede trades til Polymarket CLOB.
DRY_RUN=true → alle ordrer logges som 'paper', ingen rigtige CLOB-ordrer sendes.

Start:  python executor.py
Health: GET http://localhost:8081/health

Moduler:
  executor_types.py    — TradeEvent, OrderResult dataclasses
  executor_gates.py    — 7-gate verifikation + calculate_size
  executor_clob.py     — CLOB API (balance, orderbook, submit)
  executor_telegram.py — Telegram alerts + go-live polling
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg
import httpx
from aiohttp import web
from dotenv import load_dotenv

from db import acquire, close_pool, get_pool
from executor_clob import submit_to_clob
from executor_gates import calculate_size, passes_gates
from executor_telegram import (
    check_go_live_gate,
    inject_dry_run_state,
    send_daily_summary,
    send_telegram,
    telegram_polling_loop,
)
from executor_types import OrderResult, TradeEvent

load_dotenv()

# ── Env vars (alle hentes ved startup — POLYMARKET_PRIVATE_KEY logges ALDRIG) ─
DB_DSN: str = os.getenv("DB_URL", "postgresql://localhost/polymarket").replace(
    "postgresql+asyncpg://", "postgresql://"
)
POLYMARKET_PRIVATE_KEY: str = os.environ.get("POLYMARKET_PRIVATE_KEY", "")  # ALDRIG log
MAX_DAILY_LOSS: Decimal = Decimal(os.getenv("MAX_DAILY_LOSS", "50"))
POSITION_SIZE_PCT: str = os.getenv("POSITION_SIZE_PCT", "0.05")
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
GAMMA_BASE: str = "https://gamma-api.polymarket.com"
CLOB_BASE:  str = "https://clob.polymarket.com"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("executor")

# Delt mutable state — telegram_polling_loop kan sætte active=False
_dry_run_state: dict[str, bool] = {"active": DRY_RUN}
inject_dry_run_state(_dry_run_state)

_last_processed: float = time.time()


# ── pg_notify listener ──────────────────────────────────────────────────────────


def on_notify(conn: asyncpg.Connection, pid: int, channel: str, payload: str) -> None:
    """Callback fra asyncpg ved pg_notify('new_trade', <event_id>)."""
    try:
        event_id = int(payload)
    except ValueError:
        log.warning("Ugyldigt notify-payload: %r", payload)
        return
    asyncio.create_task(_handle_notify(event_id))


async def _handle_notify(event_id: int) -> None:
    try:
        event = await _fetch_trade_event(event_id)
        if event is None:
            log.warning("trade_event id=%d ikke fundet", event_id)
            return
        await process_trade_event(event)
    except Exception:
        log.exception("Fejl ved håndtering af notify event_id=%d", event_id)


async def _fetch_trade_event(event_id: int) -> TradeEvent | None:
    """Hent fuld TradeEvent inkl. wallet-info fra DB."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT te.id, te.wallet_id, w.address AS wallet_address,
                   w.label AS wallet_label, te.condition_id, te.outcome,
                   te.event_type, te.new_size, te.price_at_event
            FROM trade_events te
            JOIN wallets w ON w.id = te.wallet_id
            WHERE te.id = $1
            """,
            event_id,
        )
    if not row:
        return None
    return TradeEvent(
        id=row["id"],
        wallet_id=row["wallet_id"],
        wallet_address=row["wallet_address"],
        wallet_label=row["wallet_label"],
        condition_id=row["condition_id"],
        outcome=row["outcome"],
        event_type=row["event_type"],
        new_size=Decimal(str(row["new_size"])),
        price_at_event=(
            Decimal(str(row["price_at_event"])) if row["price_at_event"] else None
        ),
    )


async def listen_loop() -> None:
    """Dedikeret LISTEN-forbindelse — LISTEN kræver dedicated connection (ikke pool)."""
    conn = await asyncpg.connect(dsn=DB_DSN)
    await conn.add_listener("new_trade", on_notify)
    log.info("Lytter på pg_notify('new_trade')…")
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await conn.remove_listener("new_trade", on_notify)
        await conn.close()
        log.info("LISTEN-forbindelse lukket")


# ── Trade processing ────────────────────────────────────────────────────────────


async def _get_market_title(conn: asyncpg.Connection, condition_id: str) -> str:
    """Hent market titel fra market_metadata — returnér kort condition_id ved fejl."""
    row = await conn.fetchrow(
        "SELECT title FROM market_metadata WHERE condition_id = $1", condition_id
    )
    if row and row["title"]:
        return str(row["title"])[:55]
    return f"{condition_id[:10]}…"


_COIN_ABBR: dict[str, str] = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "xrp": "XRP",
    "dogecoin": "DOGE",
    "bnb": "BNB",
    "avalanche": "AVAX",
    "cardano": "ADA",
    "polygon": "MATIC",
    "chainlink": "LINK",
    "litecoin": "LTC",
    "shiba": "SHIB",
    "pepe": "PEPE",
    "toncoin": "TON",
    "sui": "SUI",
    "near": "NEAR",
}


def _split_title(title: str) -> tuple[str, str]:
    """
    Opdel markedstitel i navn + tidsvindue.
    "Bitcoin Up or Down - May 4, 10:00AM-10:15AM ET"
      → ("Bitcoin Up or Down", "May 4, 10:00AM-10:15AM ET")
    Returnerer (title, "") hvis formatet ikke genkendes.
    """
    parts = title.rsplit(" - ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return title, ""


def _coin_name(market_name: str) -> str:
    """Konvertér "Bitcoin Up or Down" → "BTC Up or Down" via præfiks-match."""
    lower = market_name.lower()
    for coin, abbr in _COIN_ABBR.items():
        if lower.startswith(coin):
            rest = market_name[len(coin):]   # " Up or Down"
            return f"{abbr}{rest}"
    return market_name


def _format_trade_msg(
    label: str,
    wallet: str,
    outcome: str,
    price: Decimal | None,
    usdc_budget: Decimal | None,
    title: str,
) -> str:
    """Formatér Telegram-besked ved trade execution (paper + live).

    usdc_budget er USDC til investering (ikke shares).
    Shares = usdc_budget / price.
    """
    direction = outcome.upper()
    arrow = "📈" if outcome.lower() in ("up", "yes") else "📉"
    p = float(price or 0)
    budget = float(usdc_budget or 0)
    shares = (budget / p) if p > 0 else 0      # antal shares der købes
    invested = shares * p                       # = budget (afrundingsfejl minimeres)
    max_win = shares * 1.0                      # $1/share ved win
    profit = max_win - invested
    roi = (profit / invested * 100) if invested > 0 else 0
    impl_prob = p * 100

    market_name, time_window = _split_title(title)
    coin_name = _coin_name(market_name)
    time_part = f" · {time_window}" if time_window else ""

    return (
        f"{label} · <b>{wallet}</b>\n"
        f"{arrow} <b>{direction}</b> — {coin_name}{time_part}\n"
        f"💵 ${invested:.2f} USDC ({shares:.0f} sha. @ ${p:.3f})\n"
        f"🏆 Max: ${max_win:.2f} USDC (+{roi:.0f}%) · {impl_prob:.0f}% sandsynlighed"
    )


async def process_trade_event(event: TradeEvent) -> None:
    """Kør gates → siz ordre → eksekvér (paper eller live)."""
    global _last_processed
    _last_processed = time.time()

    tag = event.wallet_label or event.wallet_address[:8]
    log.info("[%s] event id=%d condition=%s", tag, event.id, event.condition_id[:12])

    async with acquire() as conn:
        ok, reason = await passes_gates(conn, event)
        if not ok:
            log.info("[%s] Gate afviste event %d: %s", tag, event.id, reason)
            return

        size = await calculate_size(conn, event.wallet_id)
        title = await _get_market_title(conn, event.condition_id)

        if _dry_run_state["active"]:
            result = OrderResult(
                status="paper",
                size_filled=size,
                price=event.price_at_event,
                error_msg=None,
            )
            await log_copy_order(conn, event, size, result)
            await send_telegram(_format_trade_msg("📄 PAPER", tag, event.outcome, event.price_at_event, size, title))
            await check_go_live_gate(conn)
            return

    # Live mode — CLOB-kald uden for connection context for at undgå timeout
    result = await submit_to_clob(event, size)
    async with acquire() as conn:
        title = await _get_market_title(conn, event.condition_id)
        await log_copy_order(conn, event, size, result)

    if result.status == "filled":
        await send_telegram(_format_trade_msg("✅ LIVE", tag, event.outcome, result.price, result.size_filled, title))
    else:
        await send_telegram(f"❌ <b>LIVE FEJL</b> — {tag}\n{result.error_msg}")


# ── DB logging ──────────────────────────────────────────────────────────────────


async def log_copy_order(
    conn: asyncpg.Connection,
    event: TradeEvent,
    size_requested: Decimal,
    result: OrderResult,
) -> None:
    """Indsæt i copy_orders og opdater daily_stats atomisk (ON CONFLICT DO UPDATE)."""
    await conn.execute(
        """
        INSERT INTO copy_orders
            (source_wallet_id, trade_event_id, condition_id, outcome, side,
             size_requested, size_filled, price, status, error_msg)
        VALUES ($1, $2, $3, $4, 'buy', $5, $6, $7, $8, $9)
        """,
        event.wallet_id,
        event.id,
        event.condition_id,
        event.outcome,
        size_requested,
        result.size_filled,
        result.price,
        result.status,
        result.error_msg,
    )
    await conn.execute(
        """
        INSERT INTO daily_stats (date, total_spent, orders_count, paper_orders_count)
        VALUES (CURRENT_DATE, $1, 1, $2)
        ON CONFLICT (date) DO UPDATE SET
            total_spent        = daily_stats.total_spent + $1,
            orders_count       = daily_stats.orders_count + 1,
            paper_orders_count = daily_stats.paper_orders_count + $2,
            last_updated_at    = now()
        """,
        size_requested,
        1 if result.status == "paper" else 0,
    )


# ── Health endpoint ─────────────────────────────────────────────────────────────


async def health_handler(request: web.Request) -> web.Response:
    """Returnerer altid 'ok' — executor er event-drevet, ikke polling-baseret."""
    return web.Response(text="ok")


async def _start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8081).start()
    log.info("Health endpoint started on :8081/health")
    return runner


# ── Win-rate tracker ───────────────────────────────────────────────────────────


async def win_rate_tracker_loop() -> None:
    """Baggrunds-loop: tjek Gamma API for resolved markeder hvert 10. minut."""
    await asyncio.sleep(60)  # giv DB/pool tid til at starte
    while True:
        try:
            await _update_resolved_orders()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("win_rate_tracker_loop fejlede")
        try:
            await asyncio.sleep(600)
        except asyncio.CancelledError:
            return


async def _update_resolved_orders() -> None:
    """
    Find copy_orders uden won-status og opdater via CLOB API.

    CLOB API bruges fordi Gamma API ikke kender condition_ids for
    de korte Up/Down markeder. CLOB tokens[] har winner: true/false.

    P&L-formel (size_filled = USDC budget, IKKE shares):
      Vundet:  pnl = size_filled * (1/price - 1)
      Tabt:    pnl = -size_filled
    """
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT condition_id
            FROM copy_orders
            WHERE won IS NULL AND status IN ('paper', 'filled')
            LIMIT 50
            """
        )
    if not rows:
        return

    log.debug("Tjekker resolution for %d markeder via CLOB API…", len(rows))
    for row in rows:
        condition_id: str = row["condition_id"]
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{CLOB_BASE}/markets/{condition_id}",
                )
                if r.status_code in (404, 400):
                    continue
                r.raise_for_status()
                market = r.json()

            # Valider at vi fik det rigtige marked
            if market.get("condition_id") != condition_id:
                continue

            # Find vinder fra tokens[] — winner: true sættes når markedet resolver
            winning_outcome: str | None = None
            for token in (market.get("tokens") or []):
                if token.get("winner") is True:
                    winning_outcome = str(token.get("outcome", "")).lower()
                    break

            if not winning_outcome:
                continue  # Markedet er endnu ikke resolved

            async with acquire() as conn:
                rows_updated = await conn.fetch(
                    """
                    WITH upd AS (
                        UPDATE copy_orders
                        SET
                            won      = (LOWER(outcome) = $2),
                            pnl_usdc = CASE
                                WHEN LOWER(outcome) = $2
                                    THEN size_filled * (1.0 / NULLIF(price, 0) - 1.0)
                                ELSE -size_filled
                                END
                        WHERE condition_id = $1
                          AND won IS NULL
                          AND status IN ('paper', 'filled')
                        RETURNING won, pnl_usdc, size_filled, price
                    )
                    SELECT * FROM upd
                    """,
                    condition_id,
                    winning_outcome,
                )
                title = await _get_market_title(conn, condition_id)

            if rows_updated:
                total_pnl = sum(float(r["pnl_usdc"] or 0) for r in rows_updated)
                total_invested = sum(
                    float(r["size_filled"] or 0)
                    for r in rows_updated
                )
                did_win = rows_updated[0]["won"]
                roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0
                emoji = "✅" if did_win else "❌"
                result = "VANDT" if did_win else "TABTE"
                log.info(
                    "Resolved %s → vinder: %s  (%d orders, P&L: %+.2f)",
                    condition_id[:12],
                    winning_outcome,
                    len(rows_updated),
                    total_pnl,
                )
                await send_telegram(
                    f"{emoji} <b>{result}:</b> {title}\n"
                    f"Udfald: {winning_outcome.upper()}\n"
                    f"Sim. P&amp;L: ${total_pnl:+.2f} USDC  (ROI {roi:+.1f}%)"
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Kunne ikke resolve condition_id=%s", condition_id[:12])


# ── Daily summary ───────────────────────────────────────────────────────────────


async def daily_summary_loop() -> None:
    """Send Telegram-dagsoversigt kl. 06:00 UTC hver dag."""
    while True:
        now = datetime.now(timezone.utc)
        next_run = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait_secs = (next_run - now).total_seconds()
        log.info("Næste dagsoversigt om %.1f timer", wait_secs / 3600)
        try:
            await asyncio.sleep(wait_secs)
        except asyncio.CancelledError:
            return
        try:
            await _build_and_send_daily_summary()
        except Exception:
            log.exception("Dagsoversigt fejlede")


async def _build_and_send_daily_summary() -> None:
    """Hent stats fra DB og send via Telegram."""
    async with acquire() as conn:
        totals = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                      AS total,
                COUNT(*) FILTER (WHERE won = true)            AS won_count,
                COUNT(*) FILTER (WHERE won = false)           AS lost_count,
                COUNT(*) FILTER (WHERE won IS NULL)           AS pending_count,
                COALESCE(SUM(pnl_usdc), 0)                   AS total_pnl,
                COALESCE(SUM(size_filled * price), 0)         AS total_invested
            FROM copy_orders
            WHERE status IN ('paper', 'filled')
            """
        )
        today = await conn.fetchrow(
            """
            SELECT COUNT(*) AS today_count
            FROM copy_orders
            WHERE status IN ('paper', 'filled')
              AND timestamp >= CURRENT_DATE
            """
        )
        by_outcome = await conn.fetch(
            """
            SELECT
                UPPER(outcome)                                AS outcome,
                COUNT(*)                                      AS total,
                COUNT(*) FILTER (WHERE won = true)            AS won_count
            FROM copy_orders
            WHERE won IS NOT NULL
            GROUP BY UPPER(outcome)
            ORDER BY total DESC
            LIMIT 6
            """
        )
        top_market = await conn.fetchrow(
            """
            SELECT mm.title, COUNT(co.*) AS cnt
            FROM copy_orders co
            JOIN market_metadata mm ON mm.condition_id = co.condition_id
            WHERE co.timestamp >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY mm.title
            ORDER BY cnt DESC
            LIMIT 1
            """
        )

    await send_daily_summary(
        totals=dict(totals),
        today_count=int(today["today_count"]) if today else 0,
        by_outcome=[dict(r) for r in by_outcome],
        top_market=str(top_market["title"]) if top_market and top_market["title"] else None,
    )


# ── Main ────────────────────────────────────────────────────────────────────────


async def main() -> None:
    """Start alle tasks og håndter SIGTERM gracefully."""
    log.info("=" * 60)
    log.info("POLYMARKET EXECUTOR — DRY_RUN=%s", _dry_run_state["active"])
    log.info("=" * 60)

    await get_pool()
    health_runner = await _start_health_server()

    loop = asyncio.get_event_loop()
    listen_task = loop.create_task(listen_loop())
    polling_task = loop.create_task(telegram_polling_loop())
    win_tracker_task = loop.create_task(win_rate_tracker_loop())
    daily_summary_task = loop.create_task(daily_summary_loop())

    stop_event = asyncio.Event()

    def _sigterm_handler(*_) -> None:
        log.info("SIGTERM modtaget — lukker ned…")
        stop_event.set()

    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)

    await stop_event.wait()

    listen_task.cancel()
    polling_task.cancel()
    win_tracker_task.cancel()
    daily_summary_task.cancel()
    await asyncio.gather(
        listen_task, polling_task, win_tracker_task, daily_summary_task,
        return_exceptions=True,
    )
    await health_runner.cleanup()
    await close_pool()
    log.info("Executor lukket ned.")


if __name__ == "__main__":
    asyncio.run(main())
