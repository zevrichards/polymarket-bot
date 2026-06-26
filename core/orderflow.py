"""Order-flow signals computed from a live Polymarket order book.

These are leading indicators of short-term price pressure, used to decide
whether it's currently safe to take on inventory (buy) -- see
bots/market_maker_bot.py. The intent is to directly address the adverse
selection problem measured in BUILD_INTELLIGENCE_REPORT.md Session 7:
the bot's resting buy quotes were getting filled specifically when price
was about to keep moving against it. These signals try to detect that
situation from the book itself, before it costs us a fill.

Both functions take a py_clob_client OrderBookSummary (has .bids/.asks,
each a list of objects with .price/.size as strings).
"""
from __future__ import annotations


def book_depth(levels) -> float:
    return sum(float(level.size) for level in levels)


def compute_imbalance(book) -> float:
    """Returns a value in [-1, 1]: (bid_depth - ask_depth) / total_depth.

    Positive means more resting size on the bid side (buying pressure --
    price more likely to drift up from here, a reasonable time to buy).
    Negative means more resting size on the ask side (selling pressure --
    price more likely to drift down, a risky time to buy). Zero if the
    book is empty or perfectly balanced.
    """
    bid_depth = book_depth(book.bids)
    ask_depth = book_depth(book.asks)
    total = bid_depth + ask_depth
    if total == 0:
        return 0.0
    return (bid_depth - ask_depth) / total


def compute_microprice(book) -> float | None:
    """Volume-weighted "fair" price between best bid and best ask, leaning
    toward whichever side has heavier resting size (standard microprice
    formula: weight bid price by ask size and vice versa, so a large bid
    queue pulls the microprice up toward the ask, reflecting upward
    pressure). Returns None if either side of the book is empty.
    """
    if not book.bids or not book.asks:
        return None

    best_bid = max(book.bids, key=lambda level: float(level.price))
    best_ask = min(book.asks, key=lambda level: float(level.price))
    best_bid_price = float(best_bid.price)
    best_ask_price = float(best_ask.price)
    best_bid_size = float(best_bid.size)
    best_ask_size = float(best_ask.size)

    total_size = best_bid_size + best_ask_size
    if total_size == 0:
        return (best_bid_price + best_ask_price) / 2

    return (best_bid_price * best_ask_size + best_ask_price * best_bid_size) / total_size
