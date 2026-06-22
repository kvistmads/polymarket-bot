"""
test_proxy.py — Nulstil API-nøgle + test POLY_1271 + POLY_GNOSIS_SAFE

Kør: docker compose cp test_proxy.py executor:/app/test_proxy.py && docker compose exec executor python3 test_proxy.py
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


def run_test(label: str, sig_type: SignatureTypeV2, creds) -> None:
    print(f"\n{'='*55}")
    print(f"TEST: {label} (signatureType={int(sig_type)})")
    print("=" * 55)
    client = ClobClient(
        host=HOST,
        chain_id=137,
        key=KEY,
        creds=creds,
        signature_type=sig_type,
        funder=DEPOSIT,
    )
    args = MarketOrderArgs(
        token_id=TOKEN, amount=0.01, side=Side.BUY, order_type=OrderType.FOK
    )
    order = client.create_market_order(args)
    print(f"  maker:   {order.maker}")
    print(f"  signer:  {order.signer}")
    print(f"  api_key: {creds.api_key}")
    try:
        resp = client.create_and_post_market_order(args, order_type=OrderType.FOK)
        print(f"  Svar: {json.dumps(resp, default=str)}")
        if resp.get("status") == "matched":
            print("  ✅ ORDRE FYLDT!")
        elif resp.get("error"):
            print(f"  ❌ Fejl: {resp['error']}")
        else:
            print(f"  ℹ️  Status: {resp.get('status', 'ukendt')}")
    except Exception as e:
        print(f"  ❌ Exception: {e}")


# ── Trin 1: Slet eksisterende API-nøgle ─────────────────────────────────────
print("Trin 1: Slet eksisterende API-nøgle")
plain = ClobClient(host=HOST, chain_id=137, key=KEY)
old_creds = plain.derive_api_key()
plain.set_api_creds(old_creds)
print(f"  Sletter nøgle: {old_creds.api_key}")
try:
    result = plain.delete_api_key()
    print(f"  Slettet: {result}")
except Exception as e:
    print(f"  Sletning fejlede: {e}")

# ── Trin 2: Opret FRISK nøgle med plain EOA-klient ──────────────────────────
print("\nTrin 2: Opret frisk API-nøgle (plain EOA-klient)")
try:
    fresh_creds = plain.create_api_key()
    print(f"  Ny nøgle: {fresh_creds.api_key}")
except Exception as e:
    print(f"  create_api_key fejlede ({e}) — bruger derive")
    fresh_creds = plain.derive_api_key()
    print(f"  Derived nøgle: {fresh_creds.api_key}")

# ── Trin 3: Test POLY_1271 med den nye nøgle ─────────────────────────────────
run_test("POLY_1271", SignatureTypeV2.POLY_1271, fresh_creds)

# ── Trin 4: Test POLY_GNOSIS_SAFE med den nye nøgle ─────────────────────────
run_test("POLY_GNOSIS_SAFE", SignatureTypeV2.POLY_GNOSIS_SAFE, fresh_creds)

print("\nFærdig.")
