"""Order-flow signal computation -- no network calls."""
from types import SimpleNamespace

from core.orderflow import compute_imbalance, compute_microprice


def make_book(bids, asks):
    return SimpleNamespace(
        bids=[SimpleNamespace(price=str(p), size=str(s)) for p, s in bids],
        asks=[SimpleNamespace(price=str(p), size=str(s)) for p, s in asks],
    )


def test_imbalance_positive_when_more_bid_depth():
    book = make_book(bids=[(0.45, 10)], asks=[(0.55, 2)])
    assert compute_imbalance(book) > 0


def test_imbalance_negative_when_more_ask_depth():
    book = make_book(bids=[(0.45, 2)], asks=[(0.55, 10)])
    assert compute_imbalance(book) < 0


def test_imbalance_zero_when_balanced():
    book = make_book(bids=[(0.45, 5)], asks=[(0.55, 5)])
    assert compute_imbalance(book) == 0.0


def test_imbalance_zero_when_book_empty():
    book = make_book(bids=[], asks=[])
    assert compute_imbalance(book) == 0.0


def test_imbalance_exact_value():
    # bid_depth=8, ask_depth=2 -> (8-2)/10 = 0.6
    book = make_book(bids=[(0.45, 8)], asks=[(0.55, 2)])
    assert compute_imbalance(book) == 0.6


def test_microprice_leans_toward_heavier_bid_side():
    # Heavy bid side (10) should pull microprice toward the ask price.
    book = make_book(bids=[(0.40, 10)], asks=[(0.60, 1)])
    micro = compute_microprice(book)
    midpoint = (0.40 + 0.60) / 2
    assert micro > midpoint


def test_microprice_leans_toward_heavier_ask_side():
    book = make_book(bids=[(0.40, 1)], asks=[(0.60, 10)])
    micro = compute_microprice(book)
    midpoint = (0.40 + 0.60) / 2
    assert micro < midpoint


def test_microprice_none_when_one_side_missing():
    assert compute_microprice(make_book(bids=[], asks=[(0.6, 1)])) is None
    assert compute_microprice(make_book(bids=[(0.4, 1)], asks=[])) is None
