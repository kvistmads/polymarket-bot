#!/usr/bin/env python3
"""
Polymarket Wallet Watcher — Activity API Edition
=================================================
Opdager nye trades ved at poll'e activity API hvert 7. sekund.
Deduplicerer via transactionHash — ingen trades misses uanset levetid.

- Startup:   seeder seen-sæt med seneste 50 transaktioner
- Poll loop: henter seneste 20 aktiviteter hvert 7. sekund
- DB write:  indsætter trade_event + market_metadata ved nye BUY-trades
- Notify:    PostgreSQL NOTIFY udløser executor automatisk

Usage:
    python monitor.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg
import requests
from aiohttp import web
from dotenv import load_dotenv

from db import acquire

load_dotenv()

DB_URL: str | None = os.getenv("DB_URL")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# ── wallet config ──────────────────────────────────────────────────────────────
_followed_wallets_env = os.getenv("FOLLOWED_WALLETS", "")
DEFAULT_WALLETS = (
    [w.strip() for w in _followed_wallets_env.split(",") if w.strip()]
    if _followed_wallets_env
    else ["0x0b7a6030507efe5db145fbb57a25ba0c5f9d86cf"]
)

ACTIVITY_POLL_INTERVAL = 7   # sekunder mellem activity API-kald
ACTIVITY_SEED_LIMIT    = 50  # antal trades der seedes ved startup
ACTIVITY_FETCH_LIMIT   = 20  # antal trades der hentes per poll

# ── logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")

# ── endpoints ──────────────────────────────────────────────────────────────────
DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

HEADERS = {
    "User-Agent": "WalletWatcher/2.0",
    "Origin":     "https://polymarket.com",
    "Referer":    "https://polymarket.com/",
}


# ── REST helpers ───────────────────────────────────────────────────────────────

def _fetch_activity(wallet: str, limit: int = 20) -> list[dict]:
    """Hent de seneste `limit` aktiviteter for wallet fra Data API."""
    try:
        r = requests.get(
            f"{DATA_API}/activity",
            params={"user": wallet, "limit": str(limit), "offset": "0"},
            headers=HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception as exc:
        log.warning("activity API fejl: %s", exc)
        return []


async def fetch_activity_async(wallet: str, limit: int = 20) -> list[dict]:
    """Async wrapper — kører HTTP-kald i executor pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_activity, wallet, limit)


def _fetch_gamma_market(condition_id: str) -> dict | None:
    """Hent market-metadata fra Gamma API for condition_id."""
    try:
        r = requests.get(
            f"{GAMMA_API}/markets/{condition_id}",
            headers=HEADERS,
            timeout=10,
        )
        if r.ok:
            return r.json()
    except Exception as exc:
        log.warning("Gamma API fejl for %s: %s", condition_id[:12], exc)
    return None


async def fetch_gamma_market_async(condition_id: str) -> dict | None:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_gamma_market, condition_id)


# ── DB helpers ─────────────────────────────────────────────────────────────────

async def _get_or_create_wallet_id(conn: asyncpg.Connection, address: str) -> int:
    row = await conn.fetchrow("SELECT id FROM wallets WHERE address = $1", address)
    if row:
        return int(row["id"])
    new_id = await conn.fetchval(
        "INSERT INTO wallets (address) VALUES ($1) RETURNING id", address
    )
    log.info("Oprettet wallet: %s → id=%s", address[:10], new_id)
    return int(new_id)


async def _upsert_market_metadata(
    conn: asyncpg.Connection,
    condition_id: str,
    market: dict,
) -> None:
    """Gem market-metadata fra Gamma API — ON CONFLICT DO NOTHING."""
    raw_outcomes  = market.get("outcomes", [])
    raw_token_ids = market.get("clobTokenIds", "")

    outcomes_json = (
        raw_outcomes if isinstance(raw_outcomes, str)
        else json.dumps(raw_outcomes)
    )
    token_ids_json = (
        raw_token_ids if isinstance(raw_token_ids, str)
        else json.dumps(raw_token_ids)
    )

    await conn.execute(
        """
        INSERT INTO market_metadata (condition_id, title, slug, outcomes, clob_token_ids)
        VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
        ON CONFLICT (condition_id) DO UPDATE SET
            title         = EXCLUDED.title,
            slug          = EXCLUDED.slug,
            outcomes      = EXCLUDED.outcomes,
            clob_token_ids = EXCLUDED.clob_token_ids
        WHERE EXCLUDED.title IS NOT NULL AND EXCLUDED.title != ''
        """,
        condition_id,
        market.get("question") or market.get("title", ""),
        market.get("slug", ""),
        outcomes_json,
        token_ids_json,
    )


