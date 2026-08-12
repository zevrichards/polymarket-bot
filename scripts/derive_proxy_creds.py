"""Derives API credentials bound to the proxy (deposit) wallet using POLY_1271.

The Python SDK bug: create_level_1_headers always puts the EOA address in both
the POLY_ADDRESS header and the ClobAuth EIP-712 struct, so keys always bind to
the EOA rather than the proxy wallet.

The fix: build the L1 headers manually with proxy_address in the ClobAuth struct.
The EOA signs the message; the CLOB calls isValidSignature() on the proxy contract
to verify the EOA is authorized.

Run once after proxy contract deployment:
    .venv/Scripts/python.exe -m scripts.derive_proxy_creds
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import os

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.signing.eip712 import get_clob_auth_domain
from py_clob_client_v2.signing.model import ClobAuth
from py_clob_client_v2.signer import Signer
from py_clob_client_v2.endpoints import CREATE_API_KEY, DERIVE_API_KEY
from py_clob_client_v2.clob_types import ApiCreds
from py_order_utils.utils import prepend_zx
from eth_utils import keccak

PROXY = "0x3BD8fe5882a087638DbE07Ac3CAD86b1B9AbE825"
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


def _sign_clob_auth_proxy(signer: Signer, proxy_address: str, timestamp: int, nonce: int) -> str:
    """Like sign_clob_auth_message but embeds proxy_address instead of EOA."""
    clob_auth_msg = ClobAuth(
        address=proxy_address,   # <-- proxy wallet, not EOA
        timestamp=str(timestamp),
        nonce=nonce,
        message="This message attests that I control the given wallet",
    )
    auth_struct_hash = prepend_zx(
        keccak(clob_auth_msg.signable_bytes(get_clob_auth_domain(CHAIN_ID))).hex()
    )
    return prepend_zx(signer.sign(auth_struct_hash))


def _proxy_l1_headers(signer: Signer, proxy_address: str) -> dict:
    ts = int(datetime.now().timestamp())
    sig = _sign_clob_auth_proxy(signer, proxy_address, ts, 0)
    return {
        "POLY_ADDRESS": proxy_address,   # proxy wallet, not EOA
        "POLY_SIGNATURE": sig,
        "POLY_TIMESTAMP": str(ts),
        "POLY_NONCE": "0",
    }


# Build a client just for the signer + HTTP helpers
client = ClobClient(HOST, chain_id=CHAIN_ID, key=os.environ["POLY_PRIVATE_KEY"])

print(f"EOA     : {client.get_address()}")
print(f"Proxy   : {PROXY}")
print()

# Patch: override _l1_headers on this instance so create_api_key uses proxy headers
import types

def _patched_l1_headers(self, nonce=None):
    return _proxy_l1_headers(self.signer, PROXY)

client._l1_headers = types.MethodType(_patched_l1_headers, client)

print("Attempting to create proxy-bound API key...")
try:
    resp = client._post(f"{HOST}{CREATE_API_KEY}", headers=client._l1_headers())
    creds = ApiCreds(
        api_key=resp["apiKey"],
        api_secret=resp["secret"],
        api_passphrase=resp["passphrase"],
    )
    print("create_api_key succeeded")
except Exception as e:
    print(f"create_api_key failed ({e}), trying derive...")
    resp = client._get(f"{HOST}{DERIVE_API_KEY}", headers=client._l1_headers())
    creds = ApiCreds(
        api_key=resp["apiKey"],
        api_secret=resp["secret"],
        api_passphrase=resp["passphrase"],
    )
    print("derive_api_key succeeded")

# Write to .env without printing values
env_path = ROOT / ".env"
lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
found = set()
updated = []
for line in lines:
    k = line.split("=")[0].strip()
    if k == "POLY_API_KEY":
        updated.append(f"POLY_API_KEY={creds.api_key}\n"); found.add(k)
    elif k == "POLY_API_SECRET":
        updated.append(f"POLY_API_SECRET={creds.api_secret}\n"); found.add(k)
    elif k == "POLY_API_PASSPHRASE":
        updated.append(f"POLY_API_PASSPHRASE={creds.api_passphrase}\n"); found.add(k)
    else:
        updated.append(line)

for k in {"POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE"} - found:
    if k == "POLY_API_KEY":      updated.append(f"POLY_API_KEY={creds.api_key}\n")
    elif k == "POLY_API_SECRET": updated.append(f"POLY_API_SECRET={creds.api_secret}\n")
    else:                        updated.append(f"POLY_API_PASSPHRASE={creds.api_passphrase}\n")

env_path.write_text("".join(updated), encoding="utf-8")
print(f".env updated ({env_path})")
print()
print("Now run: .venv/Scripts/python.exe -m scripts.test_live_order")
