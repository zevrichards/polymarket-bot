"""Bot 4: Cross-exchange lag / short-horizon probability model.

The first of the four candidate strategies (see chat history /
BUILD_INTELLIGENCE_REPORT.md Session 8-9) that gives the bot an actual
independent signal, instead of trusting Polymarket's own price (Bots 1/2)
or assuming balanced order flow (Bot 3) -- see Strategies.txt for why
those failed.

Edge thesis: Polymarket resolves these markets off Chainlink's BTC/USD
feed, which only updates every 10-30s or on a 0.5% deviation (confirmed
via Chainlink's own docs) -- not continuously, unlike spot exchanges.
core/probability.py estimates P(Up) directly from real BTC price action
(a driftless GBM model: how far has price already moved from the
window's baseline, given how much time/volatility remains) and compares
it to what Polymarket's own order book implies. We only trade when the
two *disagree* meaningfully -- agreement with the market's own price is
not a signal, since the market may already be right.

This deliberately does NOT use the same entry window as Bots 1/2 (last
seconds before resolution, when price has already converged). By then
the model and the market converge to the same answer and there's no
edge left to capture -- the edge thesis only matters mid-window, while
the market is still uncertain. See find_candidates() for the actual
window used.

UNVERIFIED. This is a new hypothesis, not a confirmed strategy -- it
needs the same kind of real-data scrutiny Bots 1-3 got before trusting
any result from it.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from core import binance_client, clob_client, journal, markets, probability, resolution
from core.paper_broker import InsufficientBalance, InsufficientLiquidity, PaperBroker

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
LAG_BROKER_STATE_PATH = Path(__file__).resolve().parent.parent / "logs" / "lag_paper_state.json"
BOT_NAME = "lag_bot"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(BOT_NAME)


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def market_implied_prob(token_id: str) -> tuple[float, object] | tuple[None, None]:
    """Returns (mid_price, order_book) for the given outcome token, or
    (None, None) if the book is empty. Mid is used rather than ask alone
    to avoid the spread biasing the market's "implied probability"."""
    try:
        book = clob_client.get_order_book(token_id)
    except Exception as exc:
        log.warning("order book fetch failed for token %s: %s", token_id[:12], exc)
        return None, None

    if not book.bids or not book.asks:
        return None, None

    best_bid = max(float(level.price) for level in book.bids)
    best_ask = min(float(level.price) for level in book.asks)
    return (best_bid + best_ask) / 2, book


def find_candidate(market, recent_prices: list[float], cfg: dict) -> dict | None:
    """Returns a trade candidate dict, or None if this market doesn't
    qualify. Unlike Bots 1/2, this looks for *disagreement* between our
    model and the market, not an already-extreme market price."""
    if market.start_date is None:
        return None  # can't compute a baseline without the window's real start time

    seconds_left = market.seconds_to_resolution()
    if seconds_left is None:
        return None
    if not (cfg["min_seconds_to_resolution"] <= seconds_left <= cfg["max_seconds_to_resolution"]):
        return None

    seconds_since_start = time.time() - market.start_date.timestamp()
    if seconds_since_start < cfg["min_warmup_seconds"]:
        return None  # need at least a little realized price action to measure

    baseline_price = binance_client.get_price_at(int(market.start_date.timestamp() * 1000))
    if baseline_price is None:
        return None

    if not recent_prices:
        return None
    current_price = recent_prices[-1]
    sigma = probability.estimate_volatility_per_second(recent_prices)
    if sigma <= 0:
        return None

    model_p_up = probability.prob_up(baseline_price, current_price, seconds_left, sigma)

    if "Up" not in market.outcomes or "Down" not in market.outcomes:
        return None
    up_token = market.token_ids[market.outcomes.index("Up")]
    down_token = market.token_ids[market.outcomes.index("Down")]

    market_p_up, up_book = market_implied_prob(up_token)
    if market_p_up is None:
        return None

    edge = model_p_up - market_p_up
    min_edge = cfg["min_edge"]
    min_price, max_price = cfg["entry_price_range"]

    if edge >= min_edge and min_price <= market_p_up <= max_price:
        return {
            "outcome": "Up",
            "token_id": up_token,
            "book": up_book,
            "model_p": model_p_up,
            "market_p": market_p_up,
            "edge": edge,
            "baseline_price": baseline_price,
            "current_price": current_price,
            "sigma_per_second": sigma,
        }

    if -edge >= min_edge:
        market_p_down, down_book = market_implied_prob(down_token)
        if market_p_down is None:
            return None
        if min_price <= market_p_down <= max_price:
            return {
                "outcome": "Down",
                "token_id": down_token,
                "book": down_book,
                "model_p": 1.0 - model_p_up,
                "market_p": market_p_down,
                "edge": (1.0 - model_p_up) - market_p_down,
                "baseline_price": baseline_price,
                "current_price": current_price,
                "sigma_per_second": sigma,
            }

    return None


