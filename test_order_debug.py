"""
test_order_debug.py — CLOB V2 ordre-test.
Kør på Vultr: docker compose exec executor python3 test_order_debug.py

Sender én FOK BUY (pris=tick_size, vil aldrig fyldes) for at bekræfte V2 signing virker.
Slet denne fil efter succesfuld test.
"""
import os
import json
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()

from py_clob_client_v2 import ClobClient, MarketOrderArgs, OrderType
from py_clob_client_v2.order_utils.model.side import Side

KEY = os.environ["POLYMARKET_PRIVATE_KEY"]
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

# Token fra Bobe2's seneste trade
TOKEN_ID = "77911208241982327373495855644935587349201177208106713081551029073015187679590"

print("Initialiserer CLOB V2 klient...")
client = ClobClient(host=HOST, chain_id=CHAIN_ID, key=KEY)
creds = client.create_or_derive_api_key()
client = ClobClient(host=HOST, chain_id=CHAIN_ID, key=KEY, creds=creds)

print(f"Wallet: {client.get_address()}")
print(f"Token:  {TOKEN_ID[:20]}...")

ts = client.get_tick_size(TOKEN_ID)
neg = client.get_neg_risk(TOKEN_ID)
print(f"tick_size: {ts}")
print(f"neg_risk:  {neg}")

# Minimal ordre — 0.01 USDC FOK BUY (vil aldrig fyldes, bekræfter blot at signing virker)
print("\n--- TEST: FOK BUY 0.01 USDC ---")
order_args = MarketOrderArgs(
    token_id=TOKEN_ID,
    amount=0.01,   # USDC
    side=Side.BUY,
    order_type=OrderType.FOK,
)
try:
    resp = client.create_and_post_market_order(order_args=order_args, order_type=OrderType.FOK)
    print(f"Svar: {json.dumps(resp, indent=2, default=str)}")
    if resp.get("status") == "matched":
        print("✅ FYLDT!")
    elif resp.get("error"):
        print(f"❌ FEJL: {resp['error']}")
    else:
        print(f"ℹ️  Status: {resp.get('status', 'ukendt')} (FOK ikke fyldt = normalt ved lav pris)")
except Exception as e:
    print(f"Exception: {type(e).__name__}: {e}")

print("\nFærdig.")
