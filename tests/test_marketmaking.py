"""Avellaneda-Stoikov pricing math -- pure functions, no network."""
import pytest

from core.marketmaking import compute_quotes, estimate_contract_volatility, optimal_spread, reservation_price


def test_estimate_contract_volatility_zero_for_constant_price():
    assert estimate_contract_volatility([0.50] * 10) == 0.0


def test_estimate_contract_volatility_zero_with_insufficient_data():
    assert estimate_contract_volatility([]) == 0.0
    assert estimate_contract_volatility([0.5]) == 0.0
    assert estimate_contract_volatility([0.5, 0.51]) == 0.0


def test_estimate_contract_volatility_does_not_blow_up_near_zero_boundary():
    # Regression test for the Session 14 live bug: a price frozen near a
    # $0/$1 boundary with tiny absolute jitter must NOT produce a huge
    # volatility estimate the way log-return volatility did (0.3-0.58 on
    # a contract sitting at $0.01). Same-sized absolute jitter should give
    # roughly the same volatility near a boundary as away from one.
    near_boundary = estimate_contract_volatility([0.01, 0.015, 0.01, 0.012, 0.01, 0.013])
    mid_range = estimate_contract_volatility([0.50, 0.505, 0.50, 0.502, 0.50, 0.503])
    assert near_boundary == pytest.approx(mid_range, abs=1e-9)
    assert near_boundary < 0.01  # small absolute jitter -> small volatility, not 0.3+


def test_reservation_price_equals_mid_with_zero_inventory():
    assert reservation_price(mid=0.50, inventory=0.0, gamma=0.1, sigma=0.01, time_remaining=60) == 0.50


def test_reservation_price_drops_when_long():
    r = reservation_price(mid=0.50, inventory=5.0, gamma=0.1, sigma=0.01, time_remaining=60)
    assert r < 0.50


def test_reservation_price_rises_when_short():
    r = reservation_price(mid=0.50, inventory=-5.0, gamma=0.1, sigma=0.01, time_remaining=60)
    assert r > 0.50


def test_reservation_price_skew_grows_with_inventory_size():
    small = reservation_price(mid=0.50, inventory=1.0, gamma=0.1, sigma=0.01, time_remaining=60)
    large = reservation_price(mid=0.50, inventory=8.0, gamma=0.1, sigma=0.01, time_remaining=60)
    assert (0.50 - large) > (0.50 - small)


def test_reservation_price_at_zero_time_remaining_is_mid():
    assert reservation_price(mid=0.50, inventory=5.0, gamma=0.1, sigma=0.01, time_remaining=0) == 0.50


def test_optimal_spread_widens_with_more_time_remaining():
    near = optimal_spread(gamma=0.1, sigma=0.01, time_remaining=10, k=1.5)
    far = optimal_spread(gamma=0.1, sigma=0.01, time_remaining=200, k=1.5)
    assert far > near


def test_optimal_spread_widens_with_higher_volatility():
    low_vol = optimal_spread(gamma=0.1, sigma=0.005, time_remaining=60, k=1.5)
    high_vol = optimal_spread(gamma=0.1, sigma=0.02, time_remaining=60, k=1.5)
    assert high_vol > low_vol


def test_optimal_spread_zero_at_zero_time_remaining():
    assert optimal_spread(gamma=0.1, sigma=0.01, time_remaining=0, k=1.5) == 0.0


def test_compute_quotes_symmetric_when_flat():
    bid, ask = compute_quotes(mid=0.50, inventory=0.0, gamma=0.1, sigma=0.01, time_remaining=60, k=1.5)
    mid_of_quotes = (bid + ask) / 2
    assert mid_of_quotes == pytest.approx(0.50, abs=0.02)
    assert bid < ask


def test_compute_quotes_skews_down_when_long():
    # Parameters picked so neither case clips the [0.01, 0.99] bound --
    # gamma/k/sigma/time chosen to keep the spread moderate while still
    # producing a visible inventory skew.
    flat_bid, flat_ask = compute_quotes(mid=0.50, inventory=0.0, gamma=2.0, sigma=0.02, time_remaining=120, k=50.0)
    long_bid, long_ask = compute_quotes(mid=0.50, inventory=1.0, gamma=2.0, sigma=0.02, time_remaining=120, k=50.0)
    assert long_ask < flat_ask  # more eager to sell when already long
    assert long_bid < flat_bid  # less eager to buy more


def test_compute_quotes_never_crosses():
    bid, ask = compute_quotes(mid=0.50, inventory=9.9, gamma=2.0, sigma=0.05, time_remaining=60, k=1.5, max_inventory=10.0)
    assert bid < ask


def test_compute_quotes_stays_within_price_bounds():
    bid, ask = compute_quotes(mid=0.05, inventory=10.0, gamma=2.0, sigma=0.05, time_remaining=200, k=0.5, max_inventory=10.0)
    assert 0.01 <= bid < ask <= 0.99


def test_compute_quotes_respects_min_spread_floor():
    bid, ask = compute_quotes(mid=0.50, inventory=0.0, gamma=0.001, sigma=0.0001, time_remaining=1, k=1.5, min_spread=0.05)
    assert (ask - bid) >= 0.05 - 1e-9
