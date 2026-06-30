"""Bot 4: Cross-exchange lag / short-horizon probability model.

Session 15 update: real-time market_p via WebSocket + edge persistence.

Edge thesis (unchanged): Polymarket resolves these markets off Chainlink's
BTC/USD feed, which only updates every 10-30s or on a 0.5% deviation
(confirmed via Chainlink's own docs) -- not continuously, unlike spot
exchanges. core/probability.py estimates P(Up) directly from real BTC
price action (a driftless GBM model) and compares it to what Polymarket's
own order book implies. We only trade when the two *disagree* meaningfully
-- agreement with the market's own price is not a signal.

What changed in Session 15, and why: `market_p` (what Polymarket currently
implies) used to come from a REST order-book read, refreshed once every
60-second scan -- so by the time we compared it to a freshly-computed
`model_p`, market_p could be up to a minute stale. That's not the same bug
as "we're slower than other Polymarket traders" (we were never trying to
out-race them on their own data) -- it's that comparing today's model
output to a stale market reading can manufacture a fake disagreement that
isn't really there. core/live_orderbook.py (built for the market_maker_bot
rewrite) now supplies market_p, so the comparison is apples-to-apples at
one consistent moment.

Going faster also let us add something genuinely new, not just a
staleness fix: instead of acting on a single tick's disagreement (which
could just be noise/flicker), we now require the edge to persist in the
same direction for `min_consecutive_ticks` consecutive ticks before
trading. This is a real filter the old 60s-snapshot architecture couldn't
support -- there's no such thing as "persistence" when you only get one
reading per minute.

Honest open question (see chat, not yet resolved by data): going faster
could either reveal a real, structural lag in Polymarket's pricing (if
true, this should make the measured edge MORE reliable), or reveal that
most of the previously-measured edge was an artifact of our own staleness
(if true, the measured edge should shrink once market_p stops being
stale). We don't know which yet -- that's exactly why the previous
(profitable) state was backed up and tagged before this rewrite.

Risk controls (Session 11, unchanged in spirit, Kelly sizing / stop-loss /
daily cap / liquidity filter) all carry over -- stop-loss checks also now
read the live WebSocket book instead of REST, for the same staleness
reason as entries.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from core import binance_client, journal, markets, probability, resolution
from core.live_orderbook import LiveOrderBook
from core.paper_broker import InsufficientBalance, InsufficientLiquidity, PaperBroker

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
LAG_BROKER_STATE_PATH = Path(__file__).resolve().parent.parent / "logs" / "lag_paper_state.json"
STOPOUT_BLACKLIST_PATH = Path(__file__).resolve().parent.parent / "logs" / "lag_stopout_blacklist.json"
BOT_NAME = "lag_bot"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(BOT_NAME)


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def select_tracked_markets(cfg: dict) -> list:
    """Unlike market_maker_bot, this isn't narrowed to a tiny handful --
    the strategy benefits from scanning broadly for rare disagreements
    across many markets, not from concentrating on one or two."""
    btc_markets = markets.fetch_btc_markets()
    return [
        m for m in btc_markets
        if m.seconds_to_resolution() is not None
        and cfg["min_seconds_to_resolution"] <= m.seconds_to_resolution() <= cfg["max_seconds_to_resolution"]
    ]


def compute_signal(market, recent_prices: list[float], cfg: dict, ws_book: LiveOrderBook) -> dict | None:
    """One tick's reading of (model_p_up - market_p_up) for a market, or
    None if we don't have enough data yet. Does NOT decide whether to
    trade -- that's tick()'s job, based on whether this signal persists
    across multiple calls."""
    if market.start_date is None:
        return None  # can't compute a baseline without the window's real start time

    seconds_left = market.seconds_to_resolution()
    if seconds_left is None:
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

    up_bid, up_ask = ws_book.best_bid_ask(up_token)
    if up_bid is None or up_ask is None:
        return None
    market_p_up = (up_bid + up_ask) / 2

    return {
        "up_token": up_token,
        "down_token": down_token,
        "model_p_up": model_p_up,
        "market_p_up": market_p_up,
        "up_edge": model_p_up - market_p_up,
        "baseline_price": baseline_price,
        "current_price": current_price,
        "sigma_per_second": sigma,
    }


def edge_confirmed(history: list[float], min_edge: float, min_consecutive: int) -> bool:
    """True if the last `min_consecutive` ticks all agree on direction and
    magnitude -- filters out single-tick noise/flicker. A signal that
    flips sign or drops below threshold partway through does NOT count,
    even if the average would clear the bar."""
    if len(history) < min_consecutive:
        return False
    recent = history[-min_consecutive:]
    return all(e >= min_edge for e in recent) or all(e <= -min_edge for e in recent)


def confirm_and_build_candidate(market, signal: dict, cfg: dict, ws_book: LiveOrderBook) -> dict | None:
    """Given a signal whose edge has just been confirmed as persistent,
    re-reads live price/liquidity right now (not the historical tick
    values) and builds the actual trade candidate -- keeps the entry
    decision as fresh as possible rather than acting on a few-ticks-old
    snapshot."""
    min_edge = cfg["min_edge"]
    min_price, max_price = cfg["entry_price_range"]
    min_depth = cfg.get("min_book_depth_usd", 0.0)

    if signal["up_edge"] >= min_edge:
        token_id, outcome, model_p = signal["up_token"], "Up", signal["model_p_up"]
    elif -signal["up_edge"] >= min_edge:
        token_id, outcome, model_p = signal["down_token"], "Down", 1.0 - signal["model_p_up"]
    else:
        return None

    bid, ask = ws_book.best_bid_ask(token_id)
    if bid is None or ask is None:
        return None
    market_p = (bid + ask) / 2
    if not (min_price <= market_p <= max_price):
        return None

    depth = ws_book.depth_usd(token_id)
    if depth is None or depth < min_depth:
        return None

    book = ws_book.get_book(token_id)
    if book is None:
        return None

    edge = model_p - market_p
    if edge < min_edge:
        return None  # price moved since confirmation -- don't chase a vanished edge

    return {
        "outcome": outcome,
        "token_id": token_id,
        "book": book,
        "model_p": model_p,
        "market_p": market_p,
        "edge": edge,
        "baseline_price": signal["baseline_price"],
        "current_price": signal["current_price"],
        "sigma_per_second": signal["sigma_per_second"],
    }


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


def load_stopout_blacklist() -> set[str]:
    if STOPOUT_BLACKLIST_PATH.exists():
        with STOPOUT_BLACKLIST_PATH.open(encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_stopout_blacklist(blacklist: set[str]) -> None:
    STOPOUT_BLACKLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STOPOUT_BLACKLIST_PATH.open("w", encoding="utf-8") as f:
        json.dump(sorted(blacklist), f)


def check_stop_losses(broker: PaperBroker, cfg: dict, ws_book: LiveOrderBook) -> list[dict]:
    """Exit positions early if the current best bid implies a loss beyond
    stop_loss_pct of cost basis. Also blacklists the market_id from
    re-entry -- see Session 12 for the bug this prevents (re-buying into
    the exact move we were just stopped out of)."""
    stop_loss_pct = cfg.get("stop_loss_pct")
    if not stop_loss_pct:
        return []

    results = []
    blacklist = load_stopout_blacklist()

    for token_id, position in list(broker.state.positions.items()):
        bid, ask = ws_book.best_bid_ask(token_id)
        if bid is None or position.avg_price <= 0:
            continue

        loss_frac = (position.avg_price - bid) / position.avg_price
        if loss_frac < stop_loss_pct:
            continue

        book = ws_book.get_book(token_id)
        if book is None:
            continue  # can't simulate a realistic sell without a depth snapshot

        try:
            result = broker.sell(token_id, book)
        except Exception as exc:
            log.warning("stop-loss sell failed for %s: %s", token_id[:12], exc)
            continue

        blacklist.add(position.market_id)

        record = journal.log_trade(BOT_NAME, kind="stop_loss_exit", **result)
        log.info(
            "STOP-LOSS exit %s %s @ %.3f | loss=%.1f%% pnl=%+.2f (market blacklisted from re-entry)",
            result["market_id"], result["outcome"], result["avg_exit_price"],
            loss_frac * 100, result["pnl"],
        )
        results.append(record)

    if results:
        save_stopout_blacklist(blacklist)

    return results


def tick(
    cfg: dict,
    ws_book: LiveOrderBook,
    broker: PaperBroker,
    tracked: list,
    recent_prices: list[float],
    edge_history: dict,
) -> list[dict]:
    bot_cfg = cfg["lag_bot"]
    fills = []
    max_daily_trades = bot_cfg.get("max_daily_trades")
    trades_today = count_todays_entries(BOT_NAME) if max_daily_trades else 0
    stopout_blacklist = load_stopout_blacklist()
    min_consecutive = bot_cfg.get("min_consecutive_ticks", 1)
    min_edge = bot_cfg["min_edge"]

    for market in tracked:
        if max_daily_trades and trades_today + len(fills) >= max_daily_trades:
            log.info("daily trade cap (%d) reached, skipping remaining candidates", max_daily_trades)
            break
        if market.market_id in stopout_blacklist:
            continue
        if broker.has_open_position_for_market(market.market_id):
            continue
        if market.end_date and broker.has_open_position_for_event(market.end_date.isoformat()):
            continue

        signal = compute_signal(market, recent_prices, bot_cfg, ws_book)
        if signal is None:
            continue

        history = edge_history.setdefault(market.market_id, [])
        history.append(signal["up_edge"])
        if len(history) > min_consecutive:
            history.pop(0)

        if not edge_confirmed(history, min_edge, min_consecutive):
            continue

        candidate = confirm_and_build_candidate(market, signal, bot_cfg, ws_book)
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
            confirmed_over_ticks=len(history),
            **fill,
        )
        log.info(
            "BUY %s %s @ %.3f ($%.2f) | model=%.3f market=%.3f edge=%+.3f (confirmed over %d ticks)",
            market.slug, candidate["outcome"], fill["avg_price"], fill["cost"],
            candidate["model_p"], candidate["market_p"], candidate["edge"], len(history),
        )
        fills.append(record)
        edge_history.pop(market.market_id, None)  # acted on it -- start fresh next time

    if not fills:
        log.info("no candidates found this tick")
    return fills


def _refresh(bot_cfg: dict, ws_book: LiveOrderBook, broker: PaperBroker, subscribed: set) -> tuple[list, set]:
    resolved = resolution.resolve_broker_positions(broker, BOT_NAME)
    if resolved:
        log.info("settled %d resolved position(s)", len(resolved))

    stopped_out = check_stop_losses(broker, bot_cfg, ws_book)
    if stopped_out:
        log.info("stopped out of %d position(s)", len(stopped_out))

    tracked = select_tracked_markets(bot_cfg)
    log.info("scanned %d BTC markets", len(tracked))

    # Subscribe to every candidate's tokens (for finding new entries) AND
    # every currently-open position's token (for stop-loss monitoring) --
    # a position can outlive its market's presence in the candidate window.
    candidate_ids = {tid for m in tracked for tid in m.token_ids}
    position_ids = set(broker.state.positions.keys())
    needed = candidate_ids | position_ids

    to_subscribe = needed - subscribed
    to_unsubscribe = subscribed - needed
    if to_subscribe:
        ws_book.subscribe(list(to_subscribe))
    if to_unsubscribe:
        ws_book.unsubscribe(list(to_unsubscribe))

    return tracked, needed


def run_loop(cfg: dict | None = None) -> None:
    cfg = cfg or load_config()
    bot_cfg = cfg["lag_bot"]
    if cfg["mode"] != "paper":
        raise NotImplementedError("live trading is intentionally not implemented yet -- see README")

    broker = PaperBroker(starting_balance=cfg["starting_balance"], state_path=LAG_BROKER_STATE_PATH)
    ws_book = LiveOrderBook()
    ws_book.start()
    log.info(
        "%s: WebSocket client starting, tick every %ss, refreshing tracked markets every %ss",
        BOT_NAME, bot_cfg["tick_interval_seconds"], bot_cfg["refresh_interval_seconds"],
    )

    tracked: list = []
    subscribed: set = set()
    edge_history: dict = {}
    last_refresh = 0.0

    try:
        while True:
            try:
                now = time.time()
                if now - last_refresh >= bot_cfg["refresh_interval_seconds"]:
                    tracked, subscribed = _refresh(bot_cfg, ws_book, broker, subscribed)
                    last_refresh = now

                recent_prices = binance_client.get_recent_prices(bot_cfg["vol_lookback_seconds"])
                tick(cfg, ws_book, broker, tracked, recent_prices, edge_history)
            except KeyboardInterrupt:
                raise
            except Exception:
                # Session 16: a single uncaught exception here (e.g. a
                # transient Gamma 403/timeout) used to kill the entire
                # process -- this loop had no equivalent of
                # core/scheduler.py's catch-log-retry resilience that the
                # older bots get for free. Confirmed live: an unhandled
                # 403 silently terminated this bot for an extended period
                # before anyone noticed the report had gone stale.
                log.exception("%s: tick failed, will retry next interval", BOT_NAME)

            time.sleep(bot_cfg["tick_interval_seconds"])
    except KeyboardInterrupt:
        log.info("%s: shutting down", BOT_NAME)
    finally:
        ws_book.stop()


def run_smoke_test(cfg: dict | None = None, duration_seconds: int = 30) -> None:
    """--once equivalent for a continuously-ticking bot: run the real
    loop for a short fixed duration, then exit, instead of one-shot."""
    cfg = cfg or load_config()
    bot_cfg = cfg["lag_bot"]
    if cfg["mode"] != "paper":
        raise NotImplementedError("live trading is intentionally not implemented yet -- see README")

    broker = PaperBroker(starting_balance=cfg["starting_balance"], state_path=LAG_BROKER_STATE_PATH)
    ws_book = LiveOrderBook()
    ws_book.start()

    tracked, subscribed = _refresh(bot_cfg, ws_book, broker, set())
    log.info("tracking %d market(s)", len(tracked))

    edge_history: dict = {}
    start = time.time()
    while time.time() - start < duration_seconds:
        try:
            recent_prices = binance_client.get_recent_prices(bot_cfg["vol_lookback_seconds"])
        except Exception as exc:
            log.warning("Binance price fetch failed: %s", exc)
            time.sleep(bot_cfg["tick_interval_seconds"])
            continue
        tick(cfg, ws_book, broker, tracked, recent_prices, edge_history)
        time.sleep(bot_cfg["tick_interval_seconds"])

    ws_book.stop()
    log.info("smoke test complete")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot 4: cross-exchange lag / probability model")
    parser.add_argument(
        "--once", action="store_true",
        help="run a short (~30s) smoke test and exit, instead of running forever",
    )
    args = parser.parse_args()

    if args.once:
        run_smoke_test()
        return

    run_loop()


if __name__ == "__main__":
    main()
