"""
Engangsskript til at teste Polymarket CLOB ordre-API direkte.
Kør på Vultr: docker compose exec executor python3 test_order_debug.py
Sender en FOK BUY med pris 0.01 (vil aldrig fyldes) — kun for at se svaret.
"""
import os
import json
import httpx
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, RequestArgs
from py_clob_client.utilities import order_to_json
from py_clob_client.http_helpers.helpers import post as _clob_post
from py_clob_client.endpoints import POST_ORDER
from py_clob_client.headers.headers import create_level_2_headers

KEY = os.environ["POLYMARKET_PRIVATE_KEY"]
FUNDER = os.environ.get("POLYMARKET_FUNDER_ADDRESS") or os.environ.get("FUNDER_ADDRESS")

# Token fra Bobe2's seneste trade
TOKEN_ID = "77911208241982327373495855644935587349201177208106713081551029073015187679590"

clob = ClobClient(host="https://clob.polymarket.com", key=KEY, chain_id=137, signature_type=0)
creds = clob.create_or_derive_api_creds()
clob.set_api_creds(creds)

print(f"Wallet: {clob.get_address()}")
print(f"Token:  {TOKEN_ID[:20]}...")

# Tjek neg_risk for dette token
neg = clob.get_neg_risk(TOKEN_ID)
print(f"neg_risk: {neg}")

# Tjek tick_size
ts = clob.get_tick_size(TOKEN_ID)
print(f"tick_size: {ts}")

# Opret ordre med tick_size som pris
order_args = OrderArgs(
    token_id=TOKEN_ID,
    price=float(ts),
    size=1.0,
    side="BUY",
    fee_rate_bps=0,
    nonce=0,
    expiration=0,
)

print("\nOpretter ordre...")
signed = clob.create_order(order_args)
print("Ordre signeret OK")

# Print fuldt order dict (uden signature)
od = signed.dict()
od_safe = {k: v for k, v in od.items() if k != "signature"}
od_safe["signature"] = "REDACTED"
print(f"Order dict: {json.dumps(od_safe, indent=2, default=str)}")

# Test 1: original post_order (ingen patches)
print("\n--- TEST 1: Upatched clob.post_order() ---")
try:
    resp1 = clob.post_order(signed, OrderType.FOK)
    print(f"Svar: {resp1}")
except Exception as e:
    print(f"Fejl: {e}")

# Test 2: Manuel post med deferExec
print("\n--- TEST 2: Manuel post med deferExec:false ---")
body2 = order_to_json(signed, clob.creds.api_key, OrderType.FOK, False)
body2["deferExec"] = False
ser2 = json.dumps(body2, separators=(",", ":"), ensure_ascii=False)
req2 = RequestArgs(method="POST", request_path=POST_ORDER, body=body2, serialized_body=ser2)
hdrs2 = create_level_2_headers(clob.signer, clob.creds, req2)
r2 = httpx.post(
    f"https://clob.polymarket.com{POST_ORDER}",
    headers={**hdrs2, "Content-Type": "application/json", "User-Agent": "py_clob_client"},
    content=ser2.encode(),
)
print(f"Status: {r2.status_code} | Body: {r2.text[:300]}")

# Test 3: Uden postOnly feltet (matcher JS-klienten præcist)
print("\n--- TEST 3: Uden postOnly (som JS-klienten) ---")
body3 = {
    "deferExec": False,
    "order": order_to_json(signed, clob.creds.api_key, OrderType.FOK, False)["order"],
    "owner": clob.creds.api_key,
    "orderType": "FOK",
}
ser3 = json.dumps(body3, separators=(",", ":"), ensure_ascii=False)
req3 = RequestArgs(method="POST", request_path=POST_ORDER, body=body3, serialized_body=ser3)
hdrs3 = create_level_2_headers(clob.signer, clob.creds, req3)
r3 = httpx.post(
    f"https://clob.polymarket.com{POST_ORDER}",
    headers={**hdrs3, "Content-Type": "application/json", "User-Agent": "@polymarket/clob-client"},
    content=ser3.encode(),
)
print(f"Status: {r3.status_code} | Body: {r3.text[:300]}")

print("\nFærdig.")
