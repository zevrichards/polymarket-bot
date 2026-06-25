"""Bot 1: Directional.

Buys the favored outcome of a BTC up/down market when it's priced in the
$0.85-$0.99 band with a short window left before resolution -- the classic
"buy the near-certain side cheaply, let it settle to $1" trade described in
the source article. Paper-trading only; see README for what's deferred
before this can run live.

Guards added after paper-trading runs exposed real bugs (see
BUILD_INTELLIGENCE_REPORT.md Sessions 4 and 5):

1. Never enter a market we already hold a position in. Without this, the
   bot re-bought into the same market on consecutive scans (pyramiding),
   and in one case bought BOTH outcomes of the same market -- a guaranteed
   loss on the combination once both legs' prices sum to over $1.
2. Cap how many candidates resolving at the *same exact timestamp* get
   traded per scan (within-scan correlation cap). Polymarket lists separate
   "Bitcoin above $X" markets per strike price, all resolving off one
   underlying price observation -- they're not independent bets. Without
   this cap, the bot took 7 simultaneous "No" positions across a strike
   ladder, all correlated to one BTC move, and lost all 7 together when
   price rallied through every strike.
3. Block entering a new market if we already hold a position in ANY market
   sharing the same resolution timestamp (cross-scan correlation cap, via
   PaperBroker.has_open_position_for_event). Guard #2 alone wasn't enough:
   two correlated strikes can each become the *sole* qualifying candidate
   on consecutive 60s-apart scans, so a same-scan-only check never sees
   them together. This was caught on a second run where exactly that
   happened -- two different strikes of the same "11am ET" event were
   bought 57 seconds apart, each the only candidate in its own scan.
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path

from core import clob_client, journal, markets, resolution
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


def select_candidates(
    market_candidates: list[tuple], max_per_event: int
) -> list[tuple]:
    """Given [(market, candidate), ...], cap how many get traded per
    distinct resolution timestamp -- markets sharing an endDate (e.g. a
    ladder of strike prices all settling off one BTC price observation)
    are correlated, not independent, so only the single most-confident
    candidate per timestamp is kept by default (max_per_event=1)."""
    by_event = defaultdict(list)
    for market, candidate in market_candidates:
        by_event[market.end_date].append((market, candidate))

    selected = []
    for end_date, group in by_event.items():
        group.sort(key=lambda mc: mc[1]["ask_price"], reverse=True)
        selected.extend(group[:max_per_event])
        for market, candidate in group[max_per_event:]:
            log.info(
                "skipping %s/%s -- correlated with %d other candidate(s) resolving at %s, "
                "only taking the top %d per event",
                market.slug, candidate["outcome"], len(group) - 1, end_date, max_per_event,
            )
    return selected


def run_once(cfg: dict | None = None, broker: PaperBroker | None = None) -> list[dict]:
    cfg = cfg or load_config()
    bot_cfg = cfg["directional_bot"]
    if cfg["mode"] != "paper":
        raise NotImplementedError(
            "live trading is intentionally not implemented yet -- see README"
        )

    broker = broker or PaperBroker(starting_balance=cfg["starting_balance"])

    resolved = resolution.resolve_broker_positions(broker, BOT_NAME)
    if resolved:
        log.info("settled %d resolved position(s)", len(resolved))

    btc_markets = markets.fetch_btc_markets()
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
            log.info("skipping %s: no bankroll available", market.slug)
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
