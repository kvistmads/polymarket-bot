#!/usr/bin/env python3
"""
test_sig_types.py — Systematisk test af alle CLOB V2 signaturtyper.

Kør: docker compose exec executor python3 test_sig_types.py

Strategi:
1. On-chain kald for wallet-type (ProxyWallet vs SafeWallet)
2. Prøv POLY_PROXY (1) — EOA signer, enklest
3. Prøv POLY_GNOSIS_SAFE (2) — EOA signer
4. Prøv on-chain validateOrderSignature for at teste signaturer direkte
5. Prøv POLY_1271 (3) med TypedDataSign-format (97 bytes: sig+appDomSep)
"""

import os, json, sys, time
import httpx
from eth_utils import keccak
from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_typed_data

CLOB_BASE = "https://clob.polymarket.com"
CHAIN_ID = 137
EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc-mainnet.maticvigil.com",
    "https://matic-mainnet.chainstacklabs.com",
]

KEY            = os.environ["POLYMARKET_PRIVATE_KEY"]
DEPOSIT_WALLET = os.environ["DEPOSIT_WALLET_ADDRESS"]
EOA            = Account.from_key(KEY).address

def banner(title):
    print(f"\n{'='*60}\n{title}\n{'='*60}")

def eth_call(to: str, data: str, timeout: int = 10) -> str | None:
    for rpc in POLYGON_RPCS:
        try:
            r = httpx.post(rpc,
                json={"jsonrpc":"2.0","method":"eth_call",
                      "params":[{"to":to,"data":data},"latest"],"id":1},
                timeout=timeout)
            result = r.json().get("result")
            if result and result != "0x":
                return result
        except Exception as e:
            print(f"  RPC {rpc[:30]}... fejl: {e}")
    return None

def addr_from_call(result: str) -> str:
    if result and len(result) >= 42:
        return "0x" + result.replace("0x","")[-40:]
    return "0x" + "0"*40

def selector(fn_sig: str) -> str:
    return "0x" + keccak(text=fn_sig)[:4].hex()

# ─── STEP 1: On-chain wallet type ────────────────────────────────
banner("STEP 1: On-chain wallet type detection")

padded_eoa = EOA[2:].lower().zfill(64)
result_proxy = eth_call(EXCHANGE_V2, selector("getProxyWalletAddress(address)") + padded_eoa)
result_safe  = eth_call(EXCHANGE_V2, selector("getSafeWalletAddress(address)") + padded_eoa)

proxy_wallet = addr_from_call(result_proxy) if result_proxy else None
safe_wallet  = addr_from_call(result_safe) if result_safe else None

print(f"EOA:                  {EOA}")
print(f"DEPOSIT_WALLET:       {DEPOSIT_WALLET}")
print(f"getProxyWalletAddress = {proxy_wallet}")
print(f"getSafeWalletAddress  = {safe_wallet}")

if proxy_wallet and proxy_wallet.lower() == DEPOSIT_WALLET.lower():
    detected = "POLY_PROXY"
    print(f"✓ Wallet type: POLY PROXY (signatureType=1)")
elif safe_wallet and safe_wallet.lower() == DEPOSIT_WALLET.lower():
    detected = "POLY_GNOSIS_SAFE"
    print(f"✓ Wallet type: GNOSIS SAFE (signatureType=2)")
else:
    detected = "UNKNOWN"
    print(f"! Wallet type: UKENDT - ingen match")

# ─── STEP 2: L1 derive API key ───────────────────────────────────
banner("STEP 2: L1 derive API key")
from py_clob_client_v2 import ClobClient, SignatureTypeV2, MarketOrderArgs, OrderType
from py_clob_client_v2.order_utils.model.side import Side

l1 = ClobClient(host=CLOB_BASE, chain_id=CHAIN_ID, key=KEY)
creds = l1.create_or_derive_api_key()
print(f"API key: {creds.api_key[:8]}… (hentet via L1 EOA-auth)")

# Hent en test token_id
TEST_TOKEN_ID = None
try:
    resp = httpx.get(f"{CLOB_BASE}/markets?active=true&limit=5", timeout=10)
    markets = resp.json()
    data = markets.get("data", markets) if isinstance(markets, dict) else markets
    if isinstance(data, list):
        for mkt in data:
            tokens = mkt.get("tokens", [])
            if tokens and tokens[0].get("token_id"):
                TEST_TOKEN_ID = tokens[0]["token_id"]
                print(f"Test market: {mkt.get('question','?')[:50]}")
                print(f"Test token_id: {TEST_TOKEN_ID[:30]}…")
                break
except Exception as e:
    print(f"Market fetch fejl: {e}")

