"""Market-maker v2 (real-time rewrite): correlated-event inventory cap
and fill logic -- no network calls."""
from bots.market_maker_bot import MMState, Position, check_fill, event_inventory


def make_state(positions: dict) -> MMState:
    return MMState(positions=positions)


def test_event_inventory_sums_across_correlated_markets():
    state = make_state({
        "t1": Position(market_id="m1", outcome="Up", event_key="2026-06-25T16:00:00+00:00", inventory=3.0),
        "t2": Position(market_id="m2", outcome="Up", event_key="2026-06-25T16:00:00+00:00", inventory=2.0),
        "t3": Position(market_id="m3", outcome="Up", event_key="2026-06-25T17:00:00+00:00", inventory=5.0),
    })
    assert event_inventory(state, "2026-06-25T16:00:00+00:00") == 5.0
    assert event_inventory(state, "2026-06-25T17:00:00+00:00") == 5.0
    assert event_inventory(state, None) == 0.0


def test_event_inventory_excludes_given_token():
    state = make_state({
        "t1": Position(market_id="m1", outcome="Up", event_key="e1", inventory=3.0),
        "t2": Position(market_id="m2", outcome="Up", event_key="e1", inventory=2.0),
    })
    assert event_inventory(state, "e1", exclude_token_id="t1") == 2.0


def test_check_fill_blocks_buy_once_event_cap_reached():
    pos = Position(market_id="m1", outcome="Up", event_key="e1", inventory=0.0,
                   last_bid=0.45, last_ask=0.55)

    # Other markets in this event already hold 5.0 -- at cap.
    fill = check_fill(pos, live_bid=0.5, live_ask=0.40, our_bid=0.45, our_ask=0.55,
                       quote_size=1.0, max_per_event=5.0, event_inventory_elsewhere=5.0)

    assert fill is None
    assert pos.inventory == 0.0  # unchanged -- buy was refused


def test_check_fill_allows_buy_under_event_cap():
    pos = Position(market_id="m1", outcome="Up", event_key="e1", inventory=0.0,
                   last_bid=0.45, last_ask=0.55)

    fill = check_fill(pos, live_bid=0.5, live_ask=0.40, our_bid=0.45, our_ask=0.55,
                       quote_size=1.0, max_per_event=5.0, event_inventory_elsewhere=2.0)

    assert fill is not None
    assert fill["side"] == "bid_filled"
    assert pos.inventory == 1.0
    assert pos.cash == -0.45


def test_check_fill_never_blocks_sell_even_at_event_cap():
    # Reducing inventory (ask_filled) must always be allowed, regardless of
    # the event cap -- exiting risk should never be refused.
    pos = Position(market_id="m1", outcome="Up", event_key="e1", inventory=2.0,
                   last_bid=0.55, last_ask=0.58)

    fill = check_fill(pos, live_bid=0.60, live_ask=0.70, our_bid=0.55, our_ask=0.58,
                       quote_size=1.0, max_per_event=0.0, event_inventory_elsewhere=10.0)

    assert fill is not None
    assert fill["side"] == "ask_filled"
    assert pos.inventory == 1.0


def test_check_fill_no_cross_returns_none():
    pos = Position(market_id="m1", outcome="Up", event_key="e1", inventory=0.0,
                   last_bid=0.45, last_ask=0.55)

    # Live market hasn't touched either side of our quote.
    fill = check_fill(pos, live_bid=0.48, live_ask=0.52, our_bid=0.45, our_ask=0.55,
                       quote_size=1.0, max_per_event=10.0, event_inventory_elsewhere=0.0)

    assert fill is None
    assert pos.inventory == 0.0


def test_check_fill_blocks_buy_once_max_inventory_reached():
    # Regression test for the Session 14 live bug: when the AS pricing
    # math degrades to a near-mid fallback quote (e.g. volatility spikes
    # near a price boundary), nothing in the pricing itself reliably
    # stops repeated fills -- this is the hard cap that doesn't depend on
    # the pricing model behaving correctly. live_bid kept below our_ask so
    # only the buy side is ever in play here.
    pos = Position(market_id="m1", outcome="Up", event_key="e1", inventory=10.0,
                   last_bid=0.01, last_ask=0.02)

    fill = check_fill(pos, live_bid=0.005, live_ask=0.01, our_bid=0.01, our_ask=0.02,
                       quote_size=1.0, max_per_event=100.0, event_inventory_elsewhere=0.0,
                       max_inventory=10.0)

    assert fill is None
    assert pos.inventory == 10.0  # unchanged -- buy was refused by the inventory cap


def test_check_fill_allows_buy_just_under_max_inventory():
    pos = Position(market_id="m1", outcome="Up", event_key="e1", inventory=9.0,
                   last_bid=0.01, last_ask=0.02)

    fill = check_fill(pos, live_bid=0.005, live_ask=0.01, our_bid=0.01, our_ask=0.02,
                       quote_size=1.0, max_per_event=100.0, event_inventory_elsewhere=0.0,
                       max_inventory=10.0)

    assert fill is not None
    assert pos.inventory == 10.0


def test_check_fill_ask_only_fills_up_to_held_inventory():
    pos = Position(market_id="m1", outcome="Up", event_key="e1", inventory=0.5,
                   last_bid=0.55, last_ask=0.58)

    fill = check_fill(pos, live_bid=0.60, live_ask=0.70, our_bid=0.55, our_ask=0.58,
                       quote_size=1.0, max_per_event=10.0, event_inventory_elsewhere=0.0)

    assert fill["size"] == 0.5  # capped at what we actually hold
    assert pos.inventory == 0.0
