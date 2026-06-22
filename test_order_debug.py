"""
test_order_debug.py — CLOB V2 ordre-test med deposit wallet.
Kør på Vultr: docker compose exec executor python3 test_order_debug.py

Krav: DEPOSIT_WALLET_ADDRESS skal være sat i .env
Sender én FOK BUY (0.01 USDC) for at bekræfte V2 signing + deposit wallet virker.
Slet denne fil efter succesfuld test.
"""
import os
import json
from dotenv import load_dotenv

load_dotenv()

from py_clob_client_v2 import ClobClient, MarketOrderArgs, OrderType, SignatureTypeV2
from py_clob_client_v2.order_utils.model.side import Side

KEY = os.environ["POLYMARKET_PRIVATE_KEY"]
DEPOSIT_WALLET = os.environ.get("DEPOSIT_WALLET_ADDRESS", "")
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

# Token fra Bobe2's seneste trade
TOKEN_ID = "77911208241982327373495855644935587349201177208106713081551029073015187679590"

if not DEPOSIT_WALLET:
    print("❌ DEPOSIT_WALLET_ADDRESS er ikke sat i .env!")
    print("   Log ind på polymarket.com for at deploye din deposit wallet,")
    print("   og tilføj adressen til .env som DEPOSIT_WALLET_ADDRESS=0x...")
    exit(1)

print(f"Initialiserer CLOB V2 klient med deposit wallet...")
client = ClobClient(host=HOST, chain_id=CHAIN_ID, key=KEY)
creds = client.create_or_derive_api_key()
client = ClobClient(
    host=HOST,
    chain_id=CHAIN_ID,
    key=KEY,
    creds=creds,
    signature_type=SignatureTypeV2.POLY_1271,
    funder=DEPOSIT_WALLET,
)

print(f"EOA:            {client.get_address()}")
print(f"Deposit wallet: {DEPOSIT_WALLET}")
print(f"Token:          {TOKEN_ID[:20]}...")

ts = client.get_tick_size(TOKEN_ID)
neg = client.get_neg_risk(TOKEN_ID)
print(f"tick_size: {ts}")
print(f"neg_risk:  {neg}")

# Minimal ordre — 0.01 USDC FOK BUY (bekræfter deposit wallet signing)
print("\n--- TEST: FOK BUY 0.01 USDC (deposit wallet) ---")
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
        print(f"ℹ️  Status: {resp.get('status', 'ukendt')} (FOK ikke fyldt = normalt ved lav pris/thin market)")
except Exception as e:
    print(f"Exception: {type(e).__name__}: {e}")

print("\nFærdig.")