if not TEST_TOKEN_ID:
    TEST_TOKEN_ID = "71321045679252212594626385532706912750332728571942532289631379312455583992937"
    print(f"Bruger fallback token_id")

# ─── STEP 3: POLY_PROXY (type=1) med EOA signer ──────────────────
banner("STEP 3: Test POLY_PROXY (type=1) — signer=EOA")
print(f"order.signer={EOA[:12]}…  order.maker={DEPOSIT_WALLET[:12]}…  signatureType=1")
try:
    c = ClobClient(host=CLOB_BASE, chain_id=CHAIN_ID, key=KEY, creds=creds,
                   signature_type=SignatureTypeV2.POLY_PROXY, funder=DEPOSIT_WALLET)
    print(f"builder.signer.address() = {c.builder.signer.address()}")
    args = MarketOrderArgs(token_id=TEST_TOKEN_ID, amount=1.0, side=Side.BUY, order_type=OrderType.FOK)
    resp = c.create_and_post_market_order(order_args=args, order_type=OrderType.FOK)
    print(f"✓ POLY_PROXY SUCCESS: {json.dumps(resp)[:300]}")
except Exception as exc:
    print(f"✗ POLY_PROXY fejl: {type(exc).__name__}: {exc}")

# ─── STEP 4: POLY_GNOSIS_SAFE (type=2) med EOA signer ────────────
banner("STEP 4: Test POLY_GNOSIS_SAFE (type=2) — signer=EOA")
print(f"order.signer={EOA[:12]}…  order.maker={DEPOSIT_WALLET[:12]}…  signatureType=2")
try:
    c = ClobClient(host=CLOB_BASE, chain_id=CHAIN_ID, key=KEY, creds=creds,
                   signature_type=SignatureTypeV2.POLY_GNOSIS_SAFE, funder=DEPOSIT_WALLET)
    print(f"builder.signer.address() = {c.builder.signer.address()}")
    args = MarketOrderArgs(token_id=TEST_TOKEN_ID, amount=1.0, side=Side.BUY, order_type=OrderType.FOK)
    resp = c.create_and_post_market_order(order_args=args, order_type=OrderType.FOK)
    print(f"✓ POLY_GNOSIS_SAFE SUCCESS: {json.dumps(resp)[:300]}")
except Exception as exc:
    print(f"✗ POLY_GNOSIS_SAFE fejl: {type(exc).__name__}: {exc}")

# ─── STEP 5: On-chain validateOrderSignature test ────────────────
banner("STEP 5: On-chain validateOrderSignature (direkte kontrakttest)")

# Byg en test-ordre for signature-validering på kæden
from py_clob_client_v2.order_utils.exchange_order_builder_v2 import ExchangeOrderBuilderV2
from py_clob_client_v2.order_utils.model.order_data_v2 import OrderDataV2
from py_clob_client_v2.order_utils.utils import generate_order_salt
from py_clob_client_v2 import ClobClient

# Brug nuværende POLY_1271 + proxy-hack (nuværende approach)
c_1271 = ClobClient(host=CLOB_BASE, chain_id=CHAIN_ID, key=KEY, creds=creds,
                    signature_type=SignatureTypeV2.POLY_1271, funder=DEPOSIT_WALLET)

eoa_signer = c_1271.signer

class _DWSigner:
    def address(self):
        return DEPOSIT_WALLET
    def sign(self, msg):
        return eoa_signer.sign(msg)
    def __getattr__(self, name):
        return getattr(eoa_signer, name)

c_1271.builder.signer = _DWSigner()

# Byg order (uden at sende til CLOB)
from py_clob_client_v2.config import get_contract_config
cfg = get_contract_config(CHAIN_ID)
exchange_addr = cfg.exchange_v2
from py_clob_client_v2.constants import BYTES32_ZERO
ts = str(int(time.time()*1000))
order_data = OrderDataV2(
    maker=DEPOSIT_WALLET,
    tokenId=TEST_TOKEN_ID,
    makerAmount="1000000",   # 1 pUSD (6 decimaler)
    takerAmount="2000000",   # 2 tokens
    side=Side.BUY,
    signer=DEPOSIT_WALLET,   # POLY_1271: signer=deposit_wallet
    signatureType=SignatureTypeV2.POLY_1271,
    timestamp=ts,
    metadata=BYTES32_ZERO,
    builder=BYTES32_ZERO,
)
builder = ExchangeOrderBuilderV2(exchange_addr, CHAIN_ID, _DWSigner())
order = builder.build_order(order_data)
typed_data = builder.build_order_typed_data(order)
sig_65 = builder.build_order_signature(typed_data)
order_hash_hex = builder.build_order_hash(typed_data)

