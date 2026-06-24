"""Thin wrapper around py-clob-client.

Read-only (Level 0, unauthenticated) access is all that's needed for paper
trading and market discovery -- Polymarket's CLOB allows unauthenticated
reads of order books, prices, and markets. Authenticated access (placing
real orders) is intentionally not wired up yet; see README for the deferred
live-trading phase.
"""
from __future__ import annotations

from functools import lru_cache

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams

CLOB_HOST = "https://clob.polymarket.com"


@lru_cache(maxsize=1)
def get_client() -> ClobClient:
    """Returns a shared read-only ClobClient instance."""
    return ClobClient(CLOB_HOST)


def get_order_book(token_id: str):
    return get_client().get_order_book(token_id)


def get_order_books(token_ids: list[str]):
    params = [BookParams(token_id=tid) for tid in token_ids]
    return get_client().get_order_books(params)


def get_midpoint(token_id: str) -> float:
    result = get_client().get_midpoint(token_id)
    return float(result["mid"]) if isinstance(result, dict) else float(result)


def get_price(token_id: str, side: str = "BUY") -> float:
    result = get_client().get_price(token_id, side=side)
    return float(result["price"]) if isinstance(result, dict) else float(result)
