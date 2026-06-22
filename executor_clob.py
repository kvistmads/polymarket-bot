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
    import py_clob_client.http_helpers.helpers as _clob_http  # type: ignore[import]

    # py_clob_client opretter _http_client som modul-niveau singleton uden proxy.
    # Patch den manuelt hvis HTTPS_PROXY er sat i env.
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy_url:
        import httpx as _httpx
        _clob_http._http_client = _httpx.Client(http2=True, proxy=proxy_url)
        log.info("CLOB HTTP-klient patched med proxy (url redacted)")

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
    """Læser USDC-balance direkte fra Polygon blockchain via JSON-RPC.

    CLOB API'ets /balance-allowance viser kun pre-deposited beløb, ikke
    wallet-balancen. Vi læser direkte fra USDC ERC-20 kontrakterne.
    """
    clob = _get_clob_client()
    wallet = clob.get_address()

    # ERC-20 balanceOf(address): selector 0x70a08231 + 32-byte padded address
    padded = wallet[2:].lower().zfill(64)
    data = "0x70a08231" + padded

    # Polygon USDC-kontrakter (native USDC og USDC.e/PoS-bridged)
    usdc_contracts = [
        ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "Native USDC"),
        ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "USDC.e"),
    ]
    polygon_rpcs = [
        "https://polygon-bor-rpc.publicnode.com",     # PublicNode — ingen auth
        "https://1rpc.io/matic",                      # 1RPC — ingen auth
    ]

    async with httpx.AsyncClient(timeout=15) as client:
        for rpc in polygon_rpcs:
            total = Decimal("0")
            try:
                for contract_addr, contract_name in usdc_contracts:
                    r = await client.post(
                        rpc,
                        json={
                            "jsonrpc": "2.0",
                            "method": "eth_call",
                            "params": [{"to": contract_addr, "data": data}, "latest"],
                            "id": 1,
                        },
                    )
                    resp_json = r.json()
                    result = resp_json.get("result", "0x0") or "0x0"
                    raw = int(result, 16)
                    amount = Decimal(raw) / Decimal(10**6)
                    log.debug(
                        "get_clob_balance: %s @ %s = %s USDC",
                        contract_name, rpc, amount,
                    )
                    total += amount
                log.info("get_clob_balance: %s USDC total (via %s)", total, rpc)
                return total
            except Exception:
                log.warning("RPC %s fejlede — prøver næste", rpc, exc_info=True)
                continue

    log.error("get_clob_balance: alle RPCs fejlede for wallet %s", wallet)
    return Decimal("0")


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
        # FOK med 0.3% slippage simulerer market order — CLOB max er 0.99
        order_price = min(best_ask * Decimal("1.003"), Decimal("0.99"))

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


