#!/usr/bin/env python3
"""Test ERC-7739 TypedDataSign signature for POLY_1271 - run inside executor container"""
import os
from eth_abi import encode
from eth_utils import keccak
from eth_account import Account
import httpx

RPC = "https://polygon-bor-rpc.publicnode.com"
DEPOSIT = "0x30959791af1099a2e7DC1aCd69fC82b9e7C50e51"
KEY = os.environ["POLYMARKET_PRIVATE_KEY"]
APP_DOM_SEP = bytes.fromhex("3264e159346253e26a64e00b69032db0e7d32f94628de3e6eecb50304d7af3d2")
CHAIN_ID = 137

def rpc(method, params):
    r = httpx.post(RPC, json={"jsonrpc":"2.0","method":method,"params":params,"id":1}, timeout=15)
    return r.json().get("result", "")

# Domain components
DOMAIN_TH = keccak(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
NAME_H = keccak(b"DepositWallet")
VER_H = keccak(b"1")

# Wallet domain separator
wallet_dom = keccak(encode(
    ["bytes32","bytes32","bytes32","uint256","address"],
    [DOMAIN_TH, NAME_H, VER_H, CHAIN_ID, DEPOSIT]
))
print(f"Wallet domain sep: 0x{wallet_dom.hex()}")

# TypedDataSign type (from bytecode fragments)
TDS_TYPE = b"TypedDataSign(bytes32 contents,string name,string version,uint256 chainId,address verifyingContract,bytes32 salt)"
TDS_TH = keccak(TDS_TYPE)
print(f"TDS typehash: 0x{TDS_TH.hex()}")

# Baseline: 65 bytes of direct signing still works?
test_hash = keccak(b"polymarket erc7739 test 2025")
sig_direct = Account._sign_hash(test_hash, KEY).signature
enc = encode(["bytes32","bytes"], [test_hash, sig_direct])
r = rpc("eth_call", [{"to": DEPOSIT, "data": "0x1626ba7e" + enc.hex()}, "latest"])
print(f"Baseline 65-byte direct:  {'OK' if '1626ba7e' in r else 'FAIL'} -> {r[:20]}")

# ERC-7739 test: TypedDataSign(bytes32 contents,...) with salt=0
struct_h = keccak(encode(
    ["bytes32","bytes32","bytes32","bytes32","uint256","address","bytes32"],
    [TDS_TH, test_hash, NAME_H, VER_H, CHAIN_ID, DEPOSIT, bytes(32)]
))
erc7739_hash = keccak(b"\x19\x01" + wallet_dom + struct_h)
sig97 = Account._sign_hash(erc7739_hash, KEY).signature + APP_DOM_SEP  # 97 bytes
enc97 = encode(["bytes32","bytes"], [test_hash, sig97])
r97 = rpc("eth_call", [{"to": DEPOSIT, "data": "0x1626ba7e" + enc97.hex()}, "latest"])
print(f"ERC-7739 97-byte (salt=0): {'OK' if '1626ba7e' in r97 else 'FAIL'} -> {r97[:20]}")

# Variant: salt = appDomainSeparator
struct_h2 = keccak(encode(
    ["bytes32","bytes32","bytes32","bytes32","uint256","address","bytes32"],
    [TDS_TH, test_hash, NAME_H, VER_H, CHAIN_ID, DEPOSIT, APP_DOM_SEP]
))
erc7739_hash2 = keccak(b"\x19\x01" + wallet_dom + struct_h2)
sig97b = Account._sign_hash(erc7739_hash2, KEY).signature + APP_DOM_SEP
enc97b = encode(["bytes32","bytes"], [test_hash, sig97b])
r97b = rpc("eth_call", [{"to": DEPOSIT, "data": "0x1626ba7e" + enc97b.hex()}, "latest"])
print(f"ERC-7739 97-byte (salt=appDomSep): {'OK' if '1626ba7e' in r97b else 'FAIL'} -> {r97b[:20]}")

# Variant: no salt field in TypedDataSign
TDS_TYPE2 = b"TypedDataSign(bytes32 contents,string name,string version,uint256 chainId,address verifyingContract)"
TDS_TH2 = keccak(TDS_TYPE2)
struct_h3 = keccak(encode(
    ["bytes32","bytes32","bytes32","bytes32","uint256","address"],
    [TDS_TH2, test_hash, NAME_H, VER_H, CHAIN_ID, DEPOSIT]
))
erc7739_hash3 = keccak(b"\x19\x01" + wallet_dom + struct_h3)
sig97c = Account._sign_hash(erc7739_hash3, KEY).signature + APP_DOM_SEP
enc97c = encode(["bytes32","bytes"], [test_hash, sig97c])
r97c = rpc("eth_call", [{"to": DEPOSIT, "data": "0x1626ba7e" + enc97c.hex()}, "latest"])
print(f"ERC-7739 97-byte (no salt): {'OK' if '1626ba7e' in r97c else 'FAIL'} -> {r97c[:20]}")
