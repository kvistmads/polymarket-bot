"""
test_order_debug.py — CLOB V2 diagnostik + ordre-test.
Kør på Vultr: docker compose exec executor python3 test_order_debug.py

FASE 1: Udskriver kildekode for nøgle-metoder i py_clob_client_v2
FASE 2: Sletter gammel API-nøgle og opretter ny med plain EOA-klient
FASE 3: Test ordre med POLY_1271 + ny nøgle
"""
import os
import json
import inspect
from dotenv import load_dotenv

load_dotenv()

from py_clob_client_v2 import ClobClient, MarketOrderArgs, OrderType
from py_clob_client_v2.order_utils.model.side import Side

KEY = os.environ["POLYMARKET_PRIVATE_KEY"]
DEPOSIT_WALLET = os.environ.get("DEPOSIT_WALLET_ADDRESS", "")
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

if not DEPOSIT_WALLET:
    print("❌ DEPOSIT_WALLET_ADDRESS er ikke sat i .env!")
    exit(1)

# ─── FASE 1: Se kildekode for create_api_key / create_or_derive ────────────
print("=" * 60)
print("FASE 1: Kildekode-inspektion")
print("=" * 60)

plain_tmp = ClobClient(host=HOST, chain_id=CHAIN_ID, key=KEY)

for method_name in ["create_api_key", "create_or_derive_api_key", "derive_api_key", "delete_api_key"]:
    method = getattr(plain_tmp, method_name, None)
    if method:
        print(f"\n--- {method_name} ---")
        try:
            src = inspect.getsource(method)
            print(src[:2000])
        except Exception as e:
            print(f"  (kunne ikke hente kildekode: {e})")
    else:
        print(f"\n--- {method_name}: IKKE FUNDET ---")

# ─── FASE 2: Slet gammel nøgle, opret ny med plain EOA-klient ──────────────
print("\n" + "=" * 60)
print("FASE 2: Nulstil API-nøgle")
print("=" * 60)

# Opret plain EOA-klient og hent credentials
print(f"EOA-adresse: {plain_tmp.get_address()}")

# Prøv at slette eksisterende nøgle
delete_method = getattr(plain_tmp, "delete_api_key", None)
if delete_method:
    print("Sletter eksisterende API-nøgle...")
    creds_for_delete = plain_tmp.derive_api_key()
    plain_tmp.set_api_creds(creds_for_delete)
    try:
        result = plain_tmp.delete_api_key()
        print(f"Slettet: {result}")
    except Exception as e:
        print(f"Sletning fejlede (kan fortsætte): {e}")
else:
    print("delete_api_key ikke tilgængelig i denne version")

# Opret ny nøgle med plain EOA-klient
print("\nOpretter ny API-nøgle med plain EOA-klient...")
try:
    new_creds = plain_tmp.create_api_key()
    print(f"Ny nøgle oprettet: {new_creds.api_key}")
except Exception as e:
    print(f"create_api_key fejlede: {e}")
    print("Forsøger derive_api_key i stedet...")
    new_creds = plain_tmp.derive_api_key()
    print(f"Derived nøgle: {new_creds.api_key}")

print(f"API-nøgle: {new_creds.api_key}")

# ─── FASE 3: Test POLY_1271-ordre med den nye EOA-nøgle ────────────────────
print("\n" + "=" * 60)
print("FASE 3: Test ordre")
print("=" * 60)

# Importer SignatureTypeV2 kun til ordre-klienten
try:
    from py_clob_client_v2 import SignatureTypeV2
    print("SignatureTypeV2 tilgængelig — bruger POLY_1271 klient")
    order_client = ClobClient(
        host=HOST,
        chain_id=CHAIN_ID,
        key=KEY,
        creds=new_creds,
        signature_type=SignatureTypeV2.POLY_1271,
        funder=DEPOSIT_WALLET,
    )
except (ImportError, AttributeError) as e:
    print(f"POLY_1271 ikke tilgængelig ({e}) — bruger plain EOA klient")
    order_client = ClobClient(
        host=HOST,
        chain_id=CHAIN_ID,
        key=KEY,
        creds=new_creds,
    )

print(f"Order klient EOA: {order_client.get_address()}")
print(f"Deposit wallet:   {DEPOSIT_WALLET}")

# Token fra aktivt marked (Bobe2's seneste trade)
TOKEN_ID = "77911208241982327373495855644935587349201177208106713081551029073015187679590"

ts = order_client.get_tick_size(TOKEN_ID)
print(f"tick_size: {ts}")

print("\n--- TEST: FOK BUY 0.01 USDC ---")
order_args = MarketOrderArgs(
    token_id=TOKEN_ID,
    amount=0.01,
    side=Side.BUY,
    order_type=OrderType.FOK,
)
try:
    resp = order_client.create_and_post_market_order(
        order_args=order_args,
        order_type=OrderType.FOK,
    )
    print(f"Svar: {json.dumps(resp, indent=2, default=str)}")
    if resp.get("status") == "matched":
        print("✅ ORDRE FYLDT!")
    elif resp.get("error"):
        print(f"❌ FEJL: {resp['error']}")
    else:
        print(f"ℹ️  Status: {resp.get('status', 'ukendt')}")
except Exception as e:
    print(f"Exception: {type(e).__name__}: {e}")

print("\nFærdig.")
