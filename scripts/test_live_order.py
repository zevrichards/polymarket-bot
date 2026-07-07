"""Places a tiny non-marketable GTC limit order and immediately cancels it.

This verifies the V2 CLOB client can sign and submit orders using the proxy
wallet (signature_type=3, POLY_1271). If accepted, the bot is ready for live mode.

The order is priced at $0.01 -- far below any real BTC market ask -- so it
will sit on the book unfilled. It is cancelled immediately after confirmation.

Run:
    .venv\Scripts\python.exe -m scripts.test_live_order
"""
from __future__ import annotations

from pathlib import Path
from dotenv import load_dotenv
import os

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, OrderArgs, OrderPayload, OrderType
from core.markets import fetch_btc_markets

PROXY = "0xa23d2F995FD9C03AAC2eBefa79795Df2365CA32D"

client = ClobClient(
    "https://clob.polymarket.com",
    chain_id=137,
    key=os.environ["POLY_PRIVATE_KEY"],
    creds=ApiCreds(
        api_key=os.environ["POLY_API_KEY"],
        api_secret=os.environ["POLY_API_SECRET"],
        api_passphrase=os.environ["POLY_API_PASSPHRASE"],
    ),
    signature_type=3,   # POLY_1271 -- deposit wallet flow
    funder=PROXY,
)

print(f"Signer  : {client.get_address()}")
print()

# Find a live BTC market
markets = fetch_btc_markets(horizon_hours=1)
if not markets:
    print("No BTC markets found right now -- try again in a few minutes")
    raise SystemExit(1)

market = markets[0]
up_token = market.token_ids[market.outcomes.index("Up")] if "Up" in market.outcomes else market.token_ids[0]
print(f"Market  : {market.slug}")
print(f"Token   : {up_token[:20]}...")
print()

# Non-marketable limit order: bid $0.01 for 5 shares = $0.05 total
# Real ask is ~$0.50+, so this will never fill
order_args = OrderArgs(
    token_id=up_token,
    price=0.01,   # far below market -- will not fill
    size=5,
    side="BUY",
)
print("Placing non-marketable GTC limit order (price=0.01, size=5)...")
print()

signed = client.create_order(order_args)
resp = client.post_order(signed, OrderType.GTC)
print("Response:", resp)
print()

order_id = resp.get("orderID") if resp else None
status = resp.get("status") if resp else None

if status in ("live", "open", "matched", "delayed"):
    print(f"ORDER ACCEPTED (status={status!r}) -- proxy wallet funds ARE accessible!")
    print("Cancelling now...")
    cancel_resp = client.cancel_order(OrderPayload(orderID=order_id))
    print("Cancel response:", cancel_resp)
elif status == "unmatched":
    print("Order unmatched -- may indicate insufficient funds or approval issue")
else:
    print(f"Unexpected response status={status!r} -- see full response above")
