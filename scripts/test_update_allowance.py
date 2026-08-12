"""Calls update_balance_allowance (the possible 'deposit wallet flow' step)
then immediately attempts a test order.

Run:
    .venv/Scripts/python.exe -m scripts.test_update_allowance
"""
from __future__ import annotations

from pathlib import Path
from dotenv import load_dotenv
import os

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, BalanceAllowanceParams, AssetType, OrderArgs, OrderType
from core.markets import fetch_btc_markets

PROXY = "0x3BD8fe5882a087638DbE07Ac3CAD86b1B9AbE825"

client = ClobClient(
    "https://clob.polymarket.com",
    chain_id=137,
    key=os.environ["POLY_PRIVATE_KEY"],
    creds=ApiCreds(
        api_key=os.environ["POLY_API_KEY"],
        api_secret=os.environ["POLY_API_SECRET"],
        api_passphrase=os.environ["POLY_API_PASSPHRASE"],
    ),
    signature_type=3,
    funder=PROXY,
)

print(f"EOA   : {client.get_address()}")
print(f"Proxy : {PROXY}")
print()

# Step 1: call update_balance_allowance -- possible deposit wallet registration
print("Calling update_balance_allowance (sig_type=3)...")
try:
    result = client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print("update_balance_allowance response:", result)
except Exception as e:
    print(f"update_balance_allowance error: {e}")
print()

# Step 2: try placing an order
print("Attempting order...")
markets = fetch_btc_markets(horizon_hours=1)
if not markets:
    print("No BTC markets found")
    raise SystemExit(1)

market = markets[0]
up_token = market.token_ids[market.outcomes.index("Up")] if "Up" in market.outcomes else market.token_ids[0]
print(f"Market : {market.slug}")
print()

order_args = OrderArgs(token_id=up_token, price=0.01, size=5, side="BUY")
signed = client.create_order(order_args)
resp = client.post_order(signed, OrderType.GTC)
print("Order response:", resp)

order_id = resp.get("orderID") if resp else None
status = resp.get("status") if resp else None
if status in ("live", "open", "matched", "delayed"):
    print(f"ORDER ACCEPTED (status={status!r}) -- cancelling...")
    print(client.cancel(order_id))
else:
    print(f"status={status!r}")
