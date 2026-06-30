"""Bot 4: risk controls (Kelly sizing, stop-loss, daily cap, liquidity
filter) and Session 15's edge-persistence logic -- fixture-based, no
network."""
from types import SimpleNamespace

import pytest

from bots.lag_bot import (
    check_stop_losses,
    confirm_and_build_candidate,
    count_todays_entries,
    edge_confirmed,
    load_stopout_blacklist,
    size_position,
)
from core.paper_broker import PaperBroker, Position

BOT_CFG = {
    "max_bet": 2.0,
    "max_bankroll_fraction": 0.5,  # loose, so Kelly is the binding constraint in these tests
    "kelly_fraction": 0.25,
}


class FakeLiveOrderBook:
    """Minimal test double matching LiveOrderBook's interface, with data
    set directly instead of via WebSocket messages."""

    def __init__(self):
        self._best: dict[str, tuple[float, float]] = {}
        self._depth: dict[str, float] = {}
        self._books: dict[str, object] = {}

    def set(self, token_id: str, bid: float, ask: float, depth: float = 1000.0):
        self._best[token_id] = (bid, ask)
        self._depth[token_id] = depth
        self._books[token_id] = SimpleNamespace(
            bids=[SimpleNamespace(price=str(bid), size="1000")],
            asks=[SimpleNamespace(price=str(ask), size="1000")],
        )

    def best_bid_ask(self, token_id, max_age_seconds=30.0):
        return self._best.get(token_id, (None, None))

    def depth_usd(self, token_id, max_age_seconds=60.0):
        return self._depth.get(token_id)

    def get_book(self, token_id, max_age_seconds=60.0):
        return self._books.get(token_id)


def make_book(bids, asks):
    return SimpleNamespace(
        bids=[SimpleNamespace(price=str(p), size=str(s)) for p, s in bids],
        asks=[SimpleNamespace(price=str(p), size=str(s)) for p, s in asks],
    )


def test_size_position_scales_with_edge():
    broker = SimpleNamespace(balance=100.0)
    cfg = {**BOT_CFG, "max_bet": 1000.0}  # remove the cap so Kelly scaling is visible
    small_edge = {"market_p": 0.50, "model_p": 0.55}
    large_edge = {"market_p": 0.50, "model_p": 0.80}

    small = size_position(broker, small_edge, cfg)
    large = size_position(broker, large_edge, cfg)

    assert 0 < small < large


def test_size_position_zero_when_model_disagrees_wrong_direction():
    broker = SimpleNamespace(balance=100.0)
    candidate = {"market_p": 0.60, "model_p": 0.40}
    assert size_position(broker, candidate, BOT_CFG) == 0.0


def test_size_position_capped_by_max_bet():
    broker = SimpleNamespace(balance=10_000.0)
    candidate = {"market_p": 0.10, "model_p": 0.95}
    assert size_position(broker, candidate, BOT_CFG) == BOT_CFG["max_bet"]


def test_size_position_handles_degenerate_price():
    broker = SimpleNamespace(balance=100.0)
    assert size_position(broker, {"market_p": 0.0, "model_p": 0.5}, BOT_CFG) == 0.0
    assert size_position(broker, {"market_p": 1.0, "model_p": 0.5}, BOT_CFG) == 0.0


# --- edge persistence (Session 15) ---

def test_edge_confirmed_requires_full_history_length():
    assert edge_confirmed([0.1, 0.1], min_edge=0.07, min_consecutive=3) is False


def test_edge_confirmed_true_when_all_recent_ticks_agree_up():
    assert edge_confirmed([0.08, 0.09, 0.10], min_edge=0.07, min_consecutive=3) is True


def test_edge_confirmed_true_when_all_recent_ticks_agree_down():
    assert edge_confirmed([-0.08, -0.09, -0.10], min_edge=0.07, min_consecutive=3) is True