print(f"Order hash: {order_hash_hex[:20]}…")
print(f"Signatur (65 bytes): {sig_65[:20]}… (len={len(bytes.fromhex(sig_65[2:]))} bytes)")

# Prøv direkte isValidSignature på deposit_wallet kontrakten
print(f"\nKald deposit_wallet.isValidSignature(orderHash, sig65):")
is_valid_sel = "0x1626ba7e"  # isValidSignature(bytes32,bytes)
order_hash_bytes = bytes.fromhex(order_hash_hex[2:])
# ABI encode: (bytes32, bytes) → bytes32 + offset(64) + length(65) + sig(65) + padding
sig_bytes = bytes.fromhex(sig_65[2:])
encoded = encode(['bytes32', 'bytes'], [order_hash_bytes, sig_bytes])
call_data = is_valid_sel + encoded.hex()
is_valid_result = eth_call(DEPOSIT_WALLET, call_data)
print(f"  Resultat: {is_valid_result}")
if is_valid_result and is_valid_result.startswith("0x1626ba7e"):
    print(f"  ✓ VALID (magic value returneret) — 65 bytes virker på kontrakten!")
elif is_valid_result == "0x":
    print(f"  ✗ Reverted (tom svar) — 65 bytes accepteres IKKE")
else:
    print(f"  ? Uventet svar")

# Prøv også 97-byte TypedDataSign: sig(65) + appDomainSep(32)
app_domain_sep = bytes.fromhex("3264e159346253e26a64e00b69032db0e7d32f94628de3e6eecb50304d7af3d2")

# For 97-byte: EOA skal signere en ANDEN hash
# Prøv simpel version: keccak256(abi.encode(appDomSep, orderHash))
simple_tds_hash = keccak(encode(['bytes32', 'bytes32'], [app_domain_sep, order_hash_bytes]))
sig_97_inner = Account._sign_hash(simple_tds_hash, KEY)
sig_97_bytes = sig_97_inner.signature + app_domain_sep  # 65 + 32 = 97 bytes
print(f"\nKald deposit_wallet.isValidSignature(orderHash, sig97_simple):")
encoded_97 = encode(['bytes32', 'bytes'], [order_hash_bytes, sig_97_bytes])
call_data_97 = is_valid_sel + encoded_97.hex()
is_valid_97 = eth_call(DEPOSIT_WALLET, call_data_97)
print(f"  Resultat: {is_valid_97}")
if is_valid_97 and is_valid_97.startswith("0x1626ba7e"):
    print(f"  ✓ VALID! Simple TypedDataSign (keccak(appDomSep, orderHash)) virker!")
else:
    print(f"  ✗ Ikke valid med simple TypedDataSign")

# Prøv 97-byte med standard orderHash som det der signeres (original sig + appDomSep)
print(f"\nKald deposit_wallet.isValidSignature(orderHash, sig65+appDomSep):")
sig_97_passthrough = sig_bytes + app_domain_sep  # original sig + appDomSep
encoded_pt = encode(['bytes32', 'bytes'], [order_hash_bytes, sig_97_passthrough])
call_data_pt = is_valid_sel + encoded_pt.hex()
is_valid_pt = eth_call(DEPOSIT_WALLET, call_data_pt)
print(f"  Resultat: {is_valid_pt}")
if is_valid_pt and is_valid_pt.startswith("0x1626ba7e"):
    print(f"  ✓ VALID! Passthrough 97-byte format virker!")
else:
    print(f"  ✗ Ikke valid med passthrough 97-byte")

# ─── STEP 6: POLY_1271 med current proxy hack ────────────────────
banner("STEP 6: POLY_1271 (type=3) med _DepositWalletSigner proxy (nuværende kod)")
try:
    c_test = ClobClient(host=CLOB_BASE, chain_id=CHAIN_ID, key=KEY, creds=creds,
                        signature_type=SignatureTypeV2.POLY_1271, funder=DEPOSIT_WALLET)
    c_test.builder.signer = _DWSigner()
    args = MarketOrderArgs(token_id=TEST_TOKEN_ID, amount=1.0, side=Side.BUY, order_type=OrderType.FOK)
    resp = c_test.create_and_post_market_order(order_args=args, order_type=OrderType.FOK)
    print(f"✓ POLY_1271 SUCCESS: {json.dumps(resp)[:300]}")
except Exception as exc:
    print(f"✗ POLY_1271 fejl: {type(exc).__name__}: {exc}")

banner("FERDIG — sammenlign fejlmeddeleser ovenfor")