def size_position(broker: PaperBroker, cfg: dict) -> float:
    return min(cfg["max_bet"], broker.balance * cfg["max_bankroll_fraction"])


def run_once(cfg: dict | None = None, broker: PaperBroker | None = None) -> list[dict]:
    cfg = cfg or load_config()
    bot_cfg = cfg["lag_bot"]
    if cfg["mode"] != "paper":
        raise NotImplementedError(
            "live trading is intentionally not implemented yet -- see README"
        )

    broker = broker or PaperBroker(
        starting_balance=cfg["starting_balance"], state_path=LAG_BROKER_STATE_PATH
    )

    resolved = resolution.resolve_broker_positions(broker, BOT_NAME)
    if resolved:
        log.info("settled %d resolved position(s)", len(resolved))

    btc_markets = markets.fetch_btc_markets()
    log.info("scanned %d BTC markets", len(btc_markets))

    try:
        recent_prices = binance_client.get_recent_prices(bot_cfg["vol_lookback_seconds"])
    except Exception as exc:
        log.warning("Binance price fetch failed, skipping scan: %s", exc)
        return []

    fills = []
    for market in btc_markets:
        if broker.has_open_position_for_market(market.market_id):
            continue
        if market.end_date and broker.has_open_position_for_event(market.end_date.isoformat()):
            continue

        candidate = find_candidate(market, recent_prices, bot_cfg)
        if candidate is None:
            continue

        usd_amount = size_position(broker, bot_cfg)
        if usd_amount <= 0:
            continue
        try:
            fill = broker.buy(
                market_id=market.market_id,
                token_id=candidate["token_id"],
                outcome=candidate["outcome"],
                usd_amount=usd_amount,
                order_book=candidate["book"],
                event_key=market.end_date.isoformat() if market.end_date else "",
            )
        except (InsufficientLiquidity, InsufficientBalance) as exc:
            log.info("skipped %s/%s: %s", market.slug, candidate["outcome"], exc)
            continue

        record = journal.log_trade(
            BOT_NAME,
            kind="entry",
            market_slug=market.slug,
            question=market.question,
            seconds_to_resolution=market.seconds_to_resolution(),
            model_p=candidate["model_p"],
            market_p=candidate["market_p"],
            edge=candidate["edge"],
            baseline_price=candidate["baseline_price"],
            current_price=candidate["current_price"],
            sigma_per_second=candidate["sigma_per_second"],
            **fill,
        )
        log.info(
            "BUY %s %s @ %.3f ($%.2f) | model=%.3f market=%.3f edge=%+.3f",
            market.slug, candidate["outcome"], fill["avg_price"], fill["cost"],
            candidate["model_p"], candidate["market_p"], candidate["edge"],
        )
        fills.append(record)

    if not fills:
        log.info("no candidates found this scan")
    return fills


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot 4: cross-exchange lag / probability model")
    parser.add_argument("--once", action="store_true", help="run a single scan and exit")
    args = parser.parse_args()

    if args.once:
        run_once()
        return

    from core.scheduler import run_forever

    cfg = load_config()
    run_forever(run_once, interval_seconds=cfg["scan_interval_seconds"], label=BOT_NAME)


if __name__ == "__main__":
    main()
