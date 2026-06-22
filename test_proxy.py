"""
test_proxy.py — Test POLY_1271 med signer=deposit_wallet (partial monkey-patch)

Teorien: CLOB forventer order.signer=deposit_wallet for POLY_1271-ordrer,
ikke order.signer=EOA. SDK'en sætter altid signer=EOA (mulig bug).

Vi henter EOA credentials normalt, patcher så signer.address() → deposit_wallet
KUN til ordre-klienten. L2 HMAC bruger stadig EOA-secret.

Kør: docker compose cp test_proxy.py executor:/app/test_proxy.py && docker compose exec executor python3 test_proxy.py
"""
import os
import json
from dotenv import load_dotenv

load_dotenv()

from py_clob_client_v2 import ClobClient, MarketOrderArgs, OrderType, SignatureTypeV2
from py_clob_client_v2.order_utils.model.side import Side
from py_clob_client_v2.signer import Signer

KEY = os.environ["POLYMARKET_PRIVATE_KEY"]
DEPOSIT = os.environ["DEPOSIT_WALLET_ADDRESS"]
HOST = "https://clob.polymarket.com"
TOKEN = "77911208241982327373495855644935587349201177208106713081551029073015187679590"

# ── Trin 1: Hent EOA credentials normalt (ingen patch) ──────────────────────
print("Trin 1: Henter EOA credentials (normal L1 auth)")
plain = ClobClient(host=HOST, chain_id=137, key=KEY)
creds = plain.derive_api_key()
print(f"  api_key: {creds.api_key}")
print(f"  EOA:     {plain.signer.address()}")

# ── Trin 2: Patch signer.address() → deposit_wallet ─────────────────────────
print(f"\nTrin 2: Patcher signer.address() → {DEPOSIT[:20]}...")
_original_address = Signer.address
Signer.address = lambda self: DEPOSIT

try:
    # ── Trin 3: Opret ordre-klient med patchet signer ────────────────────────
    client = ClobClient(
        host=HOST,
        chain_id=137,
        key=KEY,
        creds=creds,
        signature_type=SignatureTypeV2.POLY_1271,
        funder=DEPOSIT,
    )

    args = MarketOrderArgs(
        token_id=TOKEN, amount=0.01, side=Side.BUY, order_type=OrderType.FOK
    )
    order = client.create_market_order(args)

    print(f"\nOrdre bygget:")
    print(f"  signatureType: {order.signatureType}")
    print(f"  maker:         {order.maker}")
    print(f"  signer:        {order.signer}  ← skal være deposit wallet")
    print(f"  POLY_ADDRESS:  {client.signer.address()}  ← L2 header")

    print(f"\nPoster ordre...")
    try:
        resp = client.create_and_post_market_order(args, order_type=OrderType.FOK)
        print(f"Svar: {json.dumps(resp, indent=2, default=str)}")
        if resp.get("status") == "matched":
            print("\n✅ ORDRE FYLDT!")
        elif resp.get("error"):
            print(f"\n❌ Fejl: {resp['error']}")
        else:
            print(f"\nℹ️  Status: {resp.get('status', 'ukendt')}")
    except Exception as e:
        print(f"❌ Exception: {e}")

finally:
    # Altid gendan original
    Signer.address = _original_address
    print("\nSigner.address gendannet.")
