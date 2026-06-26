"""Market-maker correlated-event inventory cap -- no network calls."""
from types import SimpleNamespace

from bots.market_maker_bot import MMState, Quote, check_fills, event_inventory


def make_state(quotes: dict) -> MMState:
    return MMState(quotes=quotes)


def test_event_inventory_sums_across_correlated_markets():
    state = make_state({
        "t1": Quote(bid_price=0.4, ask_price=0.5, inventory=3.0, event_key="2026-06-25T16:00:00+00:00"),
        "t2": Quote(bid_price=0.4, ask_price=0.5, inventory=2.0, event_key="2026-06-25T16:00:00+00:00"),
        "t3": Quote(bid_price=0.4, ask_price=0.5, inventory=5.0, event_key="2026-06-25T17:00:00+00:00"),
    })
    assert event_inventory(state, "2026-06-25T16:00:00+00:00") == 5.0
    assert event_inventory(state, "2026-06-25T17:00:00+00:00") == 5.0
    assert event_inventory(state, None) == 0.0


def test_event_inventory_excludes_given_token():
    state = make_state({
        "t1": Quote(bid_price=0.4, ask_price=0.5, inventory=3.0, event_key="e1"),
        "t2": Quote(bid_price=0.4, ask_price=0.5, inventory=2.0, event_key="e1"),
    })
    assert event_inventory(state, "e1", exclude_token_id="t1") == 2.0


def test_check_fills_blocks_buy_once_event_cap_reached():
    quote = Quote(bid_price=0.50, ask_price=0.55, inventory=0.0, event_key="e1")
    cfg = {"quote_size": 1.0, "max_inventory_per_event": 5.0}

    # Other markets in this event already hold 5.0 -- at cap.
    fill = check_fills("t1", quote, best_bid=0.5, best_ask=0.40, cfg=cfg, event_inventory_before_this_quote=5.0)

    assert fill is None
    assert quote.inventory == 0.0  # unchanged -- buy was refused


def test_check_fills_allows_buy_under_event_cap():
    quote = Quote(bid_price=0.50, ask_price=0.55, inventory=0.0, event_key="e1")
    cfg = {"quote_size": 1.0, "max_inventory_per_event": 5.0}

    fill = check_fills("t1", quote, best_bid=0.5, best_ask=0.40, cfg=cfg, event_inventory_before_this_quote=2.0)

    assert fill is not None
    assert fill["side"] == "bid_filled"
    assert quote.inventory == 1.0


def test_check_fills_never_blocks_sell_even_at_event_cap():
    # Reducing inventory (ask_filled) must always be allowed, regardless of
    # the event cap -- exiting risk should never be refused.
    quote = Quote(bid_price=0.50, ask_price=0.55, inventory=2.0, event_key="e1")
    cfg = {"quote_size": 1.0, "max_inventory_per_event": 0.0}  # cap already exceeded

    fill = check_fills("t1", quote, best_bid=0.60, best_ask=0.70, cfg=cfg, event_inventory_before_this_quote=10.0)

    assert fill is not None
    assert fill["side"] == "ask_filled"
    assert quote.inventory == 1.0


def test_check_fills_blocks_buy_when_book_signals_selling_pressure():
    quote = Quote(bid_price=0.50, ask_price=0.55, inventory=0.0)
    cfg = {"quote_size": 1.0, "min_imbalance_to_buy": -0.2}

    fill = check_fills("t1", quote, best_bid=0.5, best_ask=0.40, cfg=cfg, imbalance=-0.5)

    assert fill is None
    assert quote.inventory == 0.0


def test_check_fills_allows_buy_when_imbalance_is_fine():
    quote = Quote(bid_price=0.50, ask_price=0.55, inventory=0.0)
    cfg = {"quote_size": 1.0, "min_imbalance_to_buy": -0.2}

    fill = check_fills("t1", quote, best_bid=0.5, best_ask=0.40, cfg=cfg, imbalance=0.1)

    assert fill is not None
    assert fill["side"] == "bid_filled"


def test_check_fills_never_blocks_sell_regardless_of_imbalance():
    quote = Quote(bid_price=0.50, ask_price=0.55, inventory=2.0)
    cfg = {"quote_size": 1.0, "min_imbalance_to_buy": -0.2}

    # Extremely unfavorable imbalance shouldn't matter -- this is a sell.
    fill = check_fills("t1", quote, best_bid=0.60, best_ask=0.70, cfg=cfg, imbalance=-0.9)

    assert fill is not None
    assert fill["side"] == "ask_filled"