def test_edge_confirmed_false_on_flicker():
    # Mixed signs -- a real flicker, not a persistent disagreement.
    assert edge_confirmed([0.10, -0.10, 0.10], min_edge=0.07, min_consecutive=3) is False


def test_edge_confirmed_false_when_one_tick_drops_below_threshold():
    assert edge_confirmed([0.10, 0.10, 0.03], min_edge=0.07, min_consecutive=3) is False


def test_edge_confirmed_only_looks_at_most_recent_window():
    # An old confirming run followed by a non-confirming tail should NOT
    # count -- only the most recent min_consecutive ticks matter.
    assert edge_confirmed([0.10, 0.10, 0.10, 0.01], min_edge=0.07, min_consecutive=3) is False


# --- confirm_and_build_candidate re-checks live price at confirmation time ---

def test_confirm_and_build_candidate_builds_up_candidate():
    ws_book = FakeLiveOrderBook()
    ws_book.set("up_token", bid=0.40, ask=0.42, depth=500.0)
    market = SimpleNamespace(market_id="m1", slug="s1")
    signal = {
        "up_token": "up_token", "down_token": "down_token",
        "model_p_up": 0.55, "market_p_up": 0.41, "up_edge": 0.14,
        "baseline_price": 60000, "current_price": 60100, "sigma_per_second": 0.001,
    }
    cfg = {"min_edge": 0.07, "entry_price_range": [0.05, 0.95], "min_book_depth_usd": 50.0}

    candidate = confirm_and_build_candidate(market, signal, cfg, ws_book)

    assert candidate is not None
    assert candidate["outcome"] == "Up"
    assert candidate["token_id"] == "up_token"


def test_confirm_and_build_candidate_builds_down_candidate():
    ws_book = FakeLiveOrderBook()
    ws_book.set("down_token", bid=0.20, ask=0.22, depth=500.0)
    market = SimpleNamespace(market_id="m1", slug="s1")
    signal = {
        "up_token": "up_token", "down_token": "down_token",
        "model_p_up": 0.10, "market_p_up": 0.79, "up_edge": -0.69,
        "baseline_price": 60000, "current_price": 59900, "sigma_per_second": 0.001,
    }
    cfg = {"min_edge": 0.07, "entry_price_range": [0.05, 0.95], "min_book_depth_usd": 50.0}

    candidate = confirm_and_build_candidate(market, signal, cfg, ws_book)

    assert candidate is not None
    assert candidate["outcome"] == "Down"
    assert candidate["model_p"] == pytest.approx(0.90)


def test_confirm_and_build_candidate_none_if_edge_vanished_on_recheck():
    # Signal said the edge was there, but the live price has since moved
    # to erase it -- must not chase a stale confirmation.
    ws_book = FakeLiveOrderBook()
    ws_book.set("up_token", bid=0.54, ask=0.56, depth=500.0)  # market caught up to model
    market = SimpleNamespace(market_id="m1", slug="s1")
    signal = {
        "up_token": "up_token", "down_token": "down_token",
        "model_p_up": 0.55, "market_p_up": 0.41, "up_edge": 0.14,
        "baseline_price": 60000, "current_price": 60100, "sigma_per_second": 0.001,
    }
    cfg = {"min_edge": 0.07, "entry_price_range": [0.05, 0.95], "min_book_depth_usd": 50.0}

    candidate = confirm_and_build_candidate(market, signal, cfg, ws_book)

    assert candidate is None


def test_confirm_and_build_candidate_none_if_liquidity_too_thin():
    ws_book = FakeLiveOrderBook()
    ws_book.set("up_token", bid=0.40, ask=0.42, depth=5.0)  # below min_book_depth_usd
    market = SimpleNamespace(market_id="m1", slug="s1")
    signal = {
        "up_token": "up_token", "down_token": "down_token",
        "model_p_up": 0.55, "market_p_up": 0.41, "up_edge": 0.14,
        "baseline_price": 60000, "current_price": 60100, "sigma_per_second": 0.001,
    }
    cfg = {"min_edge": 0.07, "entry_price_range": [0.05, 0.95], "min_book_depth_usd": 50.0}

    assert confirm_and_build_candidate(market, signal, cfg, ws_book) is None


