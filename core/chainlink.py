"""Read the Chainlink BTC/USD price feed on Polygon.

Used by Bot 2 as an independent sanity check before trading a directional
candidate -- if Polymarket's implied price and Chainlink's spot price
disagree by more than the configured threshold, skip the trade rather than
assume the market is right.
"""
from __future__ import annotations

import os
from functools import lru_cache

from web3 import Web3

# Minimal Chainlink AggregatorV3Interface ABI -- only the two read methods we need.
AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

DEFAULT_RPC_URL = "https://polygon-bor-rpc.publicnode.com"


@lru_cache(maxsize=1)
def _web3() -> Web3:
    rpc_url = os.environ.get("POLYGON_RPC_URL", DEFAULT_RPC_URL)
    return Web3(Web3.HTTPProvider(rpc_url))


def get_btc_usd_price_and_age(feed_address: str) -> tuple[float, float]:
    """Returns (price, seconds_since_last_update) for the Chainlink feed."""
    import time

    w3 = _web3()
    contract = w3.eth.contract(address=Web3.to_checksum_address(feed_address), abi=AGGREGATOR_ABI)
    decimals = contract.functions.decimals().call()
    _, answer, _, updated_at, _ = contract.functions.latestRoundData().call()
    if answer <= 0:
        raise ValueError(f"chainlink feed returned non-positive answer: {answer}")
    price = answer / (10**decimals)
    age_seconds = time.time() - updated_at
    return price, age_seconds


def relative_change_pct(old_price: float, new_price: float) -> float:
    if old_price == 0:
        raise ValueError("old_price cannot be zero")
    return abs(new_price - old_price) / old_price
