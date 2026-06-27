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

Risk controls added in Session 11 (these were flagged as missing back
in Session 9 -- this bot originally just reused Bots 1/2's flat sizing
and no stop-loss/cap/liquidity checks at all):

1. Kelly-criterion sizing, replacing the flat max_bet/max_bankroll_fraction
   sizing -- bet size now scales with how large the model-vs-market edge
   actually is, fractional (kelly_fraction) to reduce variance, still
   capped by max_bet/max_bankroll_fraction as a hard ceiling.
2. Stop-loss: positions are checked every scan (not just at resolution)
   and exited early via the new PaperBroker.sell() if the current best
   bid implies a loss beyond stop_loss_pct of cost basis.
3. Daily trade cap (max_daily_trades) -- refuses new entries once today's
   count is reached, regardless of how many qualifying candidates remain.
4. Minimum-liquidity filter (min_book_depth_usd) -- skips a candidate if
   its order book is too thin to trust the price or fill realistically.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
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


def book_depth_usd(book) -> float:
    """Total notional resting on both sides of the book -- a thin book
    means the price we're reading is easy to move and risky to trust."""
    return sum(float(l.price) * float(l.size) for l in book.bids) + sum(
        float(l.price) * float(l.size) for l in book.asks
    )


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
    min_depth = cfg.get("min_book_depth_usd", 0.0)

    if edge >= min_edge and min_price <= market_p_up <= max_price:
        if book_depth_usd(up_book) >= min_depth:
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
        if min_price <= market_p_down <= max_price and book_depth_usd(down_book) >= min_depth:
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


def size_position(broker: PaperBroker, candidate: dict, cfg: dict) -> float:
    """Fractional-Kelly sizing: bet size scales with how large the edge
    actually is, instead of a flat fraction every time. For a binary bet
    costing `price` per share with model probability `model_p` of paying
    out $1, the Kelly-optimal bankroll fraction is (model_p - price) /
    (1 - price). `kelly_fraction` (e.g. 0.25 for quarter-Kelly) scales
    this down to reduce variance -- full Kelly is famously aggressive.
    Still hard-capped by max_bet/max_bankroll_fraction as a ceiling.
    """
    price = candidate["market_p"]
    model_p = candidate["model_p"]
    if price <= 0 or price >= 1:
        return 0.0

    raw_kelly = (model_p - price) / (1 - price)
    raw_kelly = max(0.0, min(1.0, raw_kelly))
    kelly_usd = broker.balance * raw_kelly * cfg["kelly_fraction"]

    return min(kelly_usd, cfg["max_bet"], broker.balance * cfg["max_bankroll_fraction"])


def count_todays_entries(bot_name: str) -> int:
    today = datetime.now(timezone.utc).date()
    count = 0
    for t in journal.read_trades(bot_name):
        if t.get("kind") != "entry":
            continue
        try:
            if datetime.fromisoformat(t["timestamp"]).date() == today:
                count += 1
        except (KeyError, ValueError):
            continue
    return count


def check_stop_losses(broker: PaperBroker, cfg: dict) -> list[dict]:
    """Exit positions early if the current best bid implies a loss beyond
    stop_loss_pct of cost basis -- without this, a position is locked in
    until resolution no matter how far the price moves against it."""
    stop_loss_pct = cfg.get("stop_loss_pct")
    if not stop_loss_pct:
        return []

    results = []
    for token_id, position in list(broker.state.positions.items()):
        try:
            book = clob_client.get_order_book(token_id)
        except Exception as exc:
            log.warning("stop-loss check: order book fetch failed for %s: %s", token_id[:12], exc)
            continue
        if not book.bids or position.avg_price <= 0:
            continue

        best_bid = max(float(level.price) for level in book.bids)
        loss_frac = (position.avg_price - best_bid) / position.avg_price
        if loss_frac < stop_loss_pct:
            continue

        try:
            result = broker.sell(token_id, book)
        except Exception as exc:
            log.warning("stop-loss sell failed for %s: %s", token_id[:12], exc)
            continue

        record = journal.log_trade(BOT_NAME, kind="stop_loss_exit", **result)
        log.info(
            "STOP-LOSS exit %s %s @ %.3f | loss=%.1f%% pnl=%+.2f",
            result["market_id"], result["outcome"], result["avg_exit_price"],
            loss_frac * 100, result["pnl"],
        )
        results.append(record)

    return results


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

    stopped_out = check_stop_losses(broker, bot_cfg)
    if stopped_out:
        log.info("stopped out of %d position(s)", len(stopped_out))

    btc_markets = markets.fetch_btc_markets()
    log.info("scanned %d BTC markets", len(btc_markets))

    try:
        recent_prices = binance_client.get_recent_prices(bot_cfg["vol_lookback_seconds"])
    except Exception as exc:
        log.warning("Binance price fetch failed, skipping scan: %s", exc)
        return []

    max_daily_trades = bot_cfg.get("max_daily_trades")
    trades_today = count_todays_entries(BOT_NAME) if max_daily_trades else 0

    fills = []
    for market in btc_markets:
        if max_daily_trades and trades_today + len(fills) >= max_daily_trades:
            log.info("daily trade cap (%d) reached, skipping remaining candidates", max_daily_trades)
            break
        if broker.has_open_position_for_market(market.market_id):
            continue
        if market.end_date and broker.has_open_position_for_event(market.end_date.isoformat()):
            continue

        candidate = find_candidate(market, recent_prices, bot_cfg)
        if candidate is None:
            continue

        usd_amount = size_position(broker, candidate, bot_cfg)
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
