#!/usr/bin/env python3
"""
Polymarket Wallet Watcher — Multi-Wallet Edition
=================================================
Monitors multiple wallets' open positions on Polymarket in real-time.

- Startup:   fetches + displays a snapshot of all open positions per wallet
- WebSocket: ONE shared CLOB market channel covering all wallets' tokens
- Polling:   re-checks each wallet's positions every 30s to detect new / closed trades

Usage:
    python watcher_multi.py
    python watcher_multi.py --wallets 0xABC...,0xDEF...
    python watcher_multi.py --interval 15
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import asyncpg
import requests
from aiohttp import web
from dotenv import load_dotenv

from db import acquire

load_dotenv()

DB_URL: str | None = os.getenv("DB_URL")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

try:
    import websockets
except ImportError:
    sys.exit("Missing dependency: pip install websockets")

# ── defaults ──────────────────────────────────────────────────────────────────
_followed_wallets_env = os.getenv("FOLLOWED_WALLETS", "")
DEFAULT_WALLETS = (
    [w.strip() for w in _followed_wallets_env.split(",") if w.strip()]
    if _followed_wallets_env
    else ["0x0b7a6030507efe5db145fbb57a25ba0c5f9d86cf"]
)
POLL_INTERVAL = 30  # seconds

# ── logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")

# ── endpoints ─────────────────────────────────────────────────────────────────
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

HEADERS = {
    "User-Agent": "WalletWatcher/1.0",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
}


# ── database helpers ──────────────────────────────────────────────────────────
def _decimal(value: object) -> float | None:
    """Konvertér API-streng til float — returnér None ved None/tom streng."""
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def _get_or_create_wallet_id(conn: asyncpg.Connection, address: str) -> int:
    """Returnér wallet.id — indsæt wallets-rækken hvis den ikke findes."""
    row = await conn.fetchrow("SELECT id FROM wallets WHERE address = $1", address)
    if row:
        return int(row["id"])
    new_id = await conn.fetchval(
        "INSERT INTO wallets (address) VALUES ($1) RETURNING id", address
    )
    log.info("Inserted new wallet: %s → id=%s", address[:10], new_id)
    return int(new_id)


async def _db_upsert_position(
    conn: asyncpg.Connection, wallet_id: int, pos: dict
) -> None:
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


# ── REST helpers ──────────────────────────────────────────────────────────────
def fetch_positions(wallet: str) -> list[dict]:
    """Fetch all open positions for *wallet* from the Polymarket Data API."""
    all_pos: list[dict] = []
    offset = 0
    while True:
        r = requests.get(
            f"{DATA_API}/positions",
            params={
                "user": wallet,
                "sortBy": "CURRENT",
                "sortDirection": "DESC",
                "sizeThreshold": "0.1",
                "limit": "100",
                "offset": str(offset),
            },
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        all_pos.extend(data)
        if len(data) < 100:
            break
        offset += 100
        time.sleep(0.3)
    return all_pos


def fetch_market_gamma(condition_id: str) -> dict | None:
    """Resolve a conditionId -> full market metadata via Gamma API."""
    try:
        r = requests.get(
            f"{GAMMA_API}/markets/{condition_id}",
            headers=HEADERS,
            timeout=15,
        )
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


def resolve_token_ids(positions: list[dict]) -> dict[str, dict]:
    """
    For each position extract the CLOB token_id (the `asset` field).
    Returns  {token_id: position_dict}.
    Falls back to Gamma API lookup if `asset` is missing.
    """
    token_map: dict[str, dict] = {}
    gamma_cache: dict[str, dict | None] = {}

    for pos in positions:
        # Data API always returns the token id in `asset`
        tid = pos.get("asset", "")
        if tid and isinstance(tid, str) and len(tid) > 10:
            token_map[tid] = pos
            continue

        # Fallback: look up via Gamma API
        cid = pos.get("conditionId", "")
        if not cid:
            continue
        if cid not in gamma_cache:
            gamma_cache[cid] = fetch_market_gamma(cid)
            time.sleep(0.2)

        mkt = gamma_cache[cid]
        if not mkt:
            continue

        raw_outcomes = mkt.get("outcomes", "[]")
        outcomes = (
            json.loads(raw_outcomes)
            if isinstance(raw_outcomes, str)
            else (raw_outcomes or [])
        )
        raw_ids = mkt.get("clobTokenIds", "")
        clob_ids = raw_ids.split(",") if isinstance(raw_ids, str) else (raw_ids or [])
        clob_ids = [x.strip() for x in clob_ids if x.strip()]

        target_outcome = (pos.get("outcome") or "").lower()
        for i, o in enumerate(outcomes):
            if str(o).lower() == target_outcome and i < len(clob_ids):
                token_map[clob_ids[i]] = pos
                break

    return token_map


def fetch_user_stats(wallet: str) -> dict | None:
    """Fetch wallet-level stats (trade count, join date, etc.)."""
    try:
        r = requests.get(
            f"{DATA_API}/v1/user-stats",
            params={"proxyAddress": wallet},
            headers=HEADERS,
            timeout=15,
        )
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


# ── async wrappers (fix #11, #12) ─────────────────────────────────────────────
async def fetch_positions_with_retry(
    wallet: str,
    max_attempts: int = 3,
) -> list[dict]:
    """Hent positioner med eksponentiel backoff ved 429/5xx.

    Returnerer tom liste efter max_attempts mislykkede forsøg.
    Kører den synkrone HTTP-call i executor pool så event-loopet ikke blokeres.
    """
    loop = asyncio.get_event_loop()
    for attempt in range(max_attempts):
        try:
            return await loop.run_in_executor(None, fetch_positions, wallet)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 429 or status >= 500:
                wait = 2**attempt  # 1s, 2s, 4s
                log.warning(
                    "%s HTTP %s — retry %d/%d in %ds",
                    _w(wallet),
                    status,
                    attempt + 1,
                    max_attempts,
                    wait,
                )
                await asyncio.sleep(wait)
            else:
                log.error("%s HTTP error %s — giving up", _w(wallet), status)
                return []
        except requests.RequestException as exc:
            log.warning("%s request error attempt %d: %s", _w(wallet), attempt + 1, exc)
            if attempt < max_attempts - 1:
                await asyncio.sleep(2**attempt)
    log.warning(
        "%s all %d attempts failed — skipping this poll",
        _w(wallet),
        max_attempts,
    )
    return []


async def fetch_user_stats_async(wallet: str) -> dict | None:
    """Async wrapper for fetch_user_stats — kører i executor pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_user_stats, wallet)


