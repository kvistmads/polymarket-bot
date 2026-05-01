"""
executor_clob.py — Polymarket CLOB API integration.

Eksponerer:
  get_clob_balance()          → Decimal (tilgængeligt USDC)
  get_clob_orderbook(token_id)→ dict (bid/ask)
  submit_to_clob(event, size) → OrderResult

Kræver POLYMARKET_PRIVATE_KEY env var kun i live mode.
Private key logges ALDRIG — heller ikke ved fejl.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from decimal import Decimal

import httpx

from db import acquire
from executor_types import OrderResult, TradeEvent

log = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
_CHAIN_ID = 137  # Polygon Mainnet

# Lazy-init singleton — kun brugt i live mode
_clob_client = None


def _get_clob_client():
    """Returnér singleton ClobClient. Initialiseret ved første kald (live mode)."""
    global _clob_client
    if _clob_client is not None:
        return _clob_client

    from py_clob_client.client import ClobClient  # type: ignore[import]

    key = os.environ["POLYMARKET_PRIVATE_KEY"]
    # py_clob_client >= 0.17 bruger 'key' i stedet for 'private_key'
    _clob_client = ClobClient(host=CLOB_BASE, key=key, chain_id=_CHAIN_ID, signature_type=0)
    try:
        creds = _clob_client.create_or_derive_api_creds()
        _clob_client.set_api_creds(creds)
        log.info("CLOB client initialiseret (key redacted)")
    except Exception:
        log.exception("Kunne ikke hente CLOB API credentials")
        raise
    return _clob_client


async def get_clob_balance() -> Decimal:
    """GET /balance — returnerer tilgængeligt USDC fra CLOB API."""
    loop = asyncio.get_event_loop()
    try:
        clob = _get_clob_client()
        resp = await loop.run_in_executor(None, clob.get_balance)
        if isinstance(resp, dict):
            return Decimal(str(resp.get("USDC", "0")))
        return Decimal(str(resp))
    except Exception:
        log.exception("get_clob_balance fejlede")
        raise


async def get_clob_orderbook(token_id: str) -> dict:
    """GET /book?token_id={token_id} — returnerer bid/ask orderbook."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
        r.raise_for_status()
        return r.json()  # type: ignore[return-value]


async def _resolve_token_id(condition_id: str, outcome: str) -> str | None:
    """Opslag af CLOB token_id fra market_metadata tabel."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT clob_token_ids, outcomes FROM market_metadata WHERE condition_id = $1",
            condition_id,
        )
    if not row:
        log.warning("Ingen market_metadata for condition_id=%s", condition_id)
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


async def submit_to_clob(event: TradeEvent, size: Decimal) -> OrderResult:
    """POST /order — L2-signeret FOK markedsordre på Polymarket CLOB.

    Private key tilgås KUN her — logges ALDRIG.
    """
    try:
        token_id = await _resolve_token_id(event.condition_id, event.outcome)
        if not token_id:
            return OrderResult(
                status="failed",
                size_filled=None,
                price=None,
                error_msg=f"token_id ikke fundet for {event.condition_id}/{event.outcome}",
            )

        book = await get_clob_orderbook(token_id)
        asks = book.get("asks", [])
        if not asks:
            return OrderResult(
                status="failed",
                size_filled=None,
                price=None,
                error_msg="ingen asks i orderbook",
            )

        best_ask = Decimal(str(asks[0]["price"]))
        # FOK med 0.3% slippage simulerer market order
        order_price = min(best_ask * Decimal("1.003"), Decimal("1.0"))

        return await _place_fok_order(token_id, order_price, size)

    except Exception as exc:
        # Aldrig log private key — kun fejltype
        log.exception("submit_to_clob fejlede (key redacted)")
        return OrderResult(
            status="failed",
            size_filled=None,
            price=None,
            error_msg=f"{type(exc).__name__}: {exc}",
        )


async def _place_fok_order(token_id: str, price: Decimal, size: Decimal) -> OrderResult:
    """Opret og send én FOK limit ordre. Kaldt kun fra submit_to_clob."""
    from py_clob_client.clob_types import OrderArgs, OrderType, Side  # type: ignore[import]

    loop = asyncio.get_event_loop()
    clob = _get_clob_client()

    order_args = OrderArgs(
        token_id=token_id,
        price=float(price),
        size=float(size),
        side=Side.BUY,
        fee_rate_bps=0,
        nonce=0,
        expiration=0,
    )
    signed = await loop.run_in_executor(None, clob.create_order, order_args)
    resp = await loop.run_in_executor(
        None, lambda: clob.post_order(signed, OrderType.FOK)
    )

    if isinstance(resp, dict) and resp.get("status") == "matched":
        return OrderResult(
            status="filled",
            size_filled=Decimal(str(resp.get("size_matched", size))),
            price=Decimal(str(resp.get("price", price))),
            error_msg=None,
        )
    return OrderResult(
        status="failed",
        size_filled=None,
        price=None,
        error_msg=str(resp),
    )
