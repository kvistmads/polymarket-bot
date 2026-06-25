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

SIGNATUR-ARKITEKTUR (POLY_1271 / ERC-7739):
  order.maker  = deposit_wallet  (via funder param)
  order.signer = deposit_wallet  (via _DepositWalletSigner proxy på builder.signer)
  L2 auth POLY-ADDRESS = EOA    (client.signer urørt)

  Signaturformat (97 bytes):
    bytes[0:65]  = EOA ECDSA-sig af ERC-7739 TypedDataSign hash
    bytes[65:97] = CTF Exchange V2 app domain separator

  ERC-7739 TypedDataSign hash:
    keccak256(0x1901 + wallet_domain + keccak256(abi.encode(
      TypedDataSign_typehash,
      orderHash,          ← CTF Exchange V2 EIP-712 hash
      keccak256("DepositWallet"),
      keccak256("1"),
      137,
      deposit_wallet,
      bytes32(0)          ← salt
    )))
  Bekræftet via bytekode-analyse af DepositWallet impl
  0x58ca52ebe0dadfdf531cde7062e76746de4db1eb (2026-06-25).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import contextmanager
from decimal import Decimal
from typing import Generator

import httpx
from hexbytes import HexBytes

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

# CTF Exchange V2 domain separator — "app domain" i ERC-7739 kontekst.
# Bekræftet via eth_call getDomainSeparator() på Exchange V2.
_CTF_DOMAIN_SEP: bytes = bytes.fromhex(
    "3264e159346253e26a64e00b69032db0e7d32f94628de3e6eecb50304d7af3d2"
)

# Lazy-init singleton — kun brugt i live mode
_clob_client = None


# ---------------------------------------------------------------------------
# ERC-7739 TypedDataSign signatur-beregning
# ---------------------------------------------------------------------------

def _compute_erc7739_hash(order_hash: bytes, deposit_wallet: str) -> bytes:
    """Beregn ERC-7739 TypedDataSign hash som EOA'en skal signere.

    Polymarket DepositWallet (0x58ca52...de4db1eb) verificerer signaturer
    via isValidSignature(bytes32 hash, bytes sig) med disse regler:
      - sig.length == 65 → simpel ECDSA check (intern brug)
      - sig.length == 97 → TypedDataSign format (CLOB-ordrer)

    For 97-byte format ekstraherer kontrakten appDomainSeparator fra sig[65:97]
    og verificerer at EOA signerede TypedDataSign hash af (orderHash, wallet_domain).

    TypedDataSign type string (samlet fra bytekode-fragmenter):
      'TypedDataSign(bytes32 contents,string name,string version,'
      'uint256 chainId,address verifyingContract,bytes32 salt)'

    Wallet EIP-712 domain:
      name    = "DepositWallet"
      version = "1"
      chainId = 137
      verifyingContract = deposit_wallet (den specifikke wallet-adresse)
    """
    from eth_abi import encode
    from eth_utils import keccak

    DOMAIN_TH = keccak(
        b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    )
    NAME_H = keccak(b"DepositWallet")
    VER_H = keccak(b"1")

    wallet_domain = keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [DOMAIN_TH, NAME_H, VER_H, _CHAIN_ID, deposit_wallet],
        )
    )

    TDS_TH = keccak(
        b"TypedDataSign(bytes32 contents,string name,string version,"
        b"uint256 chainId,address verifyingContract,bytes32 salt)"
    )

    struct_hash = keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "address", "bytes32"],
            [TDS_TH, order_hash, NAME_H, VER_H, _CHAIN_ID, deposit_wallet, bytes(32)],
        )
    )

    return keccak(b"\x19\x01" + wallet_domain + struct_hash)


