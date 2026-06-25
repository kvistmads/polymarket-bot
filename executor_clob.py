"""
executor_clob.py — Polymarket CLOB V2 API integration.

Eksponerer:
  get_clob_balance()          → Decimal (tilgængeligt pUSD)
  get_clob_orderbook(token_id)→ dict (bid/ask)
  submit_to_clob(event, size) → OrderResult
  sell_from_clob(event, shares)→ OrderResult

Kræver POLYMARKET_PRIVATE_KEY env var kun i live mode.
Private key logges ALDRIG — heller ikke ved fejl.

CLOB V2 go-live: 28. april 2026.
Collateral: pUSD (0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB).
Pakke: py-clob-client-v2==1.0.0
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
_CHAIN_ID = 137  # Polygon Mainnet

# pUSD — ny collateral token i V2 (6 decimaler, som USDC)
_PUSD_CONTRACT = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

# Legacy USDC-kontrakter — bruges KUN til wrapping-status info i get_clob_balance
_LEGACY_USDC_CONTRACTS = [
    ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "Native USDC"),
    ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "USDC.e"),
]

# Lazy-init singleton — kun brugt i live mode
_clob_client = None


def _get_clob_client():
    """Returnér singleton ClobClient (V2). Initialiseret ved første kald (live mode).

    POLY_1271 arkitektur (bekræftet via localStorage + SDK-kildekode):
    - api_key.owner = EOA (baseAddress i poly_clob_api_key_map)
    - order.signer = EOA (SDK default) = api_key.owner ✓
    - order.maker = deposit_wallet (via funder param)
    - signature_type = POLY_1271 → CLOB kalder deposit_wallet.isValidSignature() ✓

    Credentials udløber/invalideres når polymarket.com regenererer nøglen.
    Hent nye via Console på polymarket.com:
      JSON.parse(localStorage.getItem('poly_clob_api_key_map'))['<deposit_wallet>']
    Opdater CLOB_API_KEY/SECRET/PASSPHRASE i .env på serveren.
    """
    global _clob_client
    if _clob_client is not None:
        return _clob_client

    from py_clob_client_v2 import ClobClient, SignatureTypeV2  # type: ignore[import]
    from py_clob_client_v2.clob_types import ApiCreds  # type: ignore[import]

    key = os.environ["POLYMARKET_PRIVATE_KEY"]
    deposit_wallet = os.environ["DEPOSIT_WALLET_ADDRESS"]

    creds = ApiCreds(
        api_key=os.environ["CLOB_API_KEY"],
        api_secret=os.environ["CLOB_SECRET"],
        api_passphrase=os.environ["CLOB_PASSPHRASE"],
    )

    _clob_client = ClobClient(
        host=CLOB_BASE,
        chain_id=_CHAIN_ID,
        key=key,
        creds=creds,
        signature_type=SignatureTypeV2.POLY_1271,
        funder=deposit_wallet,
    )
    # Monkey-patch PÅKRÆVET:
    # - api_key.owner = deposit_wallet (key gemt under deposit_wallet i localStorage-map)
    # - SDK sætter order.signer = signer.address() = EOA → mismatch med api_key.owner
    # - Patch: order.signer = deposit_wallet = api_key.owner ✓
    # - Signer.sign() bruger self.private_key direkte — upåvirket af patch ✓
    # - CLOB EIP-1271: deposit_wallet.isValidSignature(order_hash, eoa_sig) ✓
    _clob_client.builder.signer.address = lambda: deposit_wallet  # type: ignore[method-assign]

    log.info(
        "CLOB V2 client klar (key=%s…, deposit_wallet=%s…)",
        creds.api_key[:8],
        deposit_wallet[:10],
    )
    return _clob_client


async def get_clob_balance() -> Decimal:
    """Læser pUSD-balance direkte fra Polygon blockchain via JSON-RPC.

    Bruger DEPOSIT_WALLET_ADDRESS fra env — ikke clob.get_address() som
    returnerer EOA-adressen. pUSD ligger i deposit wallet, ikke EOA.

    V2 collateral er pUSD (ikke USDC.e). Hvis pUSD=0 men USDC.e>0 skal du
    wrappe: kald wrap() på CollateralOnramp 0x93070a847efEf7F70739046A929D47a521F5B8ee.
    """
    wallet = os.environ["DEPOSIT_WALLET_ADDRESS"]

    # ERC-20 balanceOf(address): selector 0x70a08231 + 32-byte padded address
    padded = wallet[2:].lower().zfill(64)
    data = "0x70a08231" + padded

    polygon_rpcs = [
        "https://polygon-bor-rpc.publicnode.com",
        "https://1rpc.io/matic",
    ]

    async with httpx.AsyncClient(timeout=15) as client:
        for rpc in polygon_rpcs:
            try:
                # pUSD-balance (primær trading-collateral)
                r_pusd = await client.post(
                    rpc,
                    json={
                        "jsonrpc": "2.0",
                        "method": "eth_call",
                        "params": [{"to": _PUSD_CONTRACT, "data": data}, "latest"],
                        "id": 1,
                    },
                )
                result = r_pusd.json().get("result", "0x0") or "0x0"
                pusd = Decimal(int(result, 16)) / Decimal(10**6)
                log.info("get_clob_balance: %s pUSD (via %s)", pusd, rpc)

                if pusd == 0:
                    # Tjek legacy USDC for wrapping-vejledning
                    legacy_total = Decimal("0")
                    for contract_addr, name in _LEGACY_USDC_CONTRACTS:
                        r_leg = await client.post(
                            rpc,
                            json={
                                "jsonrpc": "2.0",
                                "method": "eth_call",
                                "params": [{"to": contract_addr, "data": data}, "latest"],
                                "id": 2,
                            },
                        )
                        res_leg = r_leg.json().get("result", "0x0") or "0x0"
                        legacy_total += Decimal(int(res_leg, 16)) / Decimal(10**6)
                    if legacy_total > 0:
                        log.warning(
                            "get_clob_balance: pUSD=0 men %s legacy USDC fundet — "
                            "wrap til pUSD via CollateralOnramp for at handle",
                            legacy_total,
                        )

                return pusd

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


def _parse_order_result(
    resp: dict | None,
    fallback_size: Decimal,
    fallback_price: Decimal,
) -> OrderResult:
    """Oversæt CLOB V2 API-svar til OrderResult."""
    if not isinstance(resp, dict):
        return OrderResult(
            status="failed",
            size_filled=None,
            price=None,
            error_msg=f"Uventet svar-type: {type(resp)}",
        )

    if resp.get("error"):
        return OrderResult(
            status="failed",
            size_filled=None,
            price=None,
            error_msg=str(resp["error"])[:200],
        )

    status = resp.get("status", "")
    if status == "matched":
        size_matched = resp.get("size_matched") or resp.get("sizeMatched") or str(fallback_size)
        price_val = resp.get("price") or str(fallback_price)
        return OrderResult(
            status="filled",
            size_filled=Decimal(str(size_matched)),
            price=Decimal(str(price_val)),
            error_msg=None,
        )

    # FOK der ikke blev fyldt — thin market, ingen Telegram
    if status in ("not_matched", "cancelled", ""):
        return OrderResult(
            status="skipped",
            size_filled=None,
            price=None,
            error_msg=None,
        )

    return OrderResult(
        status="failed",
        size_filled=None,
        price=None,
        error_msg=f"Uventet status: {status} — {str(resp)[:200]}",
    )


async def _place_fok_order(token_id: str, size: Decimal) -> OrderResult:
    """Opret og send én FOK BUY market-ordre via CLOB V2.

    size = USDC-beløb at købe for (ikke shares).
    V2 SDK henter automatisk tick_size, neg_risk og pris fra orderbook.
    """
    from py_clob_client_v2 import MarketOrderArgs, OrderType  # type: ignore[import]
    from py_clob_client_v2.order_utils.model.side import Side  # type: ignore[import]
    from py_clob_client_v2.exceptions import PolyException  # type: ignore[import]

    loop = asyncio.get_event_loop()
    clob = _get_clob_client()

    order_args = MarketOrderArgs(
        token_id=token_id,
        amount=float(size),  # USDC at bruge
        side=Side.BUY,
        order_type=OrderType.FOK,
    )

    def _post() -> dict:
        return clob.create_and_post_market_order(
            order_args=order_args,
            order_type=OrderType.FOK,
        )

    try:
        resp = await loop.run_in_executor(None, _post)
    except PolyException as exc:
        msg = str(exc).lower()
        if "no match" in msg:
            # Thin market — ingen asks tilgængelige, skip stille
            log.debug("_place_fok_order: thin market for token %s", token_id[:20])
            return OrderResult(status="skipped", size_filled=None, price=None, error_msg=None)
        log.warning("_place_fok_order: PolyException — %s", exc)
        return OrderResult(
            status="failed",
            size_filled=None,
            price=None,
            error_msg=str(exc)[:200],
        )

    return _parse_order_result(resp, fallback_size=size, fallback_price=Decimal("0"))


async def _place_fok_sell_order(token_id: str, shares: Decimal) -> OrderResult:
    """Opret og send én FOK SELL market-ordre via CLOB V2.

    shares = antal CTF-tokens at sælge.
    """
    from py_clob_client_v2 import MarketOrderArgs, OrderType  # type: ignore[import]
    from py_clob_client_v2.order_utils.model.side import Side  # type: ignore[import]
    from py_clob_client_v2.exceptions import PolyException  # type: ignore[import]

    loop = asyncio.get_event_loop()
    clob = _get_clob_client()

    order_args = MarketOrderArgs(
        token_id=token_id,
        amount=float(shares),  # shares at sælge
        side=Side.SELL,
        order_type=OrderType.FOK,
    )

    def _post() -> dict:
        return clob.create_and_post_market_order(
            order_args=order_args,
            order_type=OrderType.FOK,
        )

    try:
        resp = await loop.run_in_executor(None, _post)
    except PolyException as exc:
        msg = str(exc).lower()
        if "no match" in msg:
            log.debug("_place_fok_sell_order: thin market for token %s", token_id[:20])
            return OrderResult(status="skipped", size_filled=None, price=None, error_msg=None)
        log.warning("_place_fok_sell_order: PolyException — %s", exc)
        return OrderResult(
            status="failed",
            size_filled=None,
            price=None,
            error_msg=str(exc)[:200],
        )

    return _parse_order_result(resp, fallback_size=shares, fallback_price=Decimal("0"))


async def submit_to_clob(event: TradeEvent, size: Decimal) -> OrderResult:
    """POST /order — FOK BUY markedsordre på Polymarket CLOB V2.

    size = USDC-beløb at investere.
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

        return await _place_fok_order(token_id, size)

    except Exception as exc:
        log.exception("submit_to_clob fejlede (key redacted)")
        return OrderResult(
            status="failed",
            size_filled=None,
            price=None,
            error_msg=f"{type(exc).__name__}: {exc}",
        )


async def sell_from_clob(event: TradeEvent, shares: Decimal) -> OrderResult:
    """Sælg vores copy-position på CLOB V2 — FOK SELL markedsordre.

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

        return await _place_fok_sell_order(token_id, shares)

    except Exception as exc:
        log.exception("sell_from_clob fejlede (key redacted)")
        return OrderResult(
            status="failed",
            size_filled=None,
            price=None,
            error_msg=f"{type(exc).__name__}: {exc}",
        )
