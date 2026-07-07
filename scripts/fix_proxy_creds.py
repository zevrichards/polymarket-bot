"""Re-derives API credentials using the V2 client in the POLY_PROXY (sig_type=1)
context and updates .env in place.

Run once before switching to live mode:
    .venv/Scripts/python.exe -m scripts.fix_proxy_creds
"""
from __future__ import annotations

from pathlib import Path
from dotenv import load_dotenv
import os

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from py_clob_client_v2.client import ClobClient

PROXY = "0xa23d2F995FD9C03AAC2eBefa79795Df2365CA32D"

client = ClobClient(
    "https://clob.polymarket.com",
    chain_id=137,
    key=os.environ["POLY_PRIVATE_KEY"],
    signature_type=1,   # POLY_PROXY -- Polymarket's own proxy wallet
    funder=PROXY,
)

print(f"Signer : {client.get_address()}")
print(f"sig_type: 1 (POLY_PROXY)")
print()
print("Re-deriving API credentials in V2 POLY_PROXY context...")

creds = client.create_or_derive_api_key()
if not creds:
    print("ERROR: failed to derive credentials. Check your private key and network.")
    raise SystemExit(1)

print("Credentials derived OK")

# Update .env in place without printing the values
env_path = ROOT / ".env"
text = env_path.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)

keys_to_update = {"POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE"}
found = set()
updated = []
for line in lines:
    key = line.split("=")[0].strip()
    if key == "POLY_API_KEY":
        updated.append(f"POLY_API_KEY={creds.api_key}\n")
        found.add(key)
    elif key == "POLY_API_SECRET":
        updated.append(f"POLY_API_SECRET={creds.api_secret}\n")
        found.add(key)
    elif key == "POLY_API_PASSPHRASE":
        updated.append(f"POLY_API_PASSPHRASE={creds.api_passphrase}\n")
        found.add(key)
    else:
        updated.append(line)

# Append any keys not already in .env
for k in keys_to_update - found:
    if k == "POLY_API_KEY":
        updated.append(f"POLY_API_KEY={creds.api_key}\n")
    elif k == "POLY_API_SECRET":
        updated.append(f"POLY_API_SECRET={creds.api_secret}\n")
    elif k == "POLY_API_PASSPHRASE":
        updated.append(f"POLY_API_PASSPHRASE={creds.api_passphrase}\n")

env_path.write_text("".join(updated), encoding="utf-8")
print(f".env updated ({env_path})")
