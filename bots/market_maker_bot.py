"""Bot 3: Market-making (Session 14 rewrite -- real-time WebSocket + Avellaneda-Stoikov pricing).

The original version (REST-polling every 60s, naive symmetric quotes
around mid, ~40-90 markets tracked at once) lost money across three
separate diagnostic sessions (7-9) and never recovered even after tuning
spread/timing and adding an order-flow imbalance gate. Two root causes,
both addressed here:

1. SPEED. A quote that's up to 60 seconds stale gets picked off by anyone
   with fresher information, no matter how well it's priced. Fixed by
   core/live_orderbook.py -- a persistent WebSocket connection to
   Polymarket's public CLOB market channel, pushing book updates in real
   time (confirmed via direct testing: many updates per second on an
   active market) instead of polling REST once a minute.
2. PRICING. The old bot quoted a fixed spread symmetrically around mid,
   regardless of current inventory or how much time/volatility was left.
   Fixed by core/marketmaking.py -- an Avellaneda-Stoikov-style
   reservation price that skews away from mid based on current inventory
   (so the bot actively works to flatten itself) and a spread that widens
   with volatility and time-to-resolution instead of being flat.

Scope is also deliberately narrowed: the old bot tracked every BTC market
simultaneously, which is *why* a polling cycle took 60+ seconds to begin
with. This version tracks only the `max_tracked_markets` soonest-resolving
markets at a time, both because that's what genuinely fast-reacting market
making requires and because it removes the original cause of slowness.

Volatility is estimated from the live Polymarket mid-price itself (a
rolling per-tick history), not from Binance -- this bot cares about the
volatility of the contract it's actually quoting, not BTC's spot price
directly.

UNVERIFIED. This is a full rewrite, not a confirmed fix -- it needs the
same real-data scrutiny every other bot in this project got before
trusting any result from it.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core import journal, markets as markets_module, resolution
from core.live_orderbook import LiveOrderBook
from core.marketmaking import compute_quotes, estimate_contract_volatility

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
MM_STATE_PATH = Path(__file__).resolve().parent.parent / "logs" / "mm_state.json"
BOT_NAME = "market_maker_bot"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(BOT_NAME)


@dataclass
class Position:
    market_id: str
    outcome: str
    event_key: str
    inventory: float = 0.0
    cash: float = 0.0  # net cash flow from fills on this market (negative = spent)
    last_bid: float = 0.0  # most recently computed quote, kept for logging/debugging only
    last_ask: float = 0.0


@dataclass
class MMState:
    positions: dict[str, Position] = field(default_factory=dict)  # token_id -> Position

    def to_json(self) -> dict:
        return {k: asdict(v) for k, v in self.positions.items()}

    @classmethod
    def from_json(cls, data: dict) -> "MMState":
        return cls(positions={k: Position(**v) for k, v in data.items()})


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


def event_inventory(state: MMState, event_key: str, exclude_token_id: str | None = None) -> float:
    """Total inventory currently held across all markets sharing this
    resolution event (e.g. a strike ladder all settling off one BTC price
    observation at one timestamp) -- see has_open_position_for_event in
    core/paper_broker.py for the original version of this guard."""
    if not event_key:
        return 0.0
    return sum(
        p.inventory for tid, p in state.positions.items()
        if p.event_key == event_key and tid != exclude_token_id
    )


def select_tracked_markets(cfg: dict, max_count: int) -> list:
    """Picks the soonest-resolving qualifying BTC markets to track.
    Narrowing scope to a handful of markets (rather than every BTC market
    Gamma returns) is what makes real-time, tight-tick-loop quoting
    actually feasible -- see module docstring."""
    btc_markets = markets_module.fetch_btc_markets()
    candidates = [
        m for m in btc_markets
        if m.seconds_to_resolution() is not None
        and cfg["min_seconds_to_resolution"] <= m.seconds_to_resolution() <= cfg["max_seconds_to_resolution"]
    ]
    candidates.sort(key=lambda m: m.seconds_to_resolution())
    return candidates[:max_count]


def resolve_positions(state: MMState) -> list[dict]:
    results = []
    for token_id, pos in list(state.positions.items()):
        won = resolution.check_token_resolution(pos.market_id, token_id)
        if won is None:
            continue

        payout = pos.inventory * (1.0 if won else 0.0)
        pos.cash += payout
        pnl = pos.cash  # cash already nets all buy/sell fills on this market

        record = journal.log_trade(
            BOT_NAME, kind="resolution", market_id=pos.market_id, outcome=pos.outcome,
            token_id=token_id, inventory_settled=pos.inventory, won=won, payout=payout, pnl=pnl,
        )
        log.info(
            "RESOLVED %s/%s | won=%s inventory=%.2f payout=$%.2f pnl=%+.2f",
            pos.market_id, pos.outcome, won, pos.inventory, payout, pnl,
        )
        results.append(record)
        del state.positions[token_id]

    return results


def check_fill(
    pos: Position,
    live_bid: float,
    live_ask: float,
    our_bid: float,
    our_ask: float,
    quote_size: float,
    max_per_event: float,
    event_inventory_elsewhere: float,
    max_inventory: float = float("inf"),
) -> dict | None:
    """If the live market crossed our computed quote, simulate the fill.
    SELL fills (reducing inventory) are never blocked -- exiting risk is
    always allowed. BUY fills are blocked once the correlated-event
    inventory cap OR the per-market max_inventory cap is reached.

    max_inventory is checked explicitly here, not just relied on via
    compute_quotes' inventory clamping -- caught live (Session 14 smoke
    test): when volatility spikes (e.g. a contract price frozen near a
    $0/$1 boundary, where the old log-return sigma estimator blew up),
    the AS pricing formula can degrade to a near-mid fallback quote that
    keeps getting filled every tick, with nothing in the pricing math
    itself reliably stopping it. A real bot needs a hard cap that doesn't
    depend on the pricing model behaving -- this is that cap.
    """
    if pos.inventory > 0 and live_bid >= our_ask:
        size = min(quote_size, pos.inventory)
        pos.inventory -= size
        pos.cash += size * our_ask
        return {"side": "ask_filled", "price": our_ask, "size": size}

    if live_ask <= our_bid:
        if pos.inventory >= max_inventory:
            return None
        if event_inventory_elsewhere + pos.inventory >= max_per_event:
            return None
        size = quote_size
        pos.inventory += size
        pos.cash -= size * our_bid
        return {"side": "bid_filled", "price": our_bid, "size": size}

    return None


def tick(cfg: dict, ws_book: LiveOrderBook, state: MMState, tracked: list, mid_history: dict) -> list[dict]:
    """One pass over all currently-tracked markets: read the live book,
    recompute our quote fresh (reservation price + AS spread), check for
    a fill against the live best bid/ask. Called once per
    tick_interval_seconds, much tighter than the old 60s scan."""
    bot_cfg = cfg["market_maker_bot"]
    events = []

    for market in tracked:
        seconds_left = market.seconds_to_resolution()
        if seconds_left is None or seconds_left <= 0:
            continue
        event_key = market.end_date.isoformat()

        for outcome, token_id in zip(market.outcomes, market.token_ids):
            live_bid, live_ask = ws_book.best_bid_ask(token_id)
            if live_bid is None or live_ask is None:
                continue
            mid = (live_bid + live_ask) / 2

            hist = mid_history.setdefault(token_id, [])
            hist.append(mid)
            if len(hist) > bot_cfg["vol_lookback_ticks"]:
                hist.pop(0)
            sigma = estimate_contract_volatility(hist) or 0.005  # floor avoids a degenerate zero spread on a cold start

            pos = state.positions.get(token_id)
            if pos is None:
                if event_inventory(state, event_key) >= bot_cfg.get("max_inventory_per_event", float("inf")):
                    continue
                pos = Position(market_id=market.market_id, outcome=outcome, event_key=event_key)
                state.positions[token_id] = pos

            our_bid, our_ask = compute_quotes(
                mid, pos.inventory, bot_cfg["gamma"], sigma, seconds_left, bot_cfg["k"],
                min_spread=bot_cfg["min_spread"], max_inventory=bot_cfg["max_inventory"],
            )
            pos.last_bid, pos.last_ask = our_bid, our_ask

            inv_elsewhere = event_inventory(state, event_key, exclude_token_id=token_id)
            fill = check_fill(
                pos, live_bid, live_ask, our_bid, our_ask, bot_cfg["quote_size"],
                bot_cfg.get("max_inventory_per_event", float("inf")), inv_elsewhere,
                max_inventory=bot_cfg["max_inventory"],
            )
            if fill:
                record = journal.log_trade(
                    BOT_NAME, market_slug=market.slug, outcome=outcome, token_id=token_id,
                    inventory_after=pos.inventory, cash_after=pos.cash, sigma=sigma,
                    our_bid=our_bid, our_ask=our_ask, **fill,
                )
                log.info(
                    "%s/%s: %s @ %.3f size=%.2f (inv=%.2f)",
                    market.slug, outcome, fill["side"], fill["price"], fill["size"], pos.inventory,
                )
                events.append(record)

    return events


def _refresh_tracked(bot_cfg: dict, ws_book: LiveOrderBook, state: MMState, tracked: list) -> list:
    resolved = resolve_positions(state)
    if resolved:
        log.info("settled %d resolved position(s)", len(resolved))
        save_state(state)

    new_tracked = select_tracked_markets(bot_cfg, bot_cfg["max_tracked_markets"])
    new_ids = {tid for m in new_tracked for tid in m.token_ids}
    old_ids = {tid for m in tracked for tid in m.token_ids}

    to_subscribe = new_ids - old_ids
    to_unsubscribe = old_ids - new_ids
    if to_subscribe:
        ws_book.subscribe(list(to_subscribe))
        log.info("now tracking: %s", [m.slug for m in new_tracked])
    if to_unsubscribe:
        ws_book.unsubscribe(list(to_unsubscribe))

    return new_tracked


def run_loop(cfg: dict | None = None) -> None:
    cfg = cfg or load_config()
    bot_cfg = cfg["market_maker_bot"]
    if cfg["mode"] != "paper":
        raise NotImplementedError("live trading is intentionally not implemented yet -- see README")

    state = load_state()
    ws_book = LiveOrderBook()
    ws_book.start()
    log.info("%s: WebSocket client starting, tick every %ss, refreshing tracked markets every %ss",
              BOT_NAME, bot_cfg["tick_interval_seconds"], bot_cfg["refresh_interval_seconds"])

    mid_history: dict[str, list[float]] = {}
    tracked: list = []
    last_refresh = 0.0

    try:
        while True:
            try:
                now = time.time()
                if now - last_refresh >= bot_cfg["refresh_interval_seconds"]:
                    tracked = _refresh_tracked(bot_cfg, ws_book, state, tracked)
                    last_refresh = now

                events = tick(cfg, ws_book, state, tracked, mid_history)
                if events:
                    save_state(state)
            except KeyboardInterrupt:
                raise
            except Exception:
                # Session 16: this loop had no equivalent of
                # core/scheduler.py's catch-log-retry resilience -- a
                # single uncaught exception (e.g. a transient Gamma
                # 403/timeout) would silently kill the entire process.
                # Confirmed live in lag_bot's identical gap; fixed here too
                # since this loop has the exact same structure.
                log.exception("%s: tick failed, will retry next interval", BOT_NAME)

            time.sleep(bot_cfg["tick_interval_seconds"])
    except KeyboardInterrupt:
        log.info("%s: shutting down", BOT_NAME)
    finally:
        save_state(state)
        ws_book.stop()


def run_smoke_test(cfg: dict | None = None, duration_seconds: int = 30) -> None:
    """--once equivalent for a continuously-ticking bot: run the real
    loop for a short fixed duration, then exit, instead of one-shot."""
    cfg = cfg or load_config()
    bot_cfg = cfg["market_maker_bot"]
    if cfg["mode"] != "paper":
        raise NotImplementedError("live trading is intentionally not implemented yet -- see README")

    state = load_state()
    ws_book = LiveOrderBook()
    ws_book.start()

    resolved = resolve_positions(state)
    if resolved:
        log.info("settled %d resolved position(s)", len(resolved))

    tracked = select_tracked_markets(bot_cfg, bot_cfg["max_tracked_markets"])
    log.info("tracking %d market(s): %s", len(tracked), [m.slug for m in tracked])
    ws_book.subscribe([tid for m in tracked for tid in m.token_ids])

    mid_history: dict[str, list[float]] = {}
    start = time.time()
    while time.time() - start < duration_seconds:
        tick(cfg, ws_book, state, tracked, mid_history)
        time.sleep(bot_cfg["tick_interval_seconds"])

    save_state(state)
    ws_book.stop()
    log.info("smoke test complete")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot 3: BTC up/down market maker (real-time)")
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
