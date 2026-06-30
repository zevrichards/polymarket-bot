"""Avellaneda-Stoikov-style quote pricing: inventory-skewed reservation
price plus a volatility/time-scaled spread, instead of the old naive
symmetric-quote-around-mid approach (see BUILD_INTELLIGENCE_REPORT.md
Session 13-14 for why the naive version lost money even after the
order-flow imbalance gate -- it never adjusted price for inventory risk
or for how much time/uncertainty was left).

Standard formulas (Avellaneda & Stoikov, 2008):
  reservation price: r = mid - q * gamma * sigma^2 * (T - t)
  optimal spread:     delta = gamma * sigma^2 * (T - t) + (2/gamma) * ln(1 + gamma/k)

Where:
  mid    = current mid price
  q      = current inventory (positive = long, pushes r down so we're
           more eager to sell and less eager to buy more)
  gamma  = risk aversion (higher = more aggressive skewing/widening)
  sigma  = volatility (per unit time)
  T - t  = time remaining until the terminal horizon (market resolution)
  k      = order book liquidity/depth parameter (controls the second,
           non-inventory term of the spread -- the baseline cost of
           market presence). We use a fixed approximation rather than
           estimating it from book depth, since these markets have very
           thin, fast-changing books where a real-time k estimate would
           itself be noisy; a fixed k is a deliberate simplification, not
           an oversight.

These markets resolve to exactly $0 or $1 (not a continuous asset), and
"time remaining" is bounded and known exactly (we know precisely when a
market resolves) -- both make the AS framework's terminal-horizon
assumption a particularly good fit here, better than for a typical
continuously-traded asset with no fixed end time.
"""
from __future__ import annotations

import math


def estimate_contract_volatility(prices: list[float]) -> float:
    """Standard deviation of consecutive ABSOLUTE price changes -- NOT log
    returns. core/probability.py's log-return volatility estimator is
    correct for an unbounded price like BTC, but wrong for this bounded
    [0, 1] contract price: a tiny absolute move near a boundary (e.g.
    $0.01 -> $0.015) is a huge *percentage* move (50%), so log returns
    explode exactly where the price is most informative (near-certain
    outcomes). Confirmed via live smoke test: log-return sigma hit 0.3-0.58
    on a contract frozen near $0.01, blowing the spread/skew formula into
    a degenerate state that ignored inventory entirely -- the opposite of
    the protection it was supposed to provide. Absolute-change volatility
    doesn't have this blowup, since a $0.005 move is a $0.005 move
    regardless of whether the price is at $0.01 or $0.50.
    """
    if len(prices) < 3:
        return 0.0
    diffs = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    mean = sum(diffs) / len(diffs)
    variance = sum((d - mean) ** 2 for d in diffs) / (len(diffs) - 1)
    return math.sqrt(variance)


def reservation_price(mid: float, inventory: float, gamma: float, sigma: float, time_remaining: float) -> float:
    """The price we'd be indifferent to trading at, given current
    inventory risk -- NOT the mid price. Positive inventory pushes this
    below mid (more eager to sell), negative inventory pushes it above
    mid (more eager to buy, though this bot is long-only so inventory
    should never go negative in practice)."""
    if time_remaining <= 0:
        return mid
    return mid - inventory * gamma * (sigma ** 2) * time_remaining


def optimal_spread(gamma: float, sigma: float, time_remaining: float, k: float) -> float:
    """Full bid-ask spread width (not half-spread) -- widens with more
    time remaining (more uncertainty to be compensated for) and higher
    volatility, consistent with Glosten-Milgrom's logic that spread must
    cover expected adverse-selection cost."""
    if time_remaining <= 0:
        return 0.0
    inventory_term = gamma * (sigma ** 2) * time_remaining
    liquidity_term = (2.0 / gamma) * math.log(1.0 + gamma / k) if gamma > 0 and k > 0 else 0.0
    return inventory_term + liquidity_term


def compute_quotes(
    mid: float,
    inventory: float,
    gamma: float,
    sigma: float,
    time_remaining: float,
    k: float,
    min_spread: float = 0.01,
    max_inventory: float = 10.0,
) -> tuple[float, float]:
    """Returns (bid, ask), clamped to a sane [0.01, 0.99] price range and
    a minimum spread floor (the AS formula can produce an unrealistically
    tight spread when time_remaining/sigma are both small, which isn't
    safe to actually quote at on a real, if thin, order book).

    Inventory skew is capped at max_inventory's worth of effect so a
    single very large position doesn't push the reservation price to an
    extreme that would never realistically get quoted.
    """
    capped_inventory = max(-max_inventory, min(max_inventory, inventory))
    r = reservation_price(mid, capped_inventory, gamma, sigma, time_remaining)
    spread = max(optimal_spread(gamma, sigma, time_remaining, k), min_spread)

    bid = max(0.01, round(r - spread / 2, 2))
    ask = min(0.99, round(r + spread / 2, 2))
    if bid >= ask:
        # degenerate case (extreme skew/clamping) -- fall back to a
        # minimal symmetric quote around mid rather than crossing the book
        bid = max(0.01, round(mid - min_spread / 2, 2))
        ask = min(0.99, round(mid + min_spread / 2, 2))
    return bid, ask
