"""Bot 4 risk controls (Kelly sizing, stop-loss, daily cap, liquidity
filter) -- fixture-based, no network."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bots.lag_bot import (
    book_depth_usd,
    check_stop_losses,
    count_todays_entries,
    size_position,
)
from core.paper_broker import PaperBroker, Position

BOT_CFG = {
    "max_bet": 2.0,
    "max_bankroll_fraction": 0.5,  # loose, so Kelly is the binding constraint in these tests
    "kelly_fraction": 0.25,
}


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
    # model thinks LESS likely than market -- no business buying this side
    candidate = {"market_p": 0.60, "model_p": 0.40}
    assert size_position(broker, candidate, BOT_CFG) == 0.0


def test_size_position_capped_by_max_bet():
    broker = SimpleNamespace(balance=10_000.0)
    candidate = {"market_p": 0.10, "model_p": 0.95}  # huge edge
    assert size_position(broker, candidate, BOT_CFG) == BOT_CFG["max_bet"]


def test_size_position_handles_degenerate_price():
    broker = SimpleNamespace(balance=100.0)
    assert size_position(broker, {"market_p": 0.0, "model_p": 0.5}, BOT_CFG) == 0.0
    assert size_position(broker, {"market_p": 1.0, "model_p": 0.5}, BOT_CFG) == 0.0


def test_book_depth_usd_sums_both_sides():
    book = make_book(bids=[(0.40, 10)], asks=[(0.60, 5)])
    assert book_depth_usd(book) == pytest.approx(0.40 * 10 + 0.60 * 5)


def test_check_stop_losses_exits_position_beyond_threshold(tmp_path):
    broker = PaperBroker(starting_balance=100.0, state_path=tmp_path / "state.json")
    broker.state.positions["t1"] = Position(
        market_id="m1", token_id="t1", outcome="Up", shares=10.0, avg_price=0.50
    )
    cfg = {"stop_loss_pct": 0.3}  # exit if down 30%+

    book = make_book(bids=[(0.30, 10)], asks=[(0.35, 10)])  # down 40% from 0.50
    with patch("bots.lag_bot.clob_client.get_order_book", return_value=book):
        results = check_stop_losses(broker, cfg)

    assert len(results) == 1
    assert "t1" not in broker.state.positions
    assert results[0]["pnl"] < 0


def test_check_stop_losses_leaves_position_under_threshold(tmp_path):
    broker = PaperBroker(starting_balance=100.0, state_path=tmp_path / "state.json")
    broker.state.positions["t1"] = Position(
        market_id="m1", token_id="t1", outcome="Up", shares=10.0, avg_price=0.50
    )
    cfg = {"stop_loss_pct": 0.3}

    book = make_book(bids=[(0.45, 10)], asks=[(0.50, 10)])  # only down 10%
    with patch("bots.lag_bot.clob_client.get_order_book", return_value=book):
        results = check_stop_losses(broker, cfg)

    assert results == []
    assert "t1" in broker.state.positions


def test_check_stop_losses_disabled_when_not_configured(tmp_path):
    broker = PaperBroker(starting_balance=100.0, state_path=tmp_path / "state.json")
    broker.state.positions["t1"] = Position(
        market_id="m1", token_id="t1", outcome="Up", shares=10.0, avg_price=0.50
    )
    assert check_stop_losses(broker, {}) == []
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
