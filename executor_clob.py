"""
executor_clob.py — Polymarket CLOB V2 API integration.

Eksponerer:
  get_clob_balance()           → Decimal (tilgængeligt pUSD)
  get_clob_orderbook(token_id) → dict (bid/ask)
  submit_to_clob(event, size)  → OrderResult
  sell_from_clob(event, shares)→ OrderResult

Kræver POLYMARKET_PRIVATE_KEY og DEPOSIT_WALLET_ADDRESS env vars.
Private key logges ALDRIG — heller ikke ved fejl.

CLOB V2 go-live: 28. april 2026.
Collateral: pUSD (0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB).
Pakke: py-clob-client-v2==1.0.x

═══════════════════════════════════════════════════════════════════════
POLY_1271 / ERC-7739 SIGNATUR-ARKITEKTUR
═══════════════════════════════════════════════════════════════════════

Polymarket V2 bruger deposit wallets (ERC-1271 smart contracts). Ordrer
signeres med signatureType=3 (POLY_1271), hvor:
  order.maker  = deposit_wallet  (via funder param)
  order.signer = deposit_wallet  (via _DepositWalletSigner proxy)

ORDRESIGNATUR (317 bytes — verificeret mod TypeScript SDK, issue #65):
  innerSig   [0:65]   EOA ECDSA-sig af keccak('\x19\x01'||CTF_DOM_SEP||TDS_struct_hash)
  CTF_DOM_SEP[65:97]  appDomainSeparator for CTF Exchange V2
  contentsHash[97:129] Order EIP-712 struct hash (kræves af deposit_wallet.isValidSignature)
  typeStr   [129:-2]  ORDER_TYPE_STR = "Order(uint256 salt,...)" (186 bytes)
  uint16_BE [-2:]     len(typeStr) = 186 = 0x00BA

  TDS_struct_hash = keccak(abi.encode(TDS_TYPE_HASH, contentsHash,
    keccak("DepositWallet"), keccak("1"), 137, deposit_wallet, bytes32(0)))
  TDS_TYPE_HASH = keccak("TypedDataSign(Order contents,...)Order(...)")

L1 AUTH (API-nøgle bundet til deposit_wallet):
  POLY_ADDRESS = deposit_wallet (ikke EOA!)
  POLY_SIGNATURE = 317-byte ERC-7739 ClobAuth-sig
  CLOB kalder deposit_wallet.isValidSignature() via eth_call på Polygon.
  ClobAuth-format er identisk med ordresig, bare med ClobAuth typeStr.

Reference: github.com/Polymarket/clob-client-v2/issues/65
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
from decimal import Decimal

import httpx

from db import acquire
from executor_types import OrderResult, TradeEvent

log = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
_CHAIN_ID = 137  # Polygon Mainnet

# pUSD — ny collateral token i V2 (6 decimaler, som USDC)
_PUSD_CONTRACT = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

# Legacy USDC-kontrakter — bruges KUN til wrapping-status info
_LEGACY_USDC_CONTRACTS = [
    ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "Native USDC"),
    ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "USDC.e"),
]

# CTF Exchange V2 app domain separator.
# Bekræftet via eth_call getDomainSeparator() på Exchange V2.
_CTF_DOMAIN_SEP: bytes = bytes.fromhex(
    "3264e159346253e26a64e00b69032db0e7d32f94628de3e6eecb50304d7af3d2"
)

# EIP-712 type strings
_ORDER_TYPE_STR = (
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,"
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
    "uint256 timestamp,bytes32 metadata,bytes32 builder)"
)
# Lazy-init singleton — kun brugt i live mode
_clob_client = None


# ---------------------------------------------------------------------------
# ERC-7739 / POLY_1271 core helpers
# ---------------------------------------------------------------------------

def _kk(data: bytes) -> bytes:
    """keccak256."""
    from eth_utils import keccak
    return keccak(data)


def _compute_tds_type_hash(primary_type_name: str, content_type_str: str) -> bytes:
    """TypedDataSign type hash for given content type.

    EIP-712 kræver at TypedDataSign-type-hashen inkluderer alle nested types
    sorteret alfabetisk. Da Order og ClobAuth kun har primitive felter, er
    det blot TDS_primary_str + content_type_str.

    TDS_primary = "TypedDataSign({Name} contents,string name,string version,
                   uint256 chainId,address verifyingContract,bytes32 salt)"
    full = TDS_primary + content_type_str
    type_hash = keccak256(full)
    """
    tds_primary = (
        f"TypedDataSign({primary_type_name} contents,"
        f"string name,string version,uint256 chainId,"
        f"address verifyingContract,bytes32 salt)"
    )
    return _kk((tds_primary + content_type_str).encode())


# Precomputed TypedDataSign type hash for Order
_TDS_ORDER_TYPE_HASH: bytes = _compute_tds_type_hash("Order", _ORDER_TYPE_STR)


def _build_poly1271_sig(
    app_dom_sep: bytes,
    contents_hash: bytes,
    type_str: str,
    tds_type_hash: bytes,
    deposit_wallet: str,
    priv_key: object,
) -> bytes:
    """Byg 317-byte POLY_1271 ERC-7739 TypedDataSign-signatur.

    Format (bekræftet via TypeScript SDK, Polymarket/clob-client-v2 issue #65):
      innerSig     (65 bytes)  EOA ECDSA-sig af inner_hash
      app_dom_sep  (32 bytes)  app domain separator (CTF Exchange V2 eller ClobAuth)
      contents_hash(32 bytes)  EIP-712 struct hash af contents (Order eller ClobAuth)
      type_str     (var bytes) type description string (contentsDescr)
      uint16_BE    ( 2 bytes)  len(type_str) i big-endian

    inner_hash = keccak('\x19\x01' || app_dom_sep || TDS_struct_hash)
    TDS_struct_hash = keccak(abi.encode(
        tds_type_hash, contents_hash,
        keccak("DepositWallet"), keccak("1"),
        chainId=137, verifyingContract=deposit_wallet, salt=bytes32(0)
    ))

    CLOB's length check: need = BE_uint16(sig[-2:]) + 66, have = len(sig) - 65
    Med typeStr=186 bytes: need=252, have=252 → passerer ✓
    """
    from eth_abi import encode
    from eth_account import Account

    NAME_H = _kk(b"DepositWallet")
    VER_H = _kk(b"1")

    tds_struct_hash = _kk(encode(
        ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "address", "bytes32"],
        [tds_type_hash, contents_hash, NAME_H, VER_H, _CHAIN_ID, deposit_wallet, bytes(32)],
    ))

    inner_hash = _kk(b"\x19\x01" + app_dom_sep + tds_struct_hash)
    inner_sig = bytes(Account._sign_hash(inner_hash, private_key=priv_key).signature)

    type_bytes = type_str.encode()
    trailer = struct.pack(">H", len(type_bytes))  # uint16 big-endian
    return inner_sig + app_dom_sep + contents_hash + type_bytes + trailer


# ---------------------------------------------------------------------------
# Order-signatur patch (installeres én gang ved klient-oprettelse)
# ---------------------------------------------------------------------------

def _install_order_sig_patch(deposit_wallet: str) -> None:
    """Patch ExchangeOrderBuilderV2.build_order_signature for POLY_1271.

    Intercepts kun signatureType==3 ordrer og producerer korrekt 317-byte
    ERC-7739 TypedDataSign-format i stedet for SDK'ets 65-byte standard.

    Patchen er permanent for processens levetid — tråd-sikker da Python GIL
    sikrer atomare attribute-ændringer.
    """
    from py_clob_client_v2.order_utils.exchange_order_builder_v2 import (  # type: ignore[import]
        ExchangeOrderBuilderV2,
    )
    from eth_account.messages import encode_typed_data as _enc_td

    _orig = ExchangeOrderBuilderV2.build_order_signature

    def _patched(self, typed_data: dict) -> str:  # type: ignore[no-untyped-def]
        msg = typed_data.get("message", {})
        if int(msg.get("signatureType", 0)) != 3:
            # Ikke POLY_1271 — brug standard SDK signing
            return _orig(self, typed_data)

        # Order EIP-712 struct hash (= contentsHash i POLY_1271 signaturen).
        # encode_typed_data().body = keccak(ORDER_TYPE_HASH || abi.encode(order_fields))
        encoded = _enc_td(full_message=typed_data)
        contents_hash: bytes = encoded.body  # 32 bytes

        # EOA private key via _DepositWalletSigner proxy-kæde
        priv_key = self.signer.private_key

        sig = _build_poly1271_sig(
            app_dom_sep=_CTF_DOMAIN_SEP,
            contents_hash=contents_hash,
            type_str=_ORDER_TYPE_STR,
            tds_type_hash=_TDS_ORDER_TYPE_HASH,
            deposit_wallet=deposit_wallet,
            priv_key=priv_key,
        )
        log.debug(
            "POLY_1271 ordre-sig bygget: %d bytes (forventet 317)",
            len(sig),
        )
        return "0x" + sig.hex()

    ExchangeOrderBuilderV2.build_order_signature = _patched  # type: ignore[method-assign]
    log.info("POLY_1271 ordre-signatur patch installeret (ERC-7739 317-byte format)")


# ---------------------------------------------------------------------------
# CLOB klient-initialisering
# ---------------------------------------------------------------------------

def _get_clob_client():  # type: ignore[no-untyped-def]
    """Returnér singleton ClobClient (V2). Initialiseret ved første kald (live mode).

    Arkitektur:
    - API-nøgle deriveret med EOA via standard L1 auth (ingen patches nødvendige)
    - order.maker  = deposit_wallet (funder param)
    - order.signer = deposit_wallet (_DepositWalletSigner proxy)
    - Signatur: ERC-7739 317-byte TypedDataSign (via _install_order_sig_patch)

    CLOB verificerer POLY_1271 ordrer via deposit_wallet.isValidSignature() og
    tjekker at EOA (api_key.address) er owner af deposit_wallet on-chain.
    """
    global _clob_client
    if _clob_client is not None:
        return _clob_client

    from py_clob_client_v2 import ClobClient, SignatureTypeV2  # type: ignore[import]
    from eth_utils import to_checksum_address

    key = os.environ["POLYMARKET_PRIVATE_KEY"]
    deposit_wallet = os.environ["DEPOSIT_WALLET_ADDRESS"]
    deposit_chk = to_checksum_address(deposit_wallet)

    # Trin 1: Deriv API-nøgle med EOA — standard flow, ingen patches.
    # CLOB binder nøglen til EOA. For POLY_1271 ordrer verificerer CLOB
    # at EOA == deposit_wallet.owner() on-chain.
    l1_client = ClobClient(host=CLOB_BASE, chain_id=_CHAIN_ID, key=key)
    creds = l1_client.create_or_derive_api_key()
    log.info(
        "CLOB API-nøgle deriveret (EOA=%s…): %s…",
        l1_client.signer.address()[:10],
        creds.api_key[:8],
    )

    # Trin 2: Endelig POLY_1271-klient med deposit_wallet som funder
    _clob_client = ClobClient(
        host=CLOB_BASE,
        chain_id=_CHAIN_ID,
        key=key,
        creds=creds,
        signature_type=SignatureTypeV2.POLY_1271,
        funder=deposit_chk,
    )

    # Trin 3: Builder-signer proxy — order.signer = deposit_wallet.
    # Nødvendigt for POLY_1271: CTF Exchange bruger order.signer til
    # at kalde isValidSignature(). EOA's private_key bruges til selve signeringen.
    _eoa_signer = _clob_client.signer

    class _DepositWalletSigner:
        """Builder-signer: deposit_wallet som adresse, EOA's nøgle til signing."""

        def address(self) -> str:
            return deposit_chk

        def sign(self, msg: str) -> str:
            return _eoa_signer.sign(msg)

        def __getattr__(self, name: str) -> object:
            return getattr(_eoa_signer, name)

    _clob_client.builder.signer = _DepositWalletSigner()

    # Trin 4: Installer ERC-7739 ordre-signatur patch (permanent for processens levetid)
    _install_order_sig_patch(deposit_chk)

    log.info(
        "CLOB V2 client klar — key=%s…, EOA=%s…, deposit=%s…, sig=ERC-7739-317b",
        creds.api_key[:8],
        _eoa_signer.address()[:10],
        deposit_chk[:10],
    )
    return _clob_client


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------

async def get_clob_balance() -> Decimal:
    """Læser pUSD-balance direkte fra Polygon blockchain via JSON-RPC.

    Bruger DEPOSIT_WALLET_ADDRESS fra env — ikke clob.get_address() som
    returnerer EOA-adressen. pUSD ligger i deposit wallet, ikke EOA.
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


# ---------------------------------------------------------------------------
# Orderbook
# ---------------------------------------------------------------------------

async def get_clob_orderbook(token_id: str) -> dict:
    """GET /book?token_id={token_id} — returnerer bid/ask orderbook."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
        r.raise_for_status()
        return r.json()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Token-ID opslag
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Ordre-parsing
# ---------------------------------------------------------------------------

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
        size_matched = (
            resp.get("size_matched") or resp.get("sizeMatched") or str(fallback_size)
        )
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


# ---------------------------------------------------------------------------
# FOK BUY
# ---------------------------------------------------------------------------

async def _place_fok_order(token_id: str, size: Decimal) -> OrderResult:
    """Opret og send én FOK BUY market-ordre via CLOB V2.

    size = USDC-beløb at købe for (ikke shares).
    """
    from py_clob_client_v2 import MarketOrderArgs, OrderType  # type: ignore[import]
    from py_clob_client_v2.order_utils.model.side import Side  # type: ignore[import]
    from py_clob_client_v2.exceptions import PolyException  # type: ignore[import]

    loop = asyncio.get_event_loop()
    clob = _get_clob_client()

    order_args = MarketOrderArgs(
        token_id=token_id,
        amount=float(size),
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
            log.debug("_place_fok_order: thin market for token %s", token_id[:20])
            return OrderResult(
                status="skipped", size_filled=None, price=None, error_msg=None
            )
        log.warning("_place_fok_order: PolyException — %s", exc)
        return OrderResult(
            status="failed",
            size_filled=None,
            price=None,
            error_msg=str(exc)[:200],
        )

    return _parse_order_result(resp, fallback_size=size, fallback_price=Decimal("0"))


# ---------------------------------------------------------------------------
# FOK SELL
# ---------------------------------------------------------------------------

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
        amount=float(shares),
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
            return OrderResult(
                status="skipped", size_filled=None, price=None, error_msg=None
            )
        log.warning("_place_fok_sell_order: PolyException — %s", exc)
        return OrderResult(
            status="failed",
            size_filled=None,
            price=None,
            error_msg=str(exc)[:200],
        )

    return _parse_order_result(
        resp, fallback_size=shares, fallback_price=Decimal("0")
    )


# ---------------------------------------------------------------------------
# Offentlige API-funktioner
# ---------------------------------------------------------------------------

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
