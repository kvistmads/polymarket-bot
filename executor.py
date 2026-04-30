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
from decimal import Decimal

import asyncpg
from aiohttp import web
from dotenv import load_dotenv

from db import acquire, close_pool, get_pool
from executor_clob import submit_to_clob
from executor_gates import calculate_size, passes_gates
from executor_telegram import (
    check_go_live_gate,
    inject_dry_run_state,
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


async def process_trade_event(event: TradeEvent) -> None:
    """Kør gates → siz ordre → eksekvér (paper eller live)."""
    global _last_processed
    _last_processed = time.time()

    tag = f"[{event.wallet_label or event.wallet_address[:8]}]"
    log.info("%s event id=%d condition=%s", tag, event.id, event.condition_id[:12])

    async with acquire() as conn:
        ok, reason = await passes_gates(conn, event)
        if not ok:
            log.info("%s Gate afviste event %d: %s", tag, event.id, reason)
            return

        size = await calculate_size(conn, event.wallet_id)

        if _dry_run_state["active"]:
            result = OrderResult(
                status="paper",
                size_filled=size,
                price=event.price_at_event,
                error_msg=None,
            )
            await log_copy_order(conn, event, size, result)
            await send_telegram(
                f"📄 PAPER {tag} {event.outcome} "
                f"{event.condition_id[:8]}… size={size:.2f}"
            )
            await check_go_live_gate(conn)
            return

    # Live mode — CLOB-kald uden for connection context for at undgå timeout
    result = await submit_to_clob(event, size)
    async with acquire() as conn:
        await log_copy_order(conn, event, size, result)

    if result.status == "filled":
        await send_telegram(
            f"✅ LIVE {tag} filled {result.size_filled:.2f} @ {result.price}"
        )
    else:
        await send_telegram(f"❌ LIVE {tag} FAILED: {result.error_msg}")


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

    stop_event = asyncio.Event()

    def _sigterm_handler(*_) -> None:
        log.info("SIGTERM modtaget — lukker ned…")
        stop_event.set()

    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)

    await stop_event.wait()

    listen_task.cancel()
    polling_task.cancel()
    await asyncio.gather(listen_task, polling_task, return_exceptions=True)
    await health_runner.cleanup()
    await close_pool()
    log.info("Executor lukket ned.")


if __name__ == "__main__":
    asyncio.run(main())