# ── display helpers ───────────────────────────────────────────────────────────
def _w(wallet: str, label: str = "") -> str:
    """Kort wallet-tag til loglinjer.

    Foretrækker brugbart label hvis tilgængeligt, ellers kort wallet-prefix.
    """
    if label:
        return f"[{label}]"
    return f"[{wallet[:6]}…{wallet[-4:]}]"


def display_positions(positions: list[dict], wallet: str, label: str = "") -> None:
    tag = _w(wallet, label)
    if not positions:
        log.info("%s (no open positions)", tag)
        return

    total_val = 0.0
    total_pnl = 0.0

    for p in positions:
        title = p.get("title") or p.get("slug") or p.get("conditionId", "???")[:20]
        outcome = p.get("outcome", "?")
        size = float(p.get("size", 0) or 0)
        avg = float(p.get("avgPrice", 0) or p.get("buyAvg", 0) or 0)
        cur_price = float(p.get("curPrice", 0) or 0)
        cur_val = float(p.get("currentValue", 0) or 0)
        pnl = float(p.get("cashPnl", 0) or 0)
        pnl_pct = float(p.get("percentPnl", 0) or 0)

        total_val += cur_val
        total_pnl += pnl

        sign = "+" if pnl >= 0 else ""
        log.info("%s %s", tag, title)
        log.info(
            "    %s  |  size=%.1f  avg=$%.3f  now=$%.2f  val=$%.2f  PnL=%s$%.2f (%s%.1f%%)",
            outcome,
            size,
            avg,
            cur_price,
            cur_val,
            sign,
            pnl,
            sign,
            pnl_pct,
        )

    sign = "+" if total_pnl >= 0 else ""
    log.info("%s TOTAL  val=$%.2f  PnL=%s$%.2f", tag, total_val, sign, total_pnl)


