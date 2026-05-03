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
            price_str = f"${float(event.price_at_event):.3f}" if event.price_at_event else "?"
            usdc = float(size) * float(event.price_at_event or 0)
            await send_telegram(
                f"📄 <b>PAPER</b> — {tag}\n"
                f"{'📈' if event.outcome.lower() in ('up','yes') else '📉'} "
                f"<b>{event.outcome}</b> @ {price_str} | {float(size):.0f} shares | ${usdc:.2f} USDC\n"
                f"📊 {title}"
            )
            await check_go_live_gate(conn)
            return

    # Live mode — CLOB-kald uden for connection context for at undgå timeout
    result = await submit_to_clob(event, size)
    async with acquire() as conn:
        title = await _get_market_title(conn, event.condition_id)
        await log_copy_order(conn, event, size, result)

    if result.status == "filled":
        usdc = float(result.size_filled or 0) * float(result.price or 0)
        await send_telegram(
            f"✅ <b>LIVE FILLED</b> — {tag}\n"
            f"{'📈' if event.outcome.lower() in ('up','yes') else '📉'} "
            f"<b>{event.outcome}</b> @ ${float(result.price):.3f} | "
            f"{float(result.size_filled):.0f} shares | ${usdc:.2f} USDC\n"
            f"📊 {title}"
        )
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
    """Find copy_orders uden won-status og opdater fra Gamma API."""
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

    log.debug("Tjekker resolution for %d markeder…", len(rows))
    for row in rows:
        condition_id: str = row["condition_id"]
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{GAMMA_BASE}/markets",
                    params={"condition_id": condition_id},
                )
                r.raise_for_status()
                data = r.json()

            markets = data if isinstance(data, list) else [data]
            if not markets:
                continue
            market = markets[0]
            if not market.get("resolved"):
                continue

            # outcomes/outcomePrices kan være JSON-strenge eller lister
            import json as _json
            raw_outcomes = market.get("outcomes") or []
            raw_prices = market.get("outcomePrices") or []
            outcomes: list = _json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
            outcome_prices: list = _json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            winning_outcome: str | None = None
            for i, price_str in enumerate(outcome_prices):
                if float(price_str) >= 0.99 and i < len(outcomes):
                    winning_outcome = str(outcomes[i]).lower()
                    break

            if not winning_outcome:
                continue

            async with acquire() as conn:
                updated = await conn.fetchval(
                    """
                    WITH upd AS (
                        UPDATE copy_orders
                        SET
                            won      = (LOWER(outcome) = $2),
                            pnl_usdc = CASE
                                WHEN LOWER(outcome) = $2
                                    THEN size_filled * (1 - price)
                                ELSE -(size_filled * price)
                                END
                        WHERE condition_id = $1
                          AND won IS NULL
                          AND status IN ('paper', 'filled')
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM upd
                    """,
                    condition_id,
                    winning_outcome,
                )
            if updated:
                log.info(
                    "Resolved %s → vinder: %s  (%d orders opdateret)",
                    condition_id[:12],
                    winning_outcome,
                    updated,
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
