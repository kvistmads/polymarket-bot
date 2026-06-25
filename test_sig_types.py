#!/usr/bin/env python3
"""
test_sig_types.py — Systematisk test af alle CLOB V2 signaturtyper.

Kør: docker compose exec executor python3 test_sig_types.py

Formål:
1. Kald on-chain for at finde wallet-type (ProxyWallet vs SafeWallet)
2. Prøv POLY_PROXY (1) med EOA-signatur → klassisk PolyProxy-flow
3. Prøv POLY_GNOSIS_SAFE (2) med EOA-signatur → Safe-flow
4. Prøv POLY_1271 (3) med TypedDataSign-formateret signatur
"""

import os
import json
import httpx
from eth_utils import keccak
from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_typed_data

CLOB_BASE = "https://clob.polymarket.com"
CHAIN_ID = 137
EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
EOA = "0xbdAABb7FD059088817065e37FeF5AB5590eC8d8D"
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"

def eth_call(to: str, data: str) -> str:
    resp = httpx.post(
        POLYGON_RPC,
        json={"jsonrpc": "2.0", "method": "eth_call",
              "params": [{"to": to, "data": data}, "latest"], "id": 1},
        timeout=15,
    )
    return resp.json().get("result", "0x")

def address_from_return(hex_result: str) -> str:
    """Udpak adresse fra eth_call retur (32 bytes, padded)."""
    cleaned = hex_result.replace("0x", "")
    if len(cleaned) >= 64:
        return "0x" + cleaned[-40:]
    return "0x0000000000000000000000000000000000000000"

def selector(fn_sig: str) -> bytes:
    return keccak(text=fn_sig)[:4]

print("=" * 60)
print("STEP 1: On-chain wallet type detection")
print("=" * 60)

# Call getProxyWalletAddress(EOA) og getSafeWalletAddress(EOA) på Exchange V2
padded_eoa = EOA[2:].lower().zfill(64)

call_proxy = "0x" + selector("getProxyWalletAddress(address)").hex() + padded_eoa
call_safe  = "0x" + selector("getSafeWalletAddress(address)").hex() + padded_eoa

try:
    proxy_result = eth_call(EXCHANGE_V2, call_proxy)
    safe_result  = eth_call(EXCHANGE_V2, call_safe)
    proxy_wallet = address_from_return(proxy_result)
    safe_wallet  = address_from_return(safe_result)
    print(f"getProxyWalletAddress({EOA[:10]}...) = {proxy_wallet}")
    print(f"getSafeWalletAddress ({EOA[:10]}...) = {safe_wallet}")

    deposit_wallet = os.environ["DEPOSIT_WALLET_ADDRESS"]
    print(f"DEPOSIT_WALLET_ADDRESS = {deposit_wallet}")

    if proxy_wallet.lower() == deposit_wallet.lower():
        print("✓ Deposit wallet er en POLY PROXY → brug POLY_PROXY (signatureType=1)")
        detected_type = "PROXY"
    elif safe_wallet.lower() == deposit_wallet.lower():
        print("✓ Deposit wallet er en GNOSIS SAFE → brug POLY_GNOSIS_SAFE (signatureType=2)")
        detected_type = "SAFE"
    else:
        print("! Deposit wallet matcher INGEN kendt type — ingen af resultaterne stemmer")
        print(f"  (proxy={proxy_wallet}, safe={safe_wallet})")
        detected_type = "UNKNOWN"
except Exception as exc:
    print(f"On-chain kald fejlede: {exc}")
    detected_type = "UNKNOWN"

print()
print("=" * 60)
print("STEP 2: L1 derive API key")
print("=" * 60)

from py_clob_client_v2 import ClobClient, SignatureTypeV2
from py_clob_client_v2.clob_types import ApiCreds

KEY = os.environ["POLYMARKET_PRIVATE_KEY"]
DEPOSIT_WALLET = os.environ["DEPOSIT_WALLET_ADDRESS"]

l1 = ClobClient(host=CLOB_BASE, chain_id=CHAIN_ID, key=KEY)
creds = l1.create_or_derive_api_key()
print(f"API key: {creds.api_key[:8]}…")

print()
print("=" * 60)
print("STEP 3: Test POLY_PROXY (type=1) — EOA som signer")
print("=" * 60)
print("Forventning: order.signer=EOA, order.maker=deposit_wallet, signatureType=1")
print("CLOB bør verificere: getProxyWalletAddress(EOA)==maker AND ecrecover(hash,sig)==EOA")