# ── diff engine ───────────────────────────────────────────────────────────────
def _pos_key(p: dict) -> tuple:
    return (p.get("conditionId", ""), p.get("outcome", ""))


def diff_positions(
    old: list[dict], new: list[dict]
) -> tuple[list[dict], list[dict], list[tuple[dict, dict]]]:
    old_map = {_pos_key(p): p for p in old}
    new_map = {_pos_key(p): p for p in new}

    opened = [new_map[k] for k in set(new_map) - set(old_map)]
    closed = [old_map[k] for k in set(old_map) - set(new_map)]
    changed: list[tuple[dict, dict]] = []

    for k in set(old_map) & set(new_map):
        old_sz = float(old_map[k].get("size", 0) or 0)
        new_sz = float(new_map[k].get("size", 0) or 0)
        if abs(old_sz - new_sz) > 0.01:
            changed.append((old_map[k], new_map[k]))

    return opened, closed, changed


# ── WebSocket price stream (shared across all wallets) ────────────────────────
async def ws_price_loop(
    token_ids: list[str],
    token_map: dict[str, dict],  # token_id -> position dict (any wallet)
    token_wallet: dict[str, str],  # token_id -> wallet address
    last_prices: dict[str, float],
    new_tokens_queue: asyncio.Queue[list[str]],
) -> None:
    """
    Connect to the CLOB market WebSocket and stream live price updates
    for ALL tokens across ALL watched wallets — one shared connection.

    Nye tokens tilføjes dynamisk via ``new_tokens_queue`` — ingen fuld reconnect.
    """
    if not token_ids:
        log.info("[WS] no tokens to subscribe — skipping WebSocket")
        return

    while True:
        try:
            async with websockets.connect(
                CLOB_WS,
                ping_interval=None,
                close_timeout=5,
            ) as ws:
                sub = json.dumps(
                    {
                        "assets_ids": token_ids,
                        "type": "market",
                    }
                )
                await ws.send(sub)
                log.info(
                    "[WS] subscribed to %d token(s) across all wallets",
                    len(token_ids),
                )

                # Keepalive — CLOB expects PING every 10s
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
                            await ws.send(
                                json.dumps({"assets_ids": new_ids, "type": "market"})
                            )
                            log.info(
                                "[WS] dynamically subscribed to %d new token(s)",
                                len(new_ids),
                            )
                        except Exception as exc:
                            log.warning(
                                "[WS] failed to send dynamic subscribe: %s", exc
                            )

                ka = asyncio.create_task(keepalive())
                dt = asyncio.create_task(drain_new_tokens())

                try:
                    async for raw in ws:
                        if raw == "PONG":
                            continue
                        try:
                            msgs = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if isinstance(msgs, dict):
                            msgs = [msgs]
                        if not isinstance(msgs, list):
                            continue

                        for m in msgs:
                            etype = m.get("event_type", "")

                            if etype == "last_trade_price":
                                # Actual trade execution — always display
                                aid = m.get("asset_id", "")
                                price = float(m.get("price", 0))
                                prev = last_prices.get(aid)
                                if prev is not None and abs(prev - price) > 0.003:
                                    pos = token_map.get(aid, {})
                                    wallet = token_wallet.get(aid, "?")
                                    title = (
                                        pos.get("title") or pos.get("slug") or aid[:16]
                                    )
                                    outcome = pos.get("outcome", "?")
                                    log.info(
                                        "[TRADE] %s %s (%s)  $%.3f -> $%.3f",
                                        _w(wallet),
                                        title,
                                        outcome,
                                        prev,
                                        price,
                                    )
                                last_prices[aid] = price

                            elif etype == "price_change":
                                # Book-level changes — silently track only
                                pass

                            # book events (initial snapshot) — seed mid price
                            elif not etype:
                                aid = m.get("asset_id", "")
                                if aid and aid not in last_prices:
                                    bids = m.get("bids", [])
                                    asks = m.get("asks", [])
                                    best_bid = float(bids[0]["price"]) if bids else 0
                                    best_ask = float(asks[0]["price"]) if asks else 0
                                    if best_bid > 0 and best_ask > 0:
                                        last_prices[aid] = (best_bid + best_ask) / 2

                except websockets.ConnectionClosed:
                    log.warning("[WS] connection closed, reconnecting...")
                finally:
                    ka.cancel()
                    dt.cancel()

        except Exception as exc:
            log.error("[WS] error: %s", exc)

        await asyncio.sleep(5)


