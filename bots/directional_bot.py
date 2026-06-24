"""Bot 1: Directional.

Buys the favored outcome of a BTC up/down market when it's priced in the
$0.85-$0.99 band with a short window left before resolution -- the classic
"buy the near-certain side cheaply, let it settle to $1" trade described in
the source article. Paper-trading only; see README for what's deferred
before this can run live.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from core import clob_client, journal, markets
from core.paper_broker import InsufficientBalance, InsufficientLiquidity, PaperBroker

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
BOT_NAME = "directional_bot"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(BOT_NAME)


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def find_candidates(market, cfg: dict) -> list[dict]:
    """Return [{token_id, outcome, ask_price}] for outcomes priced in-band."""
    seconds_left = market.seconds_to_resolution()
    if seconds_left is None:
        return []
    if not (cfg["min_seconds_to_resolution"] <= seconds_left <= cfg["max_seconds_to_resolution"]):
        return []

    candidates = []
    for outcome, token_id in zip(market.outcomes, market.token_ids):
        try:
            book = clob_client.get_order_book(token_id)
        except Exception as exc:  # network/API hiccups shouldn't crash a scan
            log.warning("order book fetch failed for %s (%s): %s", market.slug, outcome, exc)
            continue
        if not book.asks:
            continue
        best_ask = min(float(level.price) for level in book.asks)
        if cfg["min_entry_price"] <= best_ask <= cfg["max_entry_price"]:
            candidates.append(
                {"token_id": token_id, "outcome": outcome, "ask_price": best_ask, "book": book}
            )
    return candidates


def size_position(broker: PaperBroker, cfg: dict) -> float:
    return min(cfg["max_bet"], broker.balance * cfg["max_bankroll_fraction"])


def run_once(cfg: dict | None = None, broker: PaperBroker | None = None) -> list[dict]:
    cfg = cfg or load_config()
    bot_cfg = cfg["directional_bot"]
    if cfg["mode"] != "paper":
        raise NotImplementedError(
            "live trading is intentionally not implemented yet -- see README"
        )

    broker = broker or PaperBroker(starting_balance=cfg["starting_balance"])
    fills = []

    btc_markets = markets.fetch_btc_markets()
    log.info("scanned %d BTC markets", len(btc_markets))

    for market in btc_markets:
        candidates = find_candidates(market, bot_cfg)
        for candidate in candidates:
            usd_amount = size_position(broker, bot_cfg)
            if usd_amount <= 0:
                log.info("skipping %s: no bankroll available", market.slug)
                continue
            try:
                fill = broker.buy(
                    market_id=market.market_id,
                    token_id=candidate["token_id"],
                    outcome=candidate["outcome"],
                    usd_amount=usd_amount,
                    order_book=candidate["book"],
                )
            except (InsufficientLiquidity, InsufficientBalance) as exc:
                log.info("skipped %s/%s: %s", market.slug, candidate["outcome"], exc)
                continue

            record = journal.log_trade(
                BOT_NAME,
                market_slug=market.slug,
                question=market.question,
                entry_price=candidate["ask_price"],
                seconds_to_resolution=market.seconds_to_resolution(),
                **fill,
            )
            log.info(
                "BUY %s %s @ %.3f ($%.2f) | %s",
                market.slug,
                candidate["outcome"],
                fill["avg_price"],
                fill["cost"],
                market.question,
            )
            fills.append(record)

    if not fills:
        log.info("no candidates found this scan")
    return fills


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot 1: directional BTC up/down trader")
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
