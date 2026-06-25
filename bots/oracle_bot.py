"""Bot 2: Oracle-verified directional.

Identical entry logic to Bot 1, but gated behind an independent Chainlink
BTC/USD read before trading. Two checks, both must pass:

1. Freshness -- the feed's latestRoundData must have updated recently. A
   stale oracle means we have no real confirmation of the current price,
   so we don't trust whatever Polymarket's order book implies either.
2. Stability -- the BTC/USD price must not have moved more than
   `max_divergence_pct` since our last observation (persisted to
   logs/oracle_state.json). A sudden jump means the market is repricing in
   real time and the "favored outcome is priced 0.85-0.99" signal is more
   likely to be stale/about-to-flip than a settled near-certainty.

Note: this is deliberately *not* a comparison between a market's $0-$1
contract price and a BTC/USD dollar price -- those aren't on the same
scale. It's a volatility/staleness guard around the same entry rule Bot 1
uses, which is what "oracle verification to avoid trading mispriced
markets" can actually mean given the data available from a public feed.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from bots.directional_bot import find_candidates, select_candidates, size_position
from core import chainlink, journal, resolution
from core import markets as markets_module
from core.paper_broker import InsufficientBalance, InsufficientLiquidity, PaperBroker

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
ORACLE_STATE_PATH = Path(__file__).resolve().parent.parent / "logs" / "oracle_state.json"
# Separate paper balance/positions from directional_bot's -- they'd otherwise
# silently share core.paper_broker.STATE_PATH and mix PnL between bots.
ORACLE_BROKER_STATE_PATH = Path(__file__).resolve().parent.parent / "logs" / "oracle_paper_state.json"
BOT_NAME = "oracle_bot"
MAX_FEED_AGE_SECONDS = 300  # Chainlink updates on ~heartbeat/deviation, not every block

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(BOT_NAME)


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _load_last_price() -> float | None:
    if not ORACLE_STATE_PATH.exists():
        return None
    with ORACLE_STATE_PATH.open(encoding="utf-8") as f:
        return json.load(f).get("last_price")


def _save_last_price(price: float) -> None:
    ORACLE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ORACLE_STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump({"last_price": price}, f)


def oracle_guard_ok(cfg: dict) -> tuple[bool, str]:
    """Returns (ok, reason). Updates persisted last-seen price as a side effect."""
    feed_address = cfg["oracle_bot"]["chainlink_feed_address"]
    try:
        price, age_seconds = chainlink.get_btc_usd_price_and_age(feed_address)
    except Exception as exc:
        return False, f"chainlink read failed: {exc}"

    if age_seconds > MAX_FEED_AGE_SECONDS:
        return False, f"feed stale ({age_seconds:.0f}s old)"

    last_price = _load_last_price()
    _save_last_price(price)
    if last_price is None:
        return True, "no prior price to compare yet"

    change = chainlink.relative_change_pct(last_price, price)
    max_divergence = cfg["oracle_bot"]["max_divergence_pct"]
    if change > max_divergence:
        return False, f"BTC moved {change:.1%} since last scan (limit {max_divergence:.1%})"

    return True, f"feed fresh ({age_seconds:.0f}s), BTC stable ({change:.1%} change)"


def run_once(cfg: dict | None = None, broker: PaperBroker | None = None) -> list[dict]:
    cfg = cfg or load_config()
    bot_cfg = cfg["directional_bot"]
    if cfg["mode"] != "paper":
        raise NotImplementedError(
            "live trading is intentionally not implemented yet -- see README"
        )

    broker = broker or PaperBroker(
        starting_balance=cfg["starting_balance"], state_path=ORACLE_BROKER_STATE_PATH
    )

    resolved = resolution.resolve_broker_positions(broker, BOT_NAME)
    if resolved:
        log.info("settled %d resolved position(s)", len(resolved))

    ok, reason = oracle_guard_ok(cfg)
    log.info("oracle guard: %s (%s)", "PASS" if ok else "BLOCK", reason)
    if not ok:
        journal.append_learning(BOT_NAME, f"Scan skipped -- oracle guard blocked: {reason}")
        return []

    btc_markets = markets_module.fetch_btc_markets()
    log.info("scanned %d BTC markets", len(btc_markets))

    market_candidates = []
    for market in btc_markets:
        if broker.has_open_position_for_market(market.market_id):
            continue  # already holding a position here -- don't add to or flip it
        if broker.has_open_position_for_event(market.end_date.isoformat()):
            continue  # already exposed to this resolution event via a different strike
        for candidate in find_candidates(market, bot_cfg):
            market_candidates.append((market, candidate))

    max_per_event = bot_cfg.get("max_correlated_markets_per_event", 1)
    selected = select_candidates(market_candidates, max_per_event)

    fills = []
    for market, candidate in selected:
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
                event_key=market.end_date.isoformat(),
            )
        except (InsufficientLiquidity, InsufficientBalance) as exc:
            log.info("skipped %s/%s: %s", market.slug, candidate["outcome"], exc)
            continue

        record = journal.log_trade(
            BOT_NAME,
            kind="entry",
            market_slug=market.slug,
            question=market.question,
            entry_price=candidate["ask_price"],
            seconds_to_resolution=market.seconds_to_resolution(),
            oracle_guard_reason=reason,
            **fill,
        )
        log.info(
            "BUY %s %s @ %.3f ($%.2f) | oracle: %s",
            market.slug,
            candidate["outcome"],
            fill["avg_price"],
            fill["cost"],
            reason,
        )
        fills.append(record)

    if not fills:
        log.info("no candidates found this scan")
    return fills


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot 2: oracle-verified directional BTC trader")
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
