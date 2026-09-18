"""Estimates P(Up) for a BTC up/down market from real BTC price data,
independent of Polymarket's own price -- this is the actual "edge" piece
described as missing from Bots 1-3 in Strategies.txt.

Model: a driftless (mu=0) geometric Brownian motion. We deliberately do
NOT assume momentum continues or reverts -- there's no validated evidence
either way for this project, and baking in an unjustified drift assumption
would just be a different unproven guess dressed up as math. The only
inputs are: how far the current price already is from the window's
baseline (the resolution-relevant comparison point), how much time is
left, and how volatile BTC has actually been recently. Compare the result
against Polymarket's own implied probability (its price); only the
*disagreement* between the two is a tradeable signal, not either one in
isolation.

Derivation: under a driftless GBM, ln(S_T) | S_now ~ Normal(ln(S_now),
sigma^2 * t_remaining) (Ito correction term omitted -- a known, deliberate
simplification appropriate for short horizons where the correction is
negligible; see BUILD_INTELLIGENCE_REPORT.md Session 9 for the choice not
to add complexity that isn't justified by evidence we have).

  P(Up) = P(S_T >= S_0 | S_now)
         = Phi( (ln(S_now) - ln(S_0)) / (sigma * sqrt(t_remaining)) )

If S_now == S_0 this is exactly 0.5 (no information yet). The more the
price has already moved away from the baseline, and the less time/
volatility remains for that to reverse, the closer this pushes to 0 or 1.
"""
from __future__ import annotations

import math


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def estimate_volatility_per_second(prices: list[float]) -> float:
    """Standard deviation of consecutive log returns -- a simple realized
    volatility estimate. Returns 0.0 if there isn't enough data to compute
    a meaningful estimate (fewer than 2 usable returns)."""
    if len(prices) < 3:
        return 0.0

    log_returns = []
    for i in range(1, len(prices)):
        if prices[i - 1] <= 0 or prices[i] <= 0:
            continue
        log_returns.append(math.log(prices[i] / prices[i - 1]))

    if len(log_returns) < 2:
        return 0.0

    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    return math.sqrt(variance)


def prob_up(
    baseline_price: float,
    current_price: float,
    seconds_remaining: float,
    sigma_per_second: float,
) -> float:
    """Returns model-estimated P(price at resolution >= baseline_price),
    given the current price, time left, and recent volatility.

    Degenerate cases (no time left, or no observed volatility) are
    resolved by the sign of (current_price - baseline_price) rather than
    dividing by zero -- if there's no time/uncertainty left for the price
    to move, whichever side it's currently on is treated as certain.
    """
    if baseline_price <= 0 or current_price <= 0:
        raise ValueError("prices must be positive")

    if seconds_remaining <= 0 or sigma_per_second <= 0:
        if current_price > baseline_price:
            return 1.0
        if current_price < baseline_price:
            return 0.0
        return 0.5

    z = (math.log(current_price) - math.log(baseline_price)) / (
        sigma_per_second * math.sqrt(seconds_remaining)
    )
    return norm_cdf(z)


def prob_up_twap(
    baseline_price: float,
    realized_avg_price: float | None,
    current_price: float,
    elapsed_seconds: float,
    remaining_seconds: float,
    sigma_per_second: float,
) -> float:
    """Returns model-estimated P(TWAP over the full window >= baseline_price).

    Session 21 finding: these markets do NOT resolve on the terminal spot
    price -- Polymarket's own resolution rule is "TWAP of the time range
    >= price at the beginning of that range." prob_up() answers a different
    question (will the endpoint be above baseline) than the one that
    actually settles the market (will the *average* over the whole window
    be above baseline). This function answers the real one.

    Model: split the window into the REALIZED portion (window start to
    now -- already observed, so its average is a known constant, not a
    random variable) and the REMAINING portion (now to window end --
    unknown, modeled the same driftless way as prob_up: E[future price] =
    current_price, no drift assumption). The window's final TWAP is the
    time-weighted blend of the two:

      final_twap = (elapsed/T)*realized_avg + (remaining/T)*future_avg

    E[future_avg] = current_price (martingale property of a driftless walk).
    Var[future_avg] uses the standard result for the time-average of a
    Brownian path over duration tau: Var = sigma^2 * tau / 3 -- ONE THIRD
    the variance of the tau-ahead *endpoint* alone (Var = sigma^2 * tau).
    Intuitively: an average is anchored by near-term values close to the
    current level as well as the more-wandered-off far values, so it's a
    less noisy target than the raw endpoint. This is the same math behind
    Asian options trading at lower implied vol than vanilla options on the
    same underlying.

    Deliberate simplification: this works in PRICE space (Normal), not log
    space (lognormal) like prob_up -- summing/averaging lognormal path
    segments has no closed form, while summing Normal ones does. For a
    5-minute BTC window the relative price range is small enough (typically
    well under 1%) that Normal vs lognormal is a negligible difference, well
    inside the same "short-horizon simplification" territory as the Ito
    correction prob_up already omits. sigma_dollar = current_price *
    sigma_per_second is the standard local linearization from log-vol to
    dollar-vol at the current price level.

    A useful sanity check this implies: early in the window (elapsed=0),
    for the same displacement/vol/time, this returns a MORE extreme
    probability than prob_up would for the same inputs -- not less. The
    market's own price, if it prices off the real TWAP mechanism, should
    look more "confident" than prob_up() ever gave it credit for. That
    matches this session's repeated observation of the market looking
    smarter than the old model.

    realized_avg_price may be None only when elapsed_seconds == 0 (the very
    start of the window, where it carries zero weight anyway).
    """
    if baseline_price <= 0 or current_price <= 0:
        raise ValueError("prices must be positive")
    if elapsed_seconds < 0 or remaining_seconds < 0:
        raise ValueError("elapsed_seconds and remaining_seconds must be non-negative")
    total_seconds = elapsed_seconds + remaining_seconds
    if total_seconds <= 0:
        raise ValueError("elapsed_seconds + remaining_seconds must be positive")
    if elapsed_seconds > 0 and realized_avg_price is None:
        raise ValueError("realized_avg_price is required once elapsed_seconds > 0")

    weight_realized = elapsed_seconds / total_seconds
    weight_remaining = remaining_seconds / total_seconds
    expected_final_twap = weight_realized * (realized_avg_price or 0.0) + weight_remaining * current_price

    if remaining_seconds <= 0 or sigma_per_second <= 0:
        if expected_final_twap > baseline_price:
            return 1.0
        if expected_final_twap < baseline_price:
            return 0.0
        return 0.5

    sigma_dollar = current_price * sigma_per_second
    variance = (weight_remaining ** 2) * (sigma_dollar ** 2) * remaining_seconds / 3.0
    if variance <= 0:
        if expected_final_twap > baseline_price:
            return 1.0
        if expected_final_twap < baseline_price:
            return 0.0
        return 0.5

    z = (expected_final_twap - baseline_price) / math.sqrt(variance)
    return norm_cdf(z)
