"""
executor_gates.py — 7-gate verifikation for trade executor.

Gate 1: Wallet aktuelt fulgt (unfollowed_at IS NULL)?
Gate 2: Kun 'opened' events?
Gate 3: Ikke allerede eksponeret i markedet?
Gate 4: Markedet likvidt (spread < 5%)?
Gate 5: Mere end 2 timer til close?
Gate 6: Order-size inden for hard cap?
Gate 7: Daglig loss limit ikke nået?

Eksponerer:
  passes_gates(conn, event) → tuple[bool, str]
  calculate_size(conn, wallet_id) → Decimal
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg
import httpx

from executor_clob import get_clob_balance, get_clob_orderbook
from executor_types import TradeEvent

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
POSITION_SIZE_PCT: str = os.getenv("POSITION_SIZE_PCT", "0.05")
MAX_DAILY_LOSS: Decimal = Decimal(os.getenv("MAX_DAILY_LOSS", "50"))
_SIZE_HARD_CAP_PCT = Decimal("0.20")
_MIN_ORDER_SIZE = Decimal("1.0")
_MAX_SPREAD = Decimal("0.05")
_MARKET_CLOSE_BUFFER_H = 2


async def passes_gates(conn: asyncpg.Connection, event: TradeEvent) -> tuple[bool, str]:
    """Kør alle 7 gates i rækkefølge. Første fejl stopper eksekveringen."""
    checks = [
        _gate1_wallet_followed,
        _gate2_only_opened,
        _gate3_not_exposed,
        _gate4_liquidity,
        _gate5_market_close,
        _gate6_size_cap,
        _gate7_daily_loss,
    ]
    for check in checks:
        ok, reason = await check(conn, event)
        if not ok:
            return False, reason
    return True, ""


# ── Gate 1 ─────────────────────────────────────────────────────────────────────


async def _gate1_wallet_followed(
    conn: asyncpg.Connection, event: TradeEvent
) -> tuple[bool, str]:
    row = await conn.fetchrow(
        "SELECT 1 FROM followed_wallets WHERE wallet_id = $1 AND unfollowed_at IS NULL",
        event.wallet_id,
    )
    if not row:
        return False, f"wallet {event.wallet_id} ikke fulgt"
    return True, ""


# ── Gate 2 ─────────────────────────────────────────────────────────────────────


async def _gate2_only_opened(
    conn: asyncpg.Connection, event: TradeEvent
) -> tuple[bool, str]:
    if event.event_type != "opened":
        return False, f"event_type='{event.event_type}' — kun 'opened' kopieres"
    return True, ""


# ── Gate 3 ─────────────────────────────────────────────────────────────────────


async def _gate3_not_exposed(
    conn: asyncpg.Connection, event: TradeEvent
) -> tuple[bool, str]:
    row = await conn.fetchrow(
        """
        SELECT 1 FROM copy_orders
        WHERE condition_id = $1
          AND status IN ('submitted', 'filled', 'paper')
        """,
        event.condition_id,
    )
    if row:
        return False, f"allerede eksponeret i {event.condition_id[:12]}…"
    return True, ""


# ── Gate 4 ─────────────────────────────────────────────────────────────────────


async def _gate4_liquidity(
    conn: asyncpg.Connection, event: TradeEvent
) -> tuple[bool, str]:
    token_id = await _get_token_id(conn, event.condition_id, event.outcome)
    if not token_id:
        return False, "token_id ikke fundet — kan ikke tjekke likviditet"
    try:
        book = await get_clob_orderbook(token_id)
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return False, "tomt orderbook"
        best_bid = Decimal(str(bids[0]["price"]))
        best_ask = Decimal(str(asks[0]["price"]))
        if best_ask == 0:
            return False, "best_ask=0"
        spread = (best_ask - best_bid) / best_ask
        if spread >= _MAX_SPREAD:
            return False, f"spread {spread:.1%} >= 5%"
        return True, ""
    except Exception:
        log.exception("Gate 4 likviditetscheck fejlede for %s", token_id)
        return False, "likviditetscheck fejlede"


async def _get_token_id(
    conn: asyncpg.Connection, condition_id: str, outcome: str
) -> str | None:
    """Opslag i market_metadata — returnerer token_id for givent outcome."""
    import json

    row = await conn.fetchrow(
        "SELECT clob_token_ids, outcomes FROM market_metadata WHERE condition_id = $1",
        condition_id,
    )
    if not row:
        return None
    outcomes_raw = row["outcomes"]
    token_ids_raw = row["clob_token_ids"]
    outcomes = (
        json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
    )
    token_ids = (
        json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
    )
    if not outcomes or not token_ids:
        return None
    target = outcome.lower()
    for i, o in enumerate(outcomes):
        if str(o).lower() == target and i < len(token_ids):
            return str(token_ids[i])
    return None


# ── Gate 5 ─────────────────────────────────────────────────────────────────────


async def _gate5_market_close(
    conn: asyncpg.Connection, event: TradeEvent
) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{GAMMA_BASE}/markets",
                params={"condition_id": event.condition_id},
            )
            r.raise_for_status()
            data = r.json()

        markets = data if isinstance(data, list) else [data]
        if not markets:
            return False, "marked ikke fundet i Gamma API"

        end_date_str = markets[0].get("endDate") or markets[0].get("end_date_iso")
        if not end_date_str:
            return False, "endDate mangler i Gamma API svar"

        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) + timedelta(hours=_MARKET_CLOSE_BUFFER_H)
        if end_date < cutoff:
            return (
                False,
                f"marked lukker om < {_MARKET_CLOSE_BUFFER_H}t ({end_date.isoformat()})",
            )
        return True, ""
    except Exception:
        log.exception("Gate 5 market-close check fejlede")
        return False, "market-close check fejlede"


# ── Gate 6 ─────────────────────────────────────────────────────────────────────


async def _gate6_size_cap(
    conn: asyncpg.Connection, event: TradeEvent
) -> tuple[bool, str]:
    try:
        size = await calculate_size(conn, event.wallet_id)
    except Exception:
        log.exception("Gate 6 size-beregning fejlede")
        return False, "size-beregning fejlede"
    if size < _MIN_ORDER_SIZE:
        return False, f"size {size:.2f} < min {_MIN_ORDER_SIZE}"
    return True, ""


# ── Gate 7 ─────────────────────────────────────────────────────────────────────


async def _gate7_daily_loss(
    conn: asyncpg.Connection, event: TradeEvent
) -> tuple[bool, str]:
    row = await conn.fetchrow(
        "SELECT realized_pnl FROM daily_stats WHERE date = CURRENT_DATE"
    )
    if row and row["realized_pnl"] is not None:
        pnl = Decimal(str(row["realized_pnl"]))
        if pnl <= -MAX_DAILY_LOSS:
            return False, f"daglig tab {pnl:.2f} <= -{MAX_DAILY_LOSS}"
    return True, ""


# ── Position sizing ────────────────────────────────────────────────────────────


async def calculate_size(conn: asyncpg.Connection, wallet_id: int) -> Decimal:
    """Beregn ordre-størrelse baseret på per-wallet override eller global pct.

    Hard cap: 20% af tilgængeligt cash.
    """
    row = await conn.fetchrow(
        "SELECT position_size_pct FROM followed_wallets "
        "WHERE wallet_id = $1 AND unfollowed_at IS NULL",
        wallet_id,
    )
    pct = (
        Decimal(str(row["position_size_pct"]))
        if row and row["position_size_pct"]
        else Decimal(POSITION_SIZE_PCT)
    )
    available_cash = await get_clob_balance()
    size = available_cash * pct
    return min(size, available_cash * _SIZE_HARD_CAP_PCT)
