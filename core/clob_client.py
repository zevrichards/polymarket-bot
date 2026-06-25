"""Thin wrapper around py-clob-client.

Read-only (Level 0, unauthenticated) access is all that's needed for paper
trading and market discovery -- Polymarket's CLOB allows unauthenticated
reads of order books, prices, and markets. Authenticated access (placing
real orders) is intentionally not wired up yet; see README for the deferred
live-trading phase.

py-clob-client's HTTP layer sets NO timeout on its requests (confirmed by
inspecting its source -- no "timeout" anywhere in http_helpers/). That means
a slow/degraded Polymarket endpoint can hang a call indefinitely, which
defeats core/scheduler.py's retry-on-failure design entirely: a hang never
raises an exception to be caught, it just stalls the process forever. Since
we can't pass a timeout into the library's calls directly, we set a
process-wide socket default here -- any socket opened anywhere in this
process (including inside py-clob-client) that doesn't specify its own
timeout falls back to this one.
"""
from __future__ import annotations

import socket
from functools import lru_cache

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams

CLOB_HOST = "https://clob.polymarket.com"
DEFAULT_SOCKET_TIMEOUT_SECONDS = 15

socket.setdefaulttimeout(DEFAULT_SOCKET_TIMEOUT_SECONDS)


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
