"""One-time setup: generate Polymarket API credentials from your wallet
private key and write them to .env.

Run this once after you have:
  1. A MetaMask wallet connected to Polymarket
  2. USDC deposited into Polymarket from that wallet

Usage:
    .venv/Scripts/python.exe -m scripts.setup_credentials

You will be prompted for your private key (input is hidden). The key is
used only to sign the credential request to Polymarket's CLOB; it is NOT
sent anywhere else and is NOT stored by this script. After running, copy
the private key into .env yourself (POLY_PRIVATE_KEY=...).

The generated API key/secret/passphrase ARE written to .env automatically.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


def main() -> None:
    print("Polymarket API credential setup")
    print("=" * 40)
    print(f"This will generate API credentials and write them to: {ENV_PATH}")
    print()
    print("Enter your MetaMask wallet private key (input hidden).")
    print("This is NOT saved by this script. You add it to .env yourself afterwards.")
    print()

    private_key = getpass.getpass("Private key (hex, with or without 0x): ").strip()
    if not private_key:
        print("ERROR: No private key provided.")
        sys.exit(1)

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        print("ERROR: py-clob-client not installed. Run: pip install py-clob-client")
        sys.exit(1)

    print()
    print("Connecting to Polymarket CLOB...")
    client = ClobClient(CLOB_HOST, chain_id=CHAIN_ID, key=private_key)

    print("Generating API credentials (signing with your private key)...")
    try:
        creds = client.create_or_derive_api_creds()
    except Exception as exc:
        print(f"ERROR: Failed to generate credentials: {exc}")
        print()
        print("Common causes:")
        print("  - Wrong private key (does not match a wallet connected to Polymarket)")
        print("  - Wallet has never traded on Polymarket (try making one manual trade first)")
        sys.exit(1)

    if creds is None:
        print("ERROR: Received null credentials from Polymarket.")
        sys.exit(1)

    print()
    print("Credentials generated successfully.")
    print()

    # Read existing .env if it exists, to preserve any other entries
    existing_lines = []
    skip_keys = {"POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE"}
    if ENV_PATH.exists():
        with ENV_PATH.open(encoding="utf-8") as f:
            for line in f:
                key = line.split("=")[0].strip()
                if key not in skip_keys and key != "POLY_PRIVATE_KEY":
                    existing_lines.append(line.rstrip("\n"))

    new_lines = existing_lines + [
        f"POLY_API_KEY={creds.api_key}",
        f"POLY_API_SECRET={creds.api_secret}",
        f"POLY_API_PASSPHRASE={creds.api_passphrase}",
    ]

    with ENV_PATH.open("w", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + "\n")

    print(f"Wrote API credentials to {ENV_PATH}")
    print()
    print("NEXT STEP — add your private key to .env manually:")
    print(f"  Open {ENV_PATH} and add the line:")
    print("  POLY_PRIVATE_KEY=0x<your_private_key_here>")
    print()
    print("Keep .env secret. It is gitignored and must never be committed.")


if __name__ == "__main__":
    main()