@contextmanager
def _poly1271_signing_patch(
    eoa_key: str, deposit_wallet: str
) -> Generator[None, None, None]:
    """Context manager: patch Account._sign_hash til ERC-7739 POLY_1271 format.

    Intercept enhver Account._sign_hash() der kaldes med vores EOA private key
    og erstat den 65-byte ECDSA sig med en 97-byte ERC-7739 payload:
      bytes[0:65]  = EOA sig af ERC-7739 TypedDataSign hash
      bytes[65:97] = CTF Exchange V2 domain separator (appDomainSeparator)

    CLOB pre-validator tjekker at sig.length > 65 og at sig[65:97] matcher
    appDomainSeparator for CTF Exchange V2. Kontrakten validerer resten on-chain.
    """
    import eth_account.account as _acc_mod
    from eth_account import Account

    original = _acc_mod.Account._sign_hash  # type: ignore[attr-defined]

    class _Poly1271Signed:
        """Minimal SignedMessage-kompatibel wrapper med 97-byte signature."""

        def __init__(self, payload: bytes) -> None:
            self.signature = HexBytes(payload)
            self.messageHash = HexBytes(b"\x00" * 32)
            self.r = 0
            self.s = 0
            self.v = 0

        def __iter__(self):  # type: ignore[override]
            yield "messageHash", self.messageHash
            yield "r", self.r
            yield "s", self.s
            yield "v", self.v
            yield "signature", self.signature

    def _key_bytes(k: object) -> bytes:
        """Normaliser private key til bytes (EOA key kan være str, HexBytes eller bytes)."""
        if isinstance(k, (bytes, bytearray)):
            return bytes(k)
        s = str(k)
        return bytes.fromhex(s.removeprefix("0x"))

    _eoa_key_bytes = _key_bytes(eoa_key)

    def _patched(msg_hash, private_key=None, **kwargs):  # type: ignore[no-untyped-def]
        try:
            is_our_key = _key_bytes(private_key) == _eoa_key_bytes
        except Exception:
            is_our_key = False

        if is_our_key:
            # msg_hash = CTF Exchange V2 EIP-712 orderHash (bytes-lignende)
            order_bytes = bytes(msg_hash)
            erc7739_h = _compute_erc7739_hash(order_bytes, deposit_wallet)
            real_sig = bytes(original(erc7739_h, private_key=private_key).signature)  # 65 bytes
            payload = real_sig + _CTF_DOMAIN_SEP  # 97 bytes
            log.debug("ERC-7739 POLY_1271 sig bygget: %d bytes", len(payload))
            return _Poly1271Signed(payload)
        return original(msg_hash, private_key=private_key, **kwargs)

    _acc_mod.Account._sign_hash = _patched  # type: ignore[method-assign]
    try:
        yield
    finally:
        _acc_mod.Account._sign_hash = original  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# CLOB klient-initialisering
# ---------------------------------------------------------------------------

def _get_clob_client():
    """Returnér singleton ClobClient (V2). Initialiseret ved første kald (live mode).

    Arkitektur (bekræftet 2026-06-25):
    - L1 key derivation: deterministisk, ingen browser-credentials nødvendige
    - order.maker  = deposit_wallet (funder param)
    - order.signer = deposit_wallet (_DepositWalletSigner proxy på builder.signer)
    - L2 auth POLY-ADDRESS = EOA    (client.signer urørt)
    - Signatur: ERC-7739 97-byte TypedDataSign (via _poly1271_signing_patch)
    """
    global _clob_client
    if _clob_client is not None:
        return _clob_client

    from py_clob_client_v2 import ClobClient, SignatureTypeV2  # type: ignore[import]

    key = os.environ["POLYMARKET_PRIVATE_KEY"]
    deposit_wallet = os.environ["DEPOSIT_WALLET_ADDRESS"]

    # Trin 1: Deriv API-nøgle via L1 (EOA-auth) — deterministisk, ingen browser.
    l1_client = ClobClient(
        host=CLOB_BASE,
        chain_id=_CHAIN_ID,
        key=key,
    )
    creds = l1_client.create_or_derive_api_key()
    log.info("CLOB API key (re)deriveret via L1: %s…", creds.api_key[:8])

    # Trin 2: Endelig POLY_1271-klient
    _clob_client = ClobClient(
        host=CLOB_BASE,
        chain_id=_CHAIN_ID,
        key=key,
        creds=creds,
        signature_type=SignatureTypeV2.POLY_1271,
        funder=deposit_wallet,
    )

    # Trin 3: Builder-signer proxy.
    #
    # PROBLEM: client.signer og builder.signer er SAMME Python-objekt.
    #   Patch address() in-place → POLY-ADDRESS i L2 auth = deposit_wallet → 401
    #   (CLOB verificerer POLY-ADDRESS == api_key.signing_address = EOA)
    #
    # FIX: Erstat builder.signer med proxy der returnerer deposit_wallet fra
    # address() men beholder EOA's private_key til signing.
    _eoa_signer = _clob_client.signer

    class _DepositWalletSigner:
        """Builder-signer: deposit_wallet som adresse, EOA's nøgle til signing."""

        def address(self) -> str:
            return deposit_wallet

        def sign(self, msg: str) -> str:
            return _eoa_signer.sign(msg)

        def __getattr__(self, name: str):
            return getattr(_eoa_signer, name)

    _clob_client.builder.signer = _DepositWalletSigner()

    log.info(
        "CLOB V2 client klar (key=%s…, deposit_wallet=%s…, sig=ERC7739)",
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

    key = os.environ["POLYMARKET_PRIVATE_KEY"]
    deposit_wallet = os.environ["DEPOSIT_WALLET_ADDRESS"]

    def _post() -> dict:
        # ERC-7739 patch: intercepter Account._sign_hash under ordre-oprettelse
        # og producer 97-byte POLY_1271 TypedDataSign signatur i stedet for 65-byte.
        with _poly1271_signing_patch(key, deposit_wallet):
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

    key = os.environ["POLYMARKET_PRIVATE_KEY"]
    deposit_wallet = os.environ["DEPOSIT_WALLET_ADDRESS"]

    def _post() -> dict:
        with _poly1271_signing_patch(key, deposit_wallet):
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
