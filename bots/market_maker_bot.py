"""Bot 3: Market-making.

Posts a resting bid and ask around the current mid price on a BTC up/down
market, instead of taking a directional position. Each scan:

1. Check whether the live book's best bid/ask has crossed our previously
   resting quote -- if so, simulate a fill (we "would have been hit").
2. Re-quote around the new mid if price moved more than the requote
   threshold.

This is a polling-loop approximation of market making, not real resting
limit orders on the live book (those require authenticated order
placement, which is out of scope for the paper-trading phase -- see
README). It's still a meaningfully different state machine from Bots 1/2:
it tracks open quotes and inventory across scans rather than taking one-shot
positions.

Inventory is long-only and capped at `max_inventory` shares per market --
we never go short, since shorting a binary outcome token via this bot's
simple quote model isn't well-defined.

Correlated-event cap (added after a live run showed this bot had built up
inventory across 18 different "Bitcoin above $X" strikes all resolving off
the SAME underlying price observation -- the same risk pattern fixed in
Bots 1/2 via has_open_position_for_event, just showing up here through
accumulated quote inventory instead of one-shot buys). `max_inventory`
alone only caps risk *per market*; it does nothing to stop the bot from
quoting an entire strike ladder and ending up with significant aggregate
exposure to one BTC price at one timestamp. `max_inventory_per_event` caps
total inventory across all markets sharing a resolution timestamp: new
quotes aren't created once the event is already at cap, and further BUY
fills (which increase inventory) are refused past the cap -- SELL fills
(which reduce inventory) are never blocked, since exiting risk is always
fine.

Order-flow imbalance guard (Session 8, unverified -- see
BUILD_INTELLIGENCE_REPORT.md). A clean paper-trading run showed this bot's
real win rate, on fills it actually took, was only 12.4% -- the textbook
signature of adverse selection: resting buy quotes kept getting filled
right before price moved further against them. core/orderflow.py computes
book imbalance (more resting size on the ask side suggests selling
pressure / price about to drift down); `min_imbalance_to_buy` refuses a
BUY fill when the book is signaling that pressure, on the theory that this
is exactly the moment a static quote is most likely to be picked off.
SELL fills are still never blocked. This is a hypothesis, not a confirmed
fix -- needs the same before/after comparison done for the spread/timing
tuning in Session 7.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core import clob_client, journal, markets as markets_module, orderflow, resolution

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
MM_STATE_PATH = Path(__file__).resolve().parent.parent / "logs" / "mm_state.json"
BOT_NAME = "market_maker_bot"
MIN_SECONDS_TO_RESOLUTION = 120  # stop quoting well before resolution -- a 92-trade live sample
# showed avg inventory settled on wins = 0.00 vs 0.65 on losses, the signature of adverse selection:
# resting quotes get picked off by informed/faster flow specifically as price converges toward the
# true outcome near resolution. 30s wasn't enough margin; widened to 120s (see BUILD_INTELLIGENCE_REPORT.md).

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(BOT_NAME)


@dataclass
class Quote:
    bid_price: float
    ask_price: float
    inventory: float = 0.0
    cash: float = 0.0  # net cash flow from fills on this market (negative = spent)
    market_id: str | None = None  # needed to check resolution; None on old/pre-upgrade state
    outcome: str | None = None
    event_key: str | None = None  # market.end_date.isoformat() -- shared by correlated strikes


@dataclass
class MMState:
    quotes: dict[str, Quote] = field(default_factory=dict)  # keyed by token_id

    def to_json(self) -> dict:
        return {k: asdict(v) for k, v in self.quotes.items()}

    @classmethod
    def from_json(cls, data: dict) -> "MMState":
        return cls(quotes={k: Quote(**v) for k, v in data.items()})


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def load_state() -> MMState:
    if MM_STATE_PATH.exists():
        with MM_STATE_PATH.open(encoding="utf-8") as f:
            return MMState.from_json(json.load(f))
    return MMState()


def save_state(state: MMState) -> None:
    MM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MM_STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state.to_json(), f, indent=2)


def best_bid_ask(book) -> tuple[float | None, float | None]:
    best_bid = max((float(level.price) for level in book.bids), default=None)
    best_ask = min((float(level.price) for level in book.asks), default=None)
    return best_bid, best_ask


def event_inventory(state: "MMState", event_key: str | None, exclude_token_id: str | None = None) -> float:
    """Total inventory currently held across all markets sharing this
    resolution event (e.g. a strike ladder all settling off one BTC price
    observation at one timestamp)."""
    if not event_key:
        return 0.0
    return sum(
        q.inventory
        for tid, q in state.quotes.items()
        if q.event_key == event_key and tid != exclude_token_id
    )


def check_fills(
    token_id: str,
    quote: Quote,
    best_bid: float,
    best_ask: float,
    cfg: dict,
    event_inventory_before_this_quote: float = 0.0,
    imbalance: float | None = None,
) -> dict | None:
    """If the live book crossed our resting quote, simulate the fill."""
    quote_size = cfg["quote_size"]
    max_per_event = cfg.get("max_inventory_per_event", float("inf"))
    min_imbalance_to_buy = cfg.get("min_imbalance_to_buy", float("-inf"))

    # Our ask gets hit if someone is willing to sell into the book at/below our ask
    # (i.e. the live best bid is >= our ask -- we'd be the best available buyer... )
    # For a binary outcome token bought from 0-1, "hit" means the live best bid
    # reaches our ask price (someone sells to us is the wrong direction for an
    # ask -- here we simulate the simpler, conservative case: our resting ask is
    # filled when the market's best bid price meets or exceeds it).
    # SELL fills (reducing inventory) are never blocked by the event cap or
    # the imbalance guard -- exiting risk is always allowed.
    if quote.inventory > 0 and best_bid >= quote.ask_price:
        size = min(quote_size, quote.inventory)
        quote.inventory -= size
        quote.cash += size * quote.ask_price
        return {"side": "ask_filled", "price": quote.ask_price, "size": size}

    # Our bid gets hit when the market's best ask price meets or undercuts it.
    # BUY fills (increasing inventory) ARE blocked by: the event-level cap
    # (correlated-risk guard), and now also the order-flow imbalance guard --
    # refuse to buy when the book signals selling pressure (more resting
    # size on the ask side), since that's exactly when a static quote is
    # most likely to be picked off by price continuing to move against it.
    if best_ask <= quote.bid_price:
        if event_inventory_before_this_quote + quote.inventory >= max_per_event:
            return None
        if imbalance is not None and imbalance < min_imbalance_to_buy:
            return None
        size = quote_size
        quote.inventory += size
        quote.cash -= size * quote.bid_price
        return {"side": "bid_filled", "price": quote.bid_price, "size": size}

    return None


def should_requote(quote: Quote, mid: float, cfg: dict) -> bool:
    current_mid = (quote.bid_price + quote.ask_price) / 2
    return abs(mid - current_mid) / current_mid > cfg["requote_threshold"]


def make_quote(
    mid: float,
    cfg: dict,
    inventory: float = 0.0,
    cash: float = 0.0,
    market_id: str | None = None,
    outcome: str | None = None,
    event_key: str | None = None,
) -> Quote:
    half_spread = cfg["target_spread"] / 2
    return Quote(
        bid_price=round(max(0.01, mid - half_spread), 2),
        ask_price=round(min(0.99, mid + half_spread), 2),
        inventory=inventory,
        cash=cash,
        market_id=market_id,
        outcome=outcome,
        event_key=event_key,
    )


def resolve_quotes(state: MMState, bot_name: str = BOT_NAME) -> list[dict]:
    """Settle any resolved market's remaining inventory at $1/$0 and drop
    the quote -- no point continuing to track a market that's over.
    Quotes created before this field existed have market_id=None and can't
    be resolved; they're left alone (will just stop getting requoted once
    fetch_btc_markets() no longer returns their now-expired market).
    """
    results = []
    for token_id, quote in list(state.quotes.items()):
        if quote.market_id is None:
            continue
        won = resolution.check_token_resolution(quote.market_id, token_id)
        if won is None:
            continue

        payout = quote.inventory * (1.0 if won else 0.0)
        quote.cash += payout
        pnl = quote.cash  # cash already nets all buy/sell fills on this market

        record = journal.log_trade(
            bot_name,
            kind="resolution",
            market_id=quote.market_id,
            outcome=quote.outcome,
            token_id=token_id,
            inventory_settled=quote.inventory,
            won=won,
            payout=payout,
            pnl=pnl,
        )
        log.info(
            "RESOLVED %s/%s | won=%s inventory=%.2f payout=$%.2f pnl=%+.2f",
            quote.market_id, quote.outcome, won, quote.inventory, payout, pnl,
        )
        results.append(record)
        del state.quotes[token_id]

    return results


def run_once(cfg: dict | None = None, state: MMState | None = None) -> list[dict]:
    cfg = cfg or load_config()
    bot_cfg = cfg["market_maker_bot"]
    if cfg["mode"] != "paper":
        raise NotImplementedError(
            "live trading is intentionally not implemented yet -- see README"
        )

    state = state if state is not None else load_state()
    events = []

    resolved = resolve_quotes(state)
    if resolved:
        log.info("settled %d resolved quote(s)", len(resolved))
        save_state(state)

    btc_markets = markets_module.fetch_btc_markets()
    log.info("scanned %d BTC markets", len(btc_markets))

    for market in btc_markets:
        seconds_left = market.seconds_to_resolution()
        if seconds_left is None or seconds_left < MIN_SECONDS_TO_RESOLUTION:
            continue

        for outcome, token_id in zip(market.outcomes, market.token_ids):
            try:
                book = clob_client.get_order_book(token_id)
            except Exception as exc:
                log.warning("order book fetch failed for %s/%s: %s", market.slug, outcome, exc)
                continue

            best_bid, best_ask = best_bid_ask(book)
            if best_bid is None or best_ask is None:
                continue
            mid = (best_bid + best_ask) / 2

            event_key = market.end_date.isoformat()
            max_per_event = bot_cfg.get("max_inventory_per_event", float("inf"))

            quote = state.quotes.get(token_id)
            if quote is None:
                if event_inventory(state, event_key) >= max_per_event:
                    log.info(
                        "%s/%s: skipping new quote -- event %s already at inventory cap",
                        market.slug, outcome, event_key,
                    )
                    continue
                quote = make_quote(mid, bot_cfg, market_id=market.market_id, outcome=outcome, event_key=event_key)
                state.quotes[token_id] = quote
                log.info("%s/%s: new quote bid=%.2f ask=%.2f", market.slug, outcome, quote.bid_price, quote.ask_price)
                continue

            inventory_elsewhere = event_inventory(state, event_key, exclude_token_id=token_id)
            imbalance = orderflow.compute_imbalance(book)
            fill = check_fills(token_id, quote, best_bid, best_ask, bot_cfg, inventory_elsewhere, imbalance)
            if fill:
                record = journal.log_trade(
                    BOT_NAME,
                    market_slug=market.slug,
                    outcome=outcome,
                    token_id=token_id,
                    inventory_after=quote.inventory,
                    cash_after=quote.cash,
                    imbalance_at_fill=imbalance,
                    **fill,
                )
                log.info("%s/%s: %s @ %.2f size=%.2f", market.slug, outcome, fill["side"], fill["price"], fill["size"])
                events.append(record)

            if quote.inventory < bot_cfg["max_inventory"] and should_requote(quote, mid, bot_cfg):
                new_quote = make_quote(
                    mid, bot_cfg,
                    inventory=quote.inventory, cash=quote.cash,
                    market_id=quote.market_id or market.market_id, outcome=quote.outcome or outcome,
                    event_key=quote.event_key or event_key,
                )
                state.quotes[token_id] = new_quote
                log.info(
                    "%s/%s: requote bid=%.2f ask=%.2f (mid moved to %.3f)",
                    market.slug, outcome, new_quote.bid_price, new_quote.ask_price, mid,
                )

    save_state(state)
    if not events:
        log.info("no fills this scan")
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot 3: BTC up/down market maker")
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