async def _insert_trade_event(
    conn: asyncpg.Connection,
    wallet_id: int,
    condition_id: str,
    outcome: str,
    size: float,
    price: float,
) -> None:
    """Indsæt immutable trade_event med event_type='opened'."""
    await conn.execute(
        """
        INSERT INTO trade_events (
            wallet_id, condition_id, outcome, event_type,
            old_size, new_size, price_at_event, pnl_at_close
        ) VALUES ($1, $2, $3, 'opened', NULL, $4, $5, NULL)
        """,
        wallet_id,
        condition_id,
        outcome,
        Decimal(str(size)),
        Decimal(str(price)),
    )


# ── activity processing ────────────────────────────────────────────────────────

def _extract_condition_id(trade: dict) -> str | None:
    """Udtræk condition_id fra activity-trade — prøv flere felt-navne."""
    for key in ("conditionId", "condition_id", "market", "marketId"):
        val = trade.get(key)
        if val and isinstance(val, str) and len(val) > 10:
            return val
    return None


async def process_new_trade(
    wallet: str,
    wallet_id: int,
    trade: dict,
) -> bool:
    """
    Behandl ét nyt BUY-trade fra activity API.

    Returns True hvis trade_event blev skrevet til DB.
    """
    side = (trade.get("side") or "").upper()
    if side != "BUY":
        return False  # Ignorer SELL/REDEEM

    condition_id = _extract_condition_id(trade)
    if not condition_id:
        log.debug("Ingen condition_id i trade: %s", trade.get("transactionHash", "?")[:12])
        return False

    outcome  = trade.get("outcome", "")
    size     = float(trade.get("size",     0) or 0)
    price    = float(trade.get("price",    0) or 0)
    usdc     = float(trade.get("usdcSize", 0) or 0)
    title    = (trade.get("title") or "")[:120]
    ts       = trade.get("timestamp", 0)
    dt       = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M UTC") if ts else "?"

    log.info(
        "[%s…%s] >>> NY POSITION: %s (%s) @ $%.3f  $%.2f USDC  %s",
        wallet[:6], wallet[-4:], title, outcome, price, usdc, dt,
    )

    # Hent market-metadata fra Gamma API (nødvendig for Gate 4)
    market = await fetch_gamma_market_async(condition_id)

    try:
        async with acquire() as conn:
            if market:
                await _upsert_market_metadata(conn, condition_id, market)
            elif title:
                # Gamma API utilgængelig — gem title fra activity API som fallback
                await conn.execute(
                    """
                    INSERT INTO market_metadata (condition_id, title, slug, outcomes, clob_token_ids)
                    VALUES ($1, $2, '', '[]'::jsonb, '[]'::jsonb)
                    ON CONFLICT (condition_id) DO UPDATE SET
                        title = EXCLUDED.title
                    WHERE EXCLUDED.title IS NOT NULL AND EXCLUDED.title != ''
                    """,
                    condition_id,
                    title,
                )
            await _insert_trade_event(conn, wallet_id, condition_id, outcome, size, price)
        log.info("  ✅ trade_event skrevet — executor behandler nu")
        return True
    except Exception:
        log.exception("DB-fejl ved behandling af trade %s", condition_id[:12])
        return False


# ── health server ──────────────────────────────────────────────────────────────
_last_successful_poll: float = 0.0