async def sell_from_clob(event: TradeEvent, shares: Decimal) -> OrderResult:
    """Sælg vores copy-position på CLOB — FOK markedsordre på bid-siden.

    shares = antal tokens vi holder (size_filled_usdc / buy_price).
    Kaldt kun i live mode fra executor._process_sell_signal.
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
        bids = book.get("bids", [])
        if not bids:
            return OrderResult(
                status="failed",
                size_filled=None,
                price=None,
                error_msg="ingen bids i orderbook — kan ikke sælge",
            )

        best_bid = Decimal(str(bids[0]["price"]))
        # 0.3% slippage ved salg (sæt lidt lavere for at sikre fill)
        order_price = max(best_bid * Decimal("0.997"), Decimal("0.01"))

        return await _place_fok_sell_order(token_id, order_price, shares)

    except Exception as exc:
        log.exception("sell_from_clob fejlede (key redacted)")
        return OrderResult(
            status="failed",
            size_filled=None,
            price=None,
            error_msg=f"{type(exc).__name__}: {exc}",
        )


async def _place_fok_sell_order(token_id: str, price: Decimal, shares: Decimal) -> OrderResult:
    """Opret og send én FOK SELL limit ordre."""
    import json as _json
    from py_clob_client.clob_types import OrderArgs, OrderType, RequestArgs  # type: ignore[import]
    from py_clob_client.utilities import order_to_json  # type: ignore[import]
    from py_clob_client.http_helpers.helpers import post as _clob_post  # type: ignore[import]
    from py_clob_client.endpoints import POST_ORDER  # type: ignore[import]
    from py_clob_client.headers.headers import create_level_2_headers  # type: ignore[import]

    loop = asyncio.get_event_loop()
    clob = _get_clob_client()

    order_args = OrderArgs(
        token_id=token_id,
        price=float(price),
        size=float(shares),
        side="SELL",
        fee_rate_bps=0,
        nonce=0,
        expiration=0,
    )
    signed = await loop.run_in_executor(None, clob.create_order, order_args)

    # JS-klienten sender deferExec:false i body — bruges af Polymarket til klientversion-check
    def _post_patched():
        body = order_to_json(signed, clob.creds.api_key, OrderType.FOK, False)
        body["deferExec"] = False
        serialized = _json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        req = RequestArgs(method="POST", request_path=POST_ORDER, body=body, serialized_body=serialized)
        headers = create_level_2_headers(clob.signer, clob.creds, req)
        return _clob_post("{}{}".format(clob.host, POST_ORDER), headers=headers, data=serialized)

    resp = await loop.run_in_executor(None, _post_patched)

    if isinstance(resp, dict) and resp.get("status") == "matched":
        return OrderResult(
            status="filled",
            size_filled=Decimal(str(resp.get("size_matched", shares))),
            price=Decimal(str(resp.get("price", price))),
            error_msg=None,
        )
    return OrderResult(
        status="failed",
        size_filled=None,
        price=None,
        error_msg=str(resp),
    )


async def _place_fok_order(token_id: str, price: Decimal, size: Decimal) -> OrderResult:
    """Opret og send én FOK BUY limit ordre. Kaldt kun fra submit_to_clob."""
    import json as _json
    from py_clob_client.clob_types import OrderArgs, OrderType, RequestArgs  # type: ignore[import]
    from py_clob_client.utilities import order_to_json  # type: ignore[import]
    from py_clob_client.http_helpers.helpers import post as _clob_post  # type: ignore[import]
    from py_clob_client.endpoints import POST_ORDER  # type: ignore[import]
    from py_clob_client.headers.headers import create_level_2_headers  # type: ignore[import]

    loop = asyncio.get_event_loop()
    clob = _get_clob_client()

    order_args = OrderArgs(
        token_id=token_id,
        price=float(price),
        size=float(size),
        side="BUY",
        fee_rate_bps=0,
        nonce=0,
        expiration=0,
    )
    signed = await loop.run_in_executor(None, clob.create_order, order_args)

    # Debug: log hvad vi sender så vi kan sammenligne med JS-klienten
    def _post_patched():
        body = order_to_json(signed, clob.creds.api_key, OrderType.FOK, False)
        body["deferExec"] = False
        serialized = _json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        debug_body = _json.loads(serialized)
        debug_body["order"]["signature"] = "REDACTED"
        log.warning("ORDER_DEBUG BODY: %s", _json.dumps(debug_body, indent=2))
        req = RequestArgs(method="POST", request_path=POST_ORDER, body=body, serialized_body=serialized)
        headers = create_level_2_headers(clob.signer, clob.creds, req)
        import httpx as _httpx
        raw = _httpx.post(
            "{}{}".format(clob.host, POST_ORDER),
            headers={**headers, "Content-Type": "application/json", "User-Agent": "py_clob_client"},
            content=serialized.encode("utf-8"),
        )
        log.warning("ORDER_DEBUG RESPONSE status=%s body=%s", raw.status_code, raw.text[:500])
        return raw.json() if raw.status_code == 200 else {"error": raw.text}

    resp = await loop.run_in_executor(None, _post_patched)

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