def test_confirm_and_build_candidate_none_outside_price_range():
    ws_book = FakeLiveOrderBook()
    ws_book.set("up_token", bid=0.97, ask=0.98, depth=500.0)  # already near-certain, no edge left to capture
    market = SimpleNamespace(market_id="m1", slug="s1")
    signal = {
        "up_token": "up_token", "down_token": "down_token",
        "model_p_up": 0.999, "market_p_up": 0.975, "up_edge": 0.024,
        "baseline_price": 60000, "current_price": 60500, "sigma_per_second": 0.001,
    }
    cfg = {"min_edge": 0.02, "entry_price_range": [0.05, 0.95], "min_book_depth_usd": 50.0}

    assert confirm_and_build_candidate(market, signal, cfg, ws_book) is None


# --- stop-loss (now reads the live WebSocket book instead of REST) ---

def test_check_stop_losses_exits_position_beyond_threshold(tmp_path):
    broker = PaperBroker(starting_balance=100.0, state_path=tmp_path / "state.json")
    broker.state.positions["t1"] = Position(
        market_id="m1", token_id="t1", outcome="Up", shares=10.0, avg_price=0.50
    )
    cfg = {"stop_loss_pct": 0.3}
    ws_book = FakeLiveOrderBook()
    ws_book.set("t1", bid=0.30, ask=0.35)  # down 40% from 0.50

    import bots.lag_bot as lag_bot_module
    orig_path = lag_bot_module.STOPOUT_BLACKLIST_PATH
    lag_bot_module.STOPOUT_BLACKLIST_PATH = tmp_path / "blacklist.json"
    try:
        results = check_stop_losses(broker, cfg, ws_book)
        blacklist = load_stopout_blacklist()
    finally:
        lag_bot_module.STOPOUT_BLACKLIST_PATH = orig_path

    assert len(results) == 1
    assert "t1" not in broker.state.positions
    assert results[0]["pnl"] < 0
    assert "m1" in blacklist


def test_check_stop_losses_leaves_position_under_threshold(tmp_path):
    broker = PaperBroker(starting_balance=100.0, state_path=tmp_path / "state.json")
    broker.state.positions["t1"] = Position(
        market_id="m1", token_id="t1", outcome="Up", shares=10.0, avg_price=0.50
    )
    cfg = {"stop_loss_pct": 0.3}
    ws_book = FakeLiveOrderBook()
    ws_book.set("t1", bid=0.45, ask=0.50)  # only down 10%

    results = check_stop_losses(broker, cfg, ws_book)

    assert results == []
    assert "t1" in broker.state.positions


def test_check_stop_losses_disabled_when_not_configured(tmp_path):
    broker = PaperBroker(starting_balance=100.0, state_path=tmp_path / "state.json")
    broker.state.positions["t1"] = Position(
        market_id="m1", token_id="t1", outcome="Up", shares=10.0, avg_price=0.50
    )
    assert check_stop_losses(broker, {}, FakeLiveOrderBook()) == []
    assert "t1" in broker.state.positions


def test_count_todays_entries(monkeypatch, tmp_path):
    from core import journal as journal_module

    monkeypatch.setattr(journal_module, "LOG_DIR", tmp_path)
    monkeypatch.setattr(journal_module, "TRADES_PATH", tmp_path / "trades.jsonl")

    journal_module.log_trade("lag_bot", kind="entry", market_slug="m1")
    journal_module.log_trade("lag_bot", kind="entry", market_slug="m2")
    journal_module.log_trade("lag_bot", kind="resolution", market_slug="m1")  # not an entry
    journal_module.log_trade("other_bot", kind="entry", market_slug="m3")  # different bot

    assert count_todays_entries("lag_bot") == 2
