"""
test_proxy.py — Afprøver POLY_PROXY (signatureType=1) for deposit wallet-ordrer.

POLY_PROXY = 1: EOA der ejer en Polymarket Proxy wallet (funder=deposit_wallet)
POLY_1271  = 3: Smart contracts der signerer selv (giver "signer must match API key")

Kør: docker compose exec executor python3 test_proxy.py
"""
import os
import json
from dotenv import load_dotenv

load_dotenv()

from py_clob_client_v2 import ClobClient, MarketOrderArgs, OrderType, SignatureTypeV2
from py_clob_client_v2.order_utils.model.side import Side

KEY = os.environ["POLYMARKET_PRIVATE_KEY"]
DEPOSIT = os.environ["DEPOSIT_WALLET_ADDRESS"]
HOST = "https://clob.polymarket.com"

TOKEN = "77911208241982327373495855644935587349201177208106713081551029073015187679590"

print("=== POLY_PROXY (signatureType=1) test ===")

tmp = ClobClient(host=HOST, chain_id=137, key=KEY)
creds = tmp.derive_api_key()

client = ClobClient(
    host=HOST,
    chain_id=137,
    key=KEY,
    creds=creds,
    signature_type=SignatureTypeV2.POLY_PROXY,
    funder=DEPOSIT,
)

args = MarketOrderArgs(
    token_id=TOKEN,
    amount=0.01,
    side=Side.BUY,
    order_type=OrderType.FOK,
)

order = client.create_market_order(args)
print(f"signatureType : {order.signatureType}  (0=EOA, 1=PROXY, 2=GNOSIS, 3=1271)")
print(f"maker         : {order.maker}")
print(f"signer        : {order.signer}")
print(f"api_key       : {creds.api_key}")

print("\nPoster ordre...")
try:
    resp = client.create_and_post_market_order(args, order_type=OrderType.FOK)
    print(f"Svar: {json.dumps(resp, indent=2, default=str)}")
    status = resp.get("status", "")
    if status == "matched":
        print("\nORDRE FYLDT!")
    elif resp.get("error"):
        print(f"\nFejl fra CLOB: {resp['error']}")
    else:
        print(f"\nStatus: {status or 'ukendt'} (FOK ikke fyldt = normalt ved lav pris)")
except Exception as e:
    print(f"\nException: {type(e).__name__}: {e}")