# ── health server (fix #14) ───────────────────────────────────────────────────
_last_successful_poll: float = 0.0


async def _start_health_server(poll_interval: int) -> web.AppRunner:
    """Start /health endpoint på port 8080.

    Returnerer 503 hvis seneste vellykkede poll er > 3x poll_interval gammel.
    """

    async def health_handler(request: web.Request) -> web.Response:
        if _last_successful_poll == 0.0:
            return web.Response(status=503, text="not_started")
        age = time.time() - _last_successful_poll
        if age > poll_interval * 3:
            return web.Response(status=503, text=f"stale:{age:.0f}s")
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    log.info("Health endpoint started on :8080/health")
    return runner


# ── main loop ─────────────────────────────────────────────────────────────────
async def main(wallets: list[str], interval: int) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    log.info("=" * 60)
    log.info("POLYMARKET WALLET WATCHER — %d wallet(s)", len(wallets))
    for w in wallets:
        log.info("Wallet:   %s", w)
    log.info("Polling:  every %ds", interval)
    log.info(
        "Started:  %s",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    log.info("=" * 60)

    # ── start health server ──
    health_runner = await _start_health_server(interval)

    # ── wallet stats + initial snapshot per wallet ──
    all_positions: dict[str, list[dict]] = {}  # wallet -> positions
    combined_token_map: dict[str, dict] = {}  # token_id -> position
    combined_token_wallet: dict[str, str] = {}  # token_id -> wallet

    for i, wallet in enumerate(wallets):
        if i > 0:
            await asyncio.sleep(1)  # small delay between wallets to avoid throttling

        tag = _w(wallet)

        stats = await fetch_user_stats_async(wallet)
        if stats:
            trades = stats.get("trades", "?")
            joined = stats.get("joinDate", "?")
            biggest = stats.get("largestWin", "?")
            log.info(
                "%s stats: %s trades | joined %s | largest win $%s",
                tag,
                trades,
                joined,
                biggest,
            )

        log.info("%s Fetching open positions...", tag)
        positions = await fetch_positions_with_retry(wallet)
        log.info("%s Found %d open position(s)", tag, len(positions))
        display_positions(positions, wallet)
        all_positions[wallet] = positions

        # Accumulate token maps
        tmap = resolve_token_ids(positions)
        combined_token_map.update(tmap)
        for tid in tmap:
            combined_token_wallet[tid] = wallet

    # ── build combined token list for single shared WebSocket ──
    token_ids = list(combined_token_map.keys())
    log.info(
        "[WS] Resolved %d unique token(s) across %d wallet(s)",
        len(token_ids),
        len(wallets),
    )

    # Seed prices from snapshots
    last_prices: dict[str, float] = {}
    for tid, pos in combined_token_map.items():
        cp = float(pos.get("curPrice", 0) or 0)
        if cp > 0:
            last_prices[tid] = cp

    # ── start ONE shared WebSocket in background ──
    new_tokens_queue: asyncio.Queue[list[str]] = asyncio.Queue()
    ws_task = asyncio.create_task(
        ws_price_loop(
            token_ids,
            combined_token_map,
            combined_token_wallet,
            last_prices,
            new_tokens_queue,
        )
    )

    # ── poll loop: check all wallets round-robin ──
    log.info(
        "Watching %d wallet(s) for new trades (Ctrl+C to stop)...",
        len(wallets),
    )

    global _last_successful_poll

    try:
        while True:
            await asyncio.sleep(interval)

            for i, wallet in enumerate(wallets):
                if i > 0:
                    await asyncio.sleep(1)  # throttle between wallet polls

                tag = _w(wallet)
                try:
                    current = await fetch_positions_with_retry(wallet)
                    prev = all_positions[wallet]
                    opened, closed, changed = diff_positions(prev, current)

                    if opened or closed or changed:
                        if DB_URL:
                            try:
                                async with acquire() as conn:
                                    wallet_id = await _get_or_create_wallet_id(
                                        conn, wallet
                                    )
                                    for p in opened:
                                        await _db_insert_trade_event(
                                            conn, wallet_id, "opened", p
                                        )
                                        await _db_upsert_position(conn, wallet_id, p)
                                    for p in closed:
                                        await _db_insert_trade_event(
                                            conn, wallet_id, "closed", p
                                        )
                                        await _db_mark_closed(conn, wallet_id, p)
                                    for old_p, new_p in changed:
                                        await _db_insert_trade_event(
                                            conn,
                                            wallet_id,
                                            "resized",
                                            new_p,
                                            old_p,
                                        )
                                        await _db_upsert_position(
                                            conn, wallet_id, new_p
                                        )
                            except Exception:
                                log.exception(
                                    "%s DB write failed — continuing without persistence",
                                    tag,
                                )

                    if opened:
                        log.info("%s >>> NEW POSITION(S) <<<", tag)
                        for p in opened:
                            title = p.get("title") or p.get("slug") or "?"
                            outcome = p.get("outcome", "?")
                            size = float(p.get("size", 0) or 0)
                            avg = float(p.get("avgPrice", 0) or p.get("buyAvg", 0) or 0)
                            cur_val = float(p.get("currentValue", 0) or 0)
                            log.info(
                                "  + %s %s  [%s]  size=%.1f  avg=$%.3f  val=$%.2f",
                                tag,
                                title,
                                outcome,
                                size,
                                avg,
                                cur_val,
                            )

                        # Add new tokens to shared maps + dynamic resubscribe
                        new_tmap = resolve_token_ids(opened)
                        if new_tmap:
                            combined_token_map.update(new_tmap)
                            for tid in new_tmap:
                                combined_token_wallet[tid] = wallet
                            new_ids = list(new_tmap.keys())
                            token_ids.extend(new_ids)
                            await new_tokens_queue.put(new_ids)
                            log.info(
                                "[WS] queued %d new token(s) from %s",
                                len(new_ids),
                                tag,
                            )

                    if closed:
                        log.info("%s >>> POSITION(S) CLOSED <<<", tag)
                        for p in closed:
                            title = p.get("title") or p.get("slug") or "?"
                            outcome = p.get("outcome", "?")
                            pnl = float(p.get("cashPnl", 0) or 0)
                            sign = "+" if pnl >= 0 else ""
                            log.info(
                                "  - %s %s  [%s]  PnL=%s$%.2f",
                                tag,
                                title,
                                outcome,
                                sign,
                                pnl,
                            )

                    if changed:
                        log.info("%s >>> POSITION SIZE CHANGED <<<", tag)
                        for old_p, new_p in changed:
                            title = new_p.get("title") or new_p.get("slug") or "?"
                            outcome = new_p.get("outcome", "?")
                            old_sz = float(old_p.get("size", 0) or 0)
                            new_sz = float(new_p.get("size", 0) or 0)
                            delta = new_sz - old_sz
                            sign = "+" if delta > 0 else ""
                            log.info(
                                "  ~ %s %s  [%s]  %.1f -> %.1f (%s%.1f)",
                                tag,
                                title,
                                outcome,
                                old_sz,
                                new_sz,
                                sign,
                                delta,
                            )

                    all_positions[wallet] = current
                    _last_successful_poll = time.time()

                except requests.RequestException as exc:
                    log.warning("%s poll error: %s", tag, exc)
                except Exception:
                    log.exception("%s unexpected error", tag)

    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
        await health_runner.cleanup()

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Multi-Wallet Watcher")
    parser.add_argument(
        "--wallets",
        default=",".join(DEFAULT_WALLETS),
        help="Comma-separated wallet addresses to monitor (default: %(default)s)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL,
        help="Position polling interval in seconds (default: %(default)s)",
    )
    args = parser.parse_args()

    wallet_list = [w.strip() for w in args.wallets.split(",") if w.strip()]
    if not wallet_list:
        sys.exit("Error: no wallet addresses provided")

    try:
        asyncio.run(main(wallet_list, args.interval))
    except KeyboardInterrupt:
        pass