async def _start_health_server() -> web.AppRunner:
    """Start /health endpoint på port 8080."""

    async def health_handler(request: web.Request) -> web.Response:
        if _last_successful_poll == 0.0:
            return web.Response(status=503, text="not_started")
        age = time.time() - _last_successful_poll
        if age > ACTIVITY_POLL_INTERVAL * 10:  # 70 sekunder
            return web.Response(status=503, text=f"stale:{age:.0f}s")
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    log.info("Health endpoint: :8080/health")
    return runner


# ── main ───────────────────────────────────────────────────────────────────────

async def main(wallets: list[str]) -> int:
    global _last_successful_poll

    log.info("=" * 60)
    log.info("POLYMARKET MONITOR — activity API edition")
    for w in wallets:
        log.info("Wallet:  %s", w)
    log.info("Interval: %ds  (seed: %d trades, poll: %d trades)",
             ACTIVITY_POLL_INTERVAL, ACTIVITY_SEED_LIMIT, ACTIVITY_FETCH_LIMIT)
    log.info("=" * 60)

    health_runner = await _start_health_server()

    # ── per-wallet state ──
    wallet_seen:     dict[str, set[str]] = {}  # wallet -> set af transactionHash
    wallet_ids:      dict[str, int]      = {}  # wallet -> DB wallet_id

    # ── seed wallet_ids fra DB ──
    if DB_URL:
        for wallet in wallets:
            try:
                async with acquire() as conn:
                    wallet_ids[wallet] = await _get_or_create_wallet_id(conn, wallet)
            except Exception:
                log.exception("Kunne ikke hente wallet_id for %s", wallet[:10])

    # ── seed seen-sæt (undgå at notificere om gamle trades ved start) ──
    for wallet in wallets:
        log.info("[%s…%s] Seeder seen-sæt med seneste %d trades...",
                 wallet[:6], wallet[-4:], ACTIVITY_SEED_LIMIT)
        initial = await fetch_activity_async(wallet, limit=ACTIVITY_SEED_LIMIT)
        seen: set[str] = set()
        for t in initial:
            tx = t.get("transactionHash", "")
            if tx:
                seen.add(tx)
        wallet_seen[wallet] = seen
        log.info("[%s…%s] %d eksisterende transaktioner loaded — klar til live polling",
                 wallet[:6], wallet[-4:], len(seen))

    _last_successful_poll = time.time()

    # ── poll loop ──
    log.info("Starter activity polling hvert %ds...", ACTIVITY_POLL_INTERVAL)
    try:
        while True:
            await asyncio.sleep(ACTIVITY_POLL_INTERVAL)

            for wallet in wallets:
                try:
                    trades = await fetch_activity_async(wallet, limit=ACTIVITY_FETCH_LIMIT)
                    if not trades:
                        continue

                    seen = wallet_seen.setdefault(wallet, set())
                    wallet_id = wallet_ids.get(wallet)

                    # Find nye trades (reversed = ældste først)
                    new_trades = [
                        t for t in trades
                        if t.get("transactionHash", "") not in seen
                    ]

                    for trade in reversed(new_trades):
                        tx = trade.get("transactionHash", "")
                        if tx:
                            seen.add(tx)

                        if wallet_id and DB_URL:
                            await process_new_trade(wallet, wallet_id, trade)
                        else:
                            # Ingen DB — log kun
                            side  = trade.get("side", "?")
                            title = (trade.get("title") or "")[:50]
                            outcome = trade.get("outcome", "?")
                            log.info("[%s…%s] %s %s — %s",
                                     wallet[:6], wallet[-4:], side, outcome, title)

                    _last_successful_poll = time.time()

                except Exception:
                    log.exception("[%s…%s] poll fejl", wallet[:6], wallet[-4:])

    except KeyboardInterrupt:
        log.info("Stopper...")
    finally:
        await health_runner.cleanup()

    return 0


if __name__ == "__main__":
    wallet_list = DEFAULT_WALLETS
    if not wallet_list:
        sys.exit("Ingen wallets konfigureret — sæt FOLLOWED_WALLETS i .env")

    try:
        asyncio.run(main(wallet_list))
    except KeyboardInterrupt:
        pass
