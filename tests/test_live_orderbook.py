"""LiveOrderBook message parsing -- exercises _on_message directly with
real-shaped fixture payloads (captured from the live WebSocket), no actual
network connection."""
import json
import time

import pytest

from core.live_orderbook import LiveOrderBook


def test_book_event_sets_best_bid_ask():
    book = LiveOrderBook()
    message = json.dumps([{
        "event_type": "book",
        "asset_id": "t1",
        "market": "0xabc",
        "bids": [{"price": "0.45", "size": "100"}, {"price": "0.40", "size": "50"}],
        "asks": [{"price": "0.55", "size": "100"}, {"price": "0.60", "size": "50"}],
        "timestamp": "1700000000000",
        "hash": "deadbeef",
    }])

    book._on_message(None, message)

    bid, ask = book.best_bid_ask("t1")
    assert bid == 0.45
    assert ask == 0.55


def test_price_change_event_updates_best_bid_ask():
    book = LiveOrderBook()
    message = json.dumps({
        "event_type": "price_change",
        "market": "0xabc",
        "price_changes": [
            {"asset_id": "t1", "price": "0.50", "size": "10", "side": "BUY",
             "best_bid": "0.48", "best_ask": "0.52"},
        ],
        "timestamp": "1700000000000",
    })

    book._on_message(None, message)

    bid, ask = book.best_bid_ask("t1")
    assert bid == 0.48
    assert ask == 0.52


def test_price_change_with_multiple_assets_updates_each_independently():
    book = LiveOrderBook()
    message = json.dumps({
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": "t1", "best_bid": "0.30", "best_ask": "0.35"},
            {"asset_id": "t2", "best_bid": "0.65", "best_ask": "0.70"},
        ],
        "timestamp": "1700000000000",
    })

    book._on_message(None, message)

    assert book.best_bid_ask("t1") == (0.30, 0.35)
    assert book.best_bid_ask("t2") == (0.65, 0.70)


def test_unknown_token_returns_none():
    book = LiveOrderBook()
    assert book.best_bid_ask("never-seen") == (None, None)


def test_stale_data_returns_none():
    book = LiveOrderBook()
    with book._lock:
        book._best["t1"] = {"bid": 0.5, "ask": 0.6, "updated_at": time.time() - 100}

    assert book.best_bid_ask("t1", max_age_seconds=30.0) == (None, None)


def test_fresh_data_within_max_age_is_returned():
    book = LiveOrderBook()
    with book._lock:
        book._best["t1"] = {"bid": 0.5, "ask": 0.6, "updated_at": time.time() - 5}

    assert book.best_bid_ask("t1", max_age_seconds=30.0) == (0.5, 0.6)


def test_malformed_json_is_ignored_not_raised():
    book = LiveOrderBook()
    book._on_message(None, "not valid json{{{")  # should not raise
    assert book.best_bid_ask("t1") == (None, None)


def test_book_event_missing_bids_or_asks_is_skipped():
    book = LiveOrderBook()
    message = json.dumps([{
        "event_type": "book", "asset_id": "t1",
        "bids": [], "asks": [{"price": "0.5", "size": "10"}],
    }])
    book._on_message(None, message)
    assert book.best_bid_ask("t1") == (None, None)


def test_subscribe_tracks_token_ids_without_connection():
    book = LiveOrderBook()
    book.subscribe(["t1", "t2"])
    assert book._subscribed == {"t1", "t2"}

    book.subscribe(["t2", "t3"])  # t2 already subscribed, t3 new
    assert book._subscribed == {"t1", "t2", "t3"}


def test_unsubscribe_clears_tracked_state():
    book = LiveOrderBook()
    book.subscribe(["t1"])
    with book._lock:
        book._best["t1"] = {"bid": 0.5, "ask": 0.6, "updated_at": time.time()}

    book.unsubscribe(["t1"])

    assert "t1" not in book._subscribed
    assert book.best_bid_ask("t1") == (None, None)


def test_book_event_populates_depth():
    book = LiveOrderBook()
    message = json.dumps([{
        "event_type": "book",
        "asset_id": "t1",
        "bids": [{"price": "0.45", "size": "100"}, {"price": "0.40", "size": "50"}],
        "asks": [{"price": "0.55", "size": "100"}, {"price": "0.60", "size": "50"}],
    }])

    book._on_message(None, message)

    # bids: 0.45*100 + 0.40*50 = 65.0 | asks: 0.55*100 + 0.60*50 = 85.0
    assert book.depth_usd("t1") == pytest.approx(150.0)


def test_depth_usd_none_when_no_book_event_received():
    book = LiveOrderBook()
    assert book.depth_usd("never-seen") is None


def test_depth_usd_not_updated_by_price_change_events():
    # price_change deltas don't carry full depth -- depth should remain
    # whatever the last "book" snapshot said, not be wiped or guessed at.
    book = LiveOrderBook()
    book_message = json.dumps([{
        "event_type": "book", "asset_id": "t1",
        "bids": [{"price": "0.45", "size": "100"}],
        "asks": [{"price": "0.55", "size": "100"}],
    }])
    book._on_message(None, book_message)

    price_change_message = json.dumps({
        "event_type": "price_change",
        "price_changes": [{"asset_id": "t1", "best_bid": "0.46", "best_ask": "0.54"}],
    })
    book._on_message(None, price_change_message)

    assert book.depth_usd("t1") == pytest.approx(0.45 * 100 + 0.55 * 100)
    assert book.best_bid_ask("t1") == (0.46, 0.54)  # best_bid_ask DOES update on price_change


def test_depth_usd_stale_returns_none():
    book = LiveOrderBook()
    with book._lock:
        book._depth["t1"] = {"bids": [(0.45, 100)], "asks": [(0.55, 100)], "updated_at": time.time() - 120}

    assert book.depth_usd("t1", max_age_seconds=60.0) is None


def test_get_book_returns_paper_broker_compatible_shape():
    book = LiveOrderBook()
    message = json.dumps([{
        "event_type": "book", "asset_id": "t1",
        "bids": [{"price": "0.45", "size": "100"}],
        "asks": [{"price": "0.55", "size": "100"}],
    }])
    book._on_message(None, message)

    result = book.get_book("t1")

    assert float(result.bids[0].price) == 0.45
    assert float(result.bids[0].size) == 100
    assert float(result.asks[0].price) == 0.55


def test_get_book_none_when_no_snapshot():
    book = LiveOrderBook()
    assert book.get_book("never-seen") is None


def test_unsubscribe_clears_depth_too():
    book = LiveOrderBook()
    with book._lock:
        book._depth["t1"] = {"bids": [(0.45, 100)], "asks": [(0.55, 100)], "updated_at": time.time()}
    book.subscribe(["t1"])

    book.unsubscribe(["t1"])

    assert book.depth_usd("t1") is None