try:
    from py_clob_client_v2 import MarketOrderArgs, OrderType
    from py_clob_client_v2.order_utils.model.side import Side

    # POLY_PROXY klient — INGEN proxy-hack, signer forbliver EOA
    c_proxy = ClobClient(
        host=CLOB_BASE, chain_id=CHAIN_ID, key=KEY, creds=creds,
        signature_type=SignatureTypeV2.POLY_PROXY,
        funder=DEPOSIT_WALLET,
    )
    print(f"builder.signer.address() = {c_proxy.builder.signer.address()}")
    print(f"builder.funder           = {c_proxy.builder.funder}")

    # Prøv en minimal ordre mod et aktivt marked
    # Brug USDC-NOK med token_id vi kender (fra monitor)
    TEST_TOKEN_ID = None
    try:
        # Hent et tilfældigt aktivt marked fra CLOB
        resp = httpx.get(f"{CLOB_BASE}/markets?active=true&limit=1", timeout=10)
        markets = resp.json()
        if isinstance(markets, dict) and "data" in markets:
            mkt = markets["data"][0]
            token_id = mkt.get("tokens", [{}])[0].get("token_id", "")
            if token_id:
                TEST_TOKEN_ID = token_id
                print(f"Test token_id: {TEST_TOKEN_ID[:20]}…")
    except Exception as e:
        print(f"Market fetch fejlede: {e}")

    if not TEST_TOKEN_ID:
        # Fallback: brug en kendt token_id (NBA / small market)
        TEST_TOKEN_ID = "71321045679252212594626385532706912750332728571942532289631379312455583992937"
        print(f"Bruger fallback token_id: {TEST_TOKEN_ID[:20]}…")

    args_proxy = MarketOrderArgs(
        token_id=TEST_TOKEN_ID,
        amount=1.0,
        side=Side.BUY,
        order_type=OrderType.FOK,
    )

    resp_proxy = c_proxy.create_and_post_market_order(order_args=args_proxy, order_type=OrderType.FOK)
    print(f"✓ POLY_PROXY SUCCESS: {json.dumps(resp_proxy, indent=2)[:500]}")
except Exception as exc:
    print(f"✗ POLY_PROXY fejl: {exc}")

print()
print("=" * 60)
print("STEP 4: Test POLY_GNOSIS_SAFE (type=2) — EOA som signer")
print("=" * 60)

try:
    c_safe = ClobClient(
        host=CLOB_BASE, chain_id=CHAIN_ID, key=KEY, creds=creds,
        signature_type=SignatureTypeV2.POLY_GNOSIS_SAFE,
        funder=DEPOSIT_WALLET,
    )
    print(f"builder.signer.address() = {c_safe.builder.signer.address()}")

    args_safe = MarketOrderArgs(
        token_id=TEST_TOKEN_ID,
        amount=1.0,
        side=Side.BUY,
        order_type=OrderType.FOK,
    )
    resp_safe = c_safe.create_and_post_market_order(order_args=args_safe, order_type=OrderType.FOK)
    print(f"✓ POLY_GNOSIS_SAFE SUCCESS: {json.dumps(resp_safe, indent=2)[:500]}")
except Exception as exc:
    print(f"✗ POLY_GNOSIS_SAFE fejl: {exc}")

print()
print("=" * 60)
print("STEP 5: Test EOA (type=0) direkte — signer=EOA, maker=EOA")
print("=" * 60)
print("(Hvis dette virker bruges EOA direkte — ingen proxy wallet nødvendig)")

try:
    c_eoa = ClobClient(
        host=CLOB_BASE, chain_id=CHAIN_ID, key=KEY, creds=creds,
        signature_type=SignatureTypeV2.EOA,
        # INGEN funder — maker=EOA
    )
    print(f"builder.signer.address() = {c_eoa.builder.signer.address()}")
    print(f"builder.funder           = {c_eoa.builder.funder}")

    args_eoa = MarketOrderArgs(
        token_id=TEST_TOKEN_ID,
        amount=1.0,
        side=Side.BUY,
        order_type=OrderType.FOK,
    )
    resp_eoa = c_eoa.create_and_post_market_order(order_args=args_eoa, order_type=OrderType.FOK)
    print(f"✓ EOA SUCCESS: {json.dumps(resp_eoa, indent=2)[:500]}")
except Exception as exc:
    print(f"✗ EOA fejl: {exc}")

print()
print("DONE — se fejlmeddeleser ovenfor for at identificere korrekt signaturtype.")
