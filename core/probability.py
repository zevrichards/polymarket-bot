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
