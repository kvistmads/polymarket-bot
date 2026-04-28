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
from typing import Any

import requests
from dotenv import load_dotenv

try:
    import websockets
except ImportError:
    sys.exit("Missing dependency: pip install websockets")

load_dotenv()

# ── konfiguration via env vars ────────────────────────────────────────────────
DEFAULT_WALLETS: list[str] = [
    w.strip()
    for w in os.getenv(
        "FOLLOWED_WALLETS", "0x0b7a6030507efe5db145fbb57a25ba0c5f9d86cf"
    ).split(",")
    if w.strip()
]
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "30"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
DB_URL: str = os.getenv("DB_URL", "")

# ── endpoints ─────────────────────────────────────────────────────────────────
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

HEADERS = {
    "User-Agent": "WalletWatcher/1.0",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
}


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
                "limit": 100,
                "offset": offset,
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
        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else (raw_outcomes or [])
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


# ── display helpers ───────────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _safe(s: str) -> None:
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode(), flush=True)


def _w(wallet: str) -> str:
    """Short wallet tag for log lines, e.g. [0x0b7a…86cf]"""
    return f"[{wallet[:6]}…{wallet[-4:]}]"


def display_positions(positions: list[dict], wallet: str) -> None:
    tag = _w(wallet)
    if not positions:
        _safe(f"  {tag} (no open positions)")
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
        _safe(f"  {tag} {title}")
        _safe(
            f"    {outcome}  |  size={size:.1f}  avg=${avg:.3f}  now=${cur_price:.2f}"
            f"  val=${cur_val:.2f}  PnL={sign}${pnl:.2f} ({sign}{pnl_pct:.1f}%)"
        )

    _safe(f"  {tag} {'─' * 50}")
    sign = "+" if total_pnl >= 0 else ""
    _safe(f"  {tag} TOTAL  val=${total_val:.2f}  PnL={sign}${total_pnl:.2f}")


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
    token_map: dict[str, dict],       # token_id -> position dict (any wallet)
    token_wallet: dict[str, str],     # token_id -> wallet address
    last_prices: dict[str, float],
) -> None:
    """
    Connect to the CLOB market WebSocket and stream live price updates
    for ALL tokens across ALL watched wallets — one shared connection.
    """
    if not token_ids:
        _safe(f"[{_ts()}] [WS] no tokens to subscribe — skipping WebSocket")
        return

    while True:
        try:
            async with websockets.connect(
                CLOB_WS,
                ping_interval=None,
                close_timeout=5,
            ) as ws:
                sub = json.dumps({
                    "assets_ids": token_ids,
                    "type": "market",
                })
                await ws.send(sub)
                _safe(f"[{_ts()}] [WS] subscribed to {len(token_ids)} token(s) across all wallets")

                # Keepalive — CLOB expects PING every 10s
                async def keepalive():
                    while True:
                        await asyncio.sleep(10)
                        try:
                            await ws.send("PING")
                        except Exception:
                            break

                ka = asyncio.create_task(keepalive())

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
                                        pos.get("title")
                                        or pos.get("slug")
                                        or aid[:16]
                                    )
                                    outcome = pos.get("outcome", "?")
                                    _safe(
                                        f"[{_ts()}] [TRADE] {_w(wallet)} {title} ({outcome})"
                                        f"  ${prev:.3f} -> ${price:.3f}"
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
                    _safe(f"[{_ts()}] [WS] connection closed, reconnecting...")
                finally:
                    ka.cancel()

        except Exception as e:
            _safe(f"[{_ts()}] [WS] error: {e}")

        await asyncio.sleep(5)


# ── main loop ─────────────────────────────────────────────────────────────────
async def main(wallets: list[str], interval: int) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    _safe(f"\n{'=' * 60}")
    _safe(f"  POLYMARKET WALLET WATCHER — {len(wallets)} wallet(s)")
    for w in wallets:
        _safe(f"  Wallet:   {w}")
    _safe(f"  Polling:  every {interval}s")
    _safe(f"  Started:  {datetime.now(timezone.utc).isoformat(timespec='seconds')}Z")
    _safe(f"{'=' * 60}")

    # ── wallet stats + initial snapshot per wallet ──
    all_positions: dict[str, list[dict]] = {}   # wallet -> positions
    combined_token_map: dict[str, dict] = {}    # token_id -> position
    combined_token_wallet: dict[str, str] = {}  # token_id -> wallet

    for i, wallet in enumerate(wallets):
        if i > 0:
            await asyncio.sleep(1)  # small delay between wallets to avoid throttling

        tag = _w(wallet)

        stats = fetch_user_stats(wallet)
        if stats:
            trades = stats.get("trades", "?")
            joined = stats.get("joinDate", "?")
            biggest = stats.get("largestWin", "?")
            _safe(f"\n  {tag} stats: {trades} trades | joined {joined} | largest win ${biggest}")

        _safe(f"\n[{_ts()}] {tag} Fetching open positions...")
        positions = fetch_positions(wallet)
        _safe(f"[{_ts()}] {tag} Found {len(positions)} open position(s)")
        display_positions(positions, wallet)
        all_positions[wallet] = positions

        # Accumulate token maps
        tmap = resolve_token_ids(positions)
        combined_token_map.update(tmap)
        for tid in tmap:
            combined_token_wallet[tid] = wallet

    # ── build combined token list for single shared WebSocket ──
    token_ids = list(combined_token_map.keys())
    _safe(f"\n[{_ts()}] [WS] Resolved {len(token_ids)} unique token(s) across {len(wallets)} wallet(s)")

    # Seed prices from snapshots
    last_prices: dict[str, float] = {}
    for tid, pos in combined_token_map.items():
        cp = float(pos.get("curPrice", 0) or 0)
        if cp > 0:
            last_prices[tid] = cp

    # ── start ONE shared WebSocket in background ──
    ws_task = asyncio.create_task(
        ws_price_loop(token_ids, combined_token_map, combined_token_wallet, last_prices)
    )

    # ── poll loop: check all wallets round-robin ──
    _safe(f"\n[{_ts()}] Watching {len(wallets)} wallet(s) for new trades (Ctrl+C to stop)...\n")

    try:
        while True:
            await asyncio.sleep(interval)

            ws_needs_restart = False

            for i, wallet in enumerate(wallets):
                if i > 0:
                    await asyncio.sleep(1)  # throttle between wallet polls

                tag = _w(wallet)
                try:
                    current = fetch_positions(wallet)
                    prev = all_positions[wallet]
                    opened, closed, changed = diff_positions(prev, current)

                    if opened:
                        _safe(f"\n[{_ts()}] {tag} >>> NEW POSITION(S) <<<")
                        for p in opened:
                            title = p.get("title") or p.get("slug") or "?"
                            outcome = p.get("outcome", "?")
                            size = float(p.get("size", 0) or 0)
                            avg = float(p.get("avgPrice", 0) or p.get("buyAvg", 0) or 0)
                            cur_val = float(p.get("currentValue", 0) or 0)
                            _safe(
                                f"  + {tag} {title}  [{outcome}]"
                                f"  size={size:.1f}  avg=${avg:.3f}  val=${cur_val:.2f}"
                            )

                        # Add new tokens to shared maps
                        new_tmap = resolve_token_ids(opened)
                        if new_tmap:
                            combined_token_map.update(new_tmap)
                            for tid in new_tmap:
                                combined_token_wallet[tid] = wallet
                            new_ids = list(new_tmap.keys())
                            token_ids.extend(new_ids)
                            _safe(f"[{_ts()}] [WS] adding {len(new_ids)} new token(s) from {tag}")
                            ws_needs_restart = True

                    if closed:
                        _safe(f"\n[{_ts()}] {tag} >>> POSITION(S) CLOSED <<<")
                        for p in closed:
                            title = p.get("title") or p.get("slug") or "?"
                            outcome = p.get("outcome", "?")
                            pnl = float(p.get("cashPnl", 0) or 0)
                            sign = "+" if pnl >= 0 else ""
                            _safe(f"  - {tag} {title}  [{outcome}]  PnL={sign}${pnl:.2f}")

                    if changed:
                        _safe(f"\n[{_ts()}] {tag} >>> POSITION SIZE CHANGED <<<")
                        for old_p, new_p in changed:
                            title = new_p.get("title") or new_p.get("slug") or "?"
                            outcome = new_p.get("outcome", "?")
                            old_sz = float(old_p.get("size", 0) or 0)
                            new_sz = float(new_p.get("size", 0) or 0)
                            delta = new_sz - old_sz
                            sign = "+" if delta > 0 else ""
                            _safe(
                                f"  ~ {tag} {title}  [{outcome}]"
                                f"  {old_sz:.1f} -> {new_sz:.1f} ({sign}{delta:.1f})"
                            )

                    all_positions[wallet] = current

                except requests.RequestException as e:
                    _safe(f"[{_ts()}] {tag} [POLL] Request error: {e}")
                except Exception as e:
                    _safe(f"[{_ts()}] {tag} [POLL] Error: {e}")

            # Restart shared WS once after all wallets polled if new tokens appeared
            if ws_needs_restart:
                ws_task.cancel()
                try:
                    await ws_task
                except asyncio.CancelledError:
                    pass
                ws_task = asyncio.create_task(
                    ws_price_loop(token_ids, combined_token_map, combined_token_wallet, last_prices)
                )

    except KeyboardInterrupt:
        _safe(f"\n[{_ts()}] Shutting down...")
    finally:
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass

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
