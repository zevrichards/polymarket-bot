"""Driftless GBM probability model -- pure math, no network."""
import math

import pytest

from core.probability import estimate_volatility_per_second, prob_up, prob_up_twap


def test_prob_up_is_half_when_price_unchanged():
    assert prob_up(baseline_price=60000, current_price=60000, seconds_remaining=120, sigma_per_second=0.0001) == pytest.approx(0.5)


def test_prob_up_increases_when_price_above_baseline():
    p = prob_up(baseline_price=60000, current_price=60100, seconds_remaining=120, sigma_per_second=0.0001)
    assert p > 0.5


def test_prob_up_decreases_when_price_below_baseline():
    p = prob_up(baseline_price=60000, current_price=59900, seconds_remaining=120, sigma_per_second=0.0001)
    assert p < 0.5


def test_prob_up_symmetric_for_equal_and_opposite_moves():
    p_up = prob_up(baseline_price=60000, current_price=60100, seconds_remaining=120, sigma_per_second=0.0001)
    p_down = prob_up(baseline_price=60000, current_price=59900.17, seconds_remaining=120, sigma_per_second=0.0001)
    # log(60100/60000) ~= -log(59900.17/60000), so these should be symmetric around 0.5
    assert p_up + p_down == pytest.approx(1.0, abs=1e-3)


def test_prob_up_more_confident_with_less_time_remaining():
    # Same price gap, but less time left to revert -> more confident.
    p_far = prob_up(baseline_price=60000, current_price=60100, seconds_remaining=240, sigma_per_second=0.0001)
    p_near = prob_up(baseline_price=60000, current_price=60100, seconds_remaining=10, sigma_per_second=0.0001)
    assert p_near > p_far


def test_prob_up_more_confident_with_lower_volatility():
    p_high_vol = prob_up(baseline_price=60000, current_price=60100, seconds_remaining=120, sigma_per_second=0.001)
    p_low_vol = prob_up(baseline_price=60000, current_price=60100, seconds_remaining=120, sigma_per_second=0.00005)
    assert p_low_vol > p_high_vol


def test_prob_up_zero_time_remaining_resolves_by_sign():
    assert prob_up(60000, 60100, seconds_remaining=0, sigma_per_second=0.0001) == 1.0
    assert prob_up(60000, 59900, seconds_remaining=0, sigma_per_second=0.0001) == 0.0
    assert prob_up(60000, 60000, seconds_remaining=0, sigma_per_second=0.0001) == 0.5


def test_prob_up_zero_volatility_resolves_by_sign():
    assert prob_up(60000, 60100, seconds_remaining=60, sigma_per_second=0.0) == 1.0
    assert prob_up(60000, 59900, seconds_remaining=60, sigma_per_second=0.0) == 0.0


def test_prob_up_rejects_non_positive_prices():
    with pytest.raises(ValueError):
        prob_up(0, 60000, seconds_remaining=60, sigma_per_second=0.0001)
    with pytest.raises(ValueError):
        prob_up(60000, -1, seconds_remaining=60, sigma_per_second=0.0001)


def test_estimate_volatility_zero_with_insufficient_data():
    assert estimate_volatility_per_second([]) == 0.0
    assert estimate_volatility_per_second([60000]) == 0.0
    assert estimate_volatility_per_second([60000, 60001]) == 0.0


def test_estimate_volatility_zero_for_constant_price():
    assert estimate_volatility_per_second([60000.0] * 10) == 0.0


def test_estimate_volatility_positive_for_varying_price():
    prices = [60000, 60050, 59980, 60020, 60100, 59950, 60010]
    sigma = estimate_volatility_per_second(prices)
    assert sigma > 0
    assert math.isfinite(sigma)


# --- TWAP-aware model (Session 21) ---
# These markets resolve on TWAP-over-the-window, not the terminal spot
# price -- prob_up_twap answers that question instead of prob_up's.

def test_prob_up_twap_is_half_when_price_unchanged_at_window_start():
    p = prob_up_twap(baseline_price=60000, realized_avg_price=None, current_price=60000,
                      elapsed_seconds=0, remaining_seconds=300, sigma_per_second=0.0001)
    assert p == pytest.approx(0.5)


def test_prob_up_twap_more_confident_than_spot_model_for_same_displacement():
    # Session 21's key derivation: averaging over the path has 1/3 the
    # variance of the terminal point alone, so at the same displacement,
    # vol, and time, the TWAP question should be answered MORE
    # confidently than the terminal-spot question -- not less.
    spot_p = prob_up(baseline_price=60000, current_price=60100, seconds_remaining=300, sigma_per_second=0.0001)
    twap_p = prob_up_twap(baseline_price=60000, realized_avg_price=None, current_price=60100,
                           elapsed_seconds=0, remaining_seconds=300, sigma_per_second=0.0001)
    assert twap_p > spot_p > 0.5


def test_prob_up_twap_fully_elapsed_resolves_by_realized_average():
    # remaining=0 -- the window is over, the TWAP is now a known constant
    # (the realized average), not a random variable. current_price must
    # not matter at all once remaining_seconds is 0.
    p_won = prob_up_twap(baseline_price=60000, realized_avg_price=60050, current_price=1,
                          elapsed_seconds=300, remaining_seconds=0, sigma_per_second=0.0001)
    p_lost = prob_up_twap(baseline_price=60000, realized_avg_price=59950, current_price=999999,
                           elapsed_seconds=300, remaining_seconds=0, sigma_per_second=0.0001)
    assert p_won == 1.0
    assert p_lost == 0.0


def test_prob_up_twap_requires_realized_average_once_elapsed():
    with pytest.raises(ValueError):
        prob_up_twap(baseline_price=60000, realized_avg_price=None, current_price=60100,
                      elapsed_seconds=30, remaining_seconds=270, sigma_per_second=0.0001)


def test_prob_up_twap_realized_average_pulls_probability_toward_its_own_side():
    # Same current_price and baseline, but a realized_avg that's already
    # solidly above baseline should push the final probability up relative
    # to one whose realized_avg sits right at baseline -- the locked-in
    # portion of the window matters, not just the live spot reading.
    p_high_realized = prob_up_twap(baseline_price=60000, realized_avg_price=60200, current_price=60000,
                                    elapsed_seconds=200, remaining_seconds=100, sigma_per_second=0.0001)
    p_flat_realized = prob_up_twap(baseline_price=60000, realized_avg_price=60000, current_price=60000,
                                    elapsed_seconds=200, remaining_seconds=100, sigma_per_second=0.0001)
    assert p_high_realized > p_flat_realized


def test_prob_up_twap_zero_volatility_resolves_by_sign():
    assert prob_up_twap(60000, 60050, 60100, elapsed_seconds=100, remaining_seconds=200, sigma_per_second=0.0) == 1.0
    assert prob_up_twap(60000, 59950, 59900, elapsed_seconds=100, remaining_seconds=200, sigma_per_second=0.0) == 0.0


def test_prob_up_twap_rejects_non_positive_prices():
    with pytest.raises(ValueError):
        prob_up_twap(0, 60000, 60000, elapsed_seconds=0, remaining_seconds=300, sigma_per_second=0.0001)
    with pytest.raises(ValueError):
        prob_up_twap(60000, 60000, -1, elapsed_seconds=0, remaining_seconds=300, sigma_per_second=0.0001)


def test_prob_up_twap_rejects_non_positive_window():
    with pytest.raises(ValueError):
        prob_up_twap(60000, None, 60000, elapsed_seconds=0, remaining_seconds=0, sigma_per_second=0.0001)
