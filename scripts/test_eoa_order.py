"""Tests EOA-mode order placement (sig_type=0, no proxy wallet).

maker = signer = EOA (MetaMask address). Funds must be in the MetaMask wallet
as pUSD/USDC on Polygon, not in the Polymarket proxy wallet.

This is the simplest possible V2 auth flow -- if this fails, it's a funds issue,
not an API/signature issue.

Run:
    .venv/Scripts/python.exe -m scripts.test_eoa_order
"""
from __future__ import annotations

from pathlib import Path
from dotenv import load_dotenv
import os

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, OrderArgs, OrderType
from core.markets import fetch_btc_markets

# No funder, no proxy -- pure EOA mode
client = ClobClient(
    "https://clob.polymarket.com",
    chain_id=137,
    key=os.environ["POLY_PRIVATE_KEY"],
    creds=ApiCreds(
        api_key=os.environ["POLY_API_KEY"],
        api_secret=os.environ["POLY_API_SECRET"],
        api_passphrase=os.environ["POLY_API_PASSPHRASE"],
    ),
    signature_type=0,  # EOA -- maker=signer=MetaMask address
)

print(f"EOA / Maker : {client.get_address()}")
print()

markets = fetch_btc_markets(horizon_hours=1)
if not markets:
    print("No BTC markets found")
    raise SystemExit(1)

market = markets[0]
up_token = market.token_ids[market.outcomes.index("Up")] if "Up" in market.outcomes else market.token_ids[0]
print(f"Market : {market.slug}")
print(f"Token  : {up_token[:20]}...")
print()

order_args = OrderArgs(
    token_id=up_token,
    price=0.01,
    size=1,   # tiny -- $0.01 total, well within $0.97 MetaMask balance
    side="BUY",
)
print("Placing EOA-mode limit order (price=0.01, size=1, sig_type=0)...")
print()

signed = client.create_order(order_args)
resp = client.post_order(signed, OrderType.GTC)
print("Response:", resp)
print()

order_id = resp.get("orderID") if resp else None
status = resp.get("status") if resp else None

if status in ("live", "open", "matched", "delayed"):
    print(f"ORDER ACCEPTED (status={status!r})")
    print("Cancelling...")
    print(client.cancel(order_id))
elif "insufficient" in str(resp).lower() or "balance" in str(resp).lower():
    print("Rejected for insufficient funds -- API/sig flow works but need pUSD in EOA")
else:
    print(f"status={status!r}")
