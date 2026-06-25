"""Hits Polymarket's real public Gamma API -- no auth required, no funds at risk."""
from core.markets import fetch_btc_markets


def test_fetch_btc_markets_returns_well_formed_results():
    btc_markets = fetch_btc_markets(max_pages=5)

    assert isinstance(btc_markets, list)
    for market in btc_markets:
        assert market.slug
        assert len(market.outcomes) == len(market.token_ids) == 2
        seconds_left = market.seconds_to_resolution()
        assert seconds_left is None or seconds_left > 0


def test_results_are_sorted_soonest_first():
    # Compare end_date directly rather than calling seconds_to_resolution()
    # per item -- that method stamps `now` on each call, so two calls a
    # microsecond apart can disagree by a few microseconds and make an
    # already-correctly-sorted list look unsorted.
    btc_markets = fetch_btc_markets(max_pages=5)
    end_dates = [m.end_date for m in btc_markets if m.end_date is not None]
    assert end_dates == sorted(end_dates)
