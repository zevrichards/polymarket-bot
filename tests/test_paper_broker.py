import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.paper_broker import InsufficientBalance, InsufficientLiquidity, PaperBroker

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_orderbook.json"


def load_fixture_book():
    data = json.loads(FIXTURE_PATH.read_text())
    asks = [SimpleNamespace(**level) for level in data["asks"]]
    bids = [SimpleNamespace(**level) for level in data["bids"]]
    return SimpleNamespace(asks=asks, bids=bids)


def make_broker(tmp_path, balance=100.0):
    return PaperBroker(starting_balance=balance, state_path=tmp_path / "state.json")


def test_buy_fills_cheapest_level_first(tmp_path):
    broker = make_broker(tmp_path)
    book = load_fixture_book()

    # $4.50 = exactly the first level (5.0 shares @ 0.90)
    fill = broker.buy("m1", "t1", "Up", 4.50, book)

    assert fill["avg_price"] == pytest.approx(0.90)
    assert fill["shares"] == pytest.approx(5.0)
    assert broker.balance == pytest.approx(95.50)


def test_buy_walks_multiple_levels(tmp_path):
    broker = make_broker(tmp_path)
    book = load_fixture_book()

    # 5.0 shares @ 0.90 ($4.50) + 1.0 share @ 0.95 ($0.95) = $5.45 for 6 shares
    fill = broker.buy("m1", "t1", "Up", 5.45, book)

    assert fill["shares"] == pytest.approx(6.0)
    assert fill["avg_price"] == pytest.approx(5.45 / 6.0)


def test_buy_raises_when_book_cannot_fill(tmp_path):
    broker = make_broker(tmp_path, balance=10_000.0)
    book = load_fixture_book()

    total_book_usd = 5.0 * 0.90 + 3.0 * 0.95 + 100.0 * 0.99
    with pytest.raises(InsufficientLiquidity):
        broker.buy("m1", "t1", "Up", total_book_usd + 1.0, book)


def test_buy_raises_when_balance_too_low(tmp_path):
    broker = make_broker(tmp_path, balance=1.0)
    book = load_fixture_book()

    with pytest.raises(InsufficientBalance):
        broker.buy("m1", "t1", "Up", 4.50, book)


def test_state_persists_across_instances(tmp_path):
    state_path = tmp_path / "state.json"
    broker = PaperBroker(starting_balance=100.0, state_path=state_path)
    broker.buy("m1", "t1", "Up", 4.50, load_fixture_book())

    reloaded = PaperBroker(starting_balance=100.0, state_path=state_path)
    assert reloaded.balance == pytest.approx(95.50)
    assert "t1" in reloaded.state.positions


def test_resolve_win_pays_out_full_share_value(tmp_path):
    broker = make_broker(tmp_path)
    broker.buy("m1", "t1", "Up", 4.50, load_fixture_book())  # 5.0 shares @ 0.90, cost $4.50

    result = broker.resolve("t1", won=True)

    assert result["payout"] == pytest.approx(5.0)
    assert result["pnl"] == pytest.approx(5.0 - 4.50)
    assert broker.balance == pytest.approx(95.50 + 5.0)
    assert "t1" not in broker.state.positions


def test_resolve_loss_pays_out_nothing(tmp_path):
    broker = make_broker(tmp_path)
    broker.buy("m1", "t1", "Up", 4.50, load_fixture_book())

    result = broker.resolve("t1", won=False)

    assert result["payout"] == 0.0
    assert result["pnl"] == pytest.approx(-4.50)
    assert broker.balance == pytest.approx(95.50)  # no payout added


def test_resolve_unknown_token_raises(tmp_path):
    broker = make_broker(tmp_path)
    with pytest.raises(KeyError):
        broker.resolve("nonexistent", won=True)


def test_has_open_position_for_event_catches_cross_scan_correlation(tmp_path):
    # Regression test: a within-scan correlation cap isn't enough, because
    # two correlated strikes can each be the *only* candidate on separate,
    # consecutive scans -- this is the cross-scan check that catches it.
    broker = make_broker(tmp_path)
    assert broker.has_open_position_for_event("2026-06-25T11:00:00+00:00") is False

    broker.buy(
        "m1", "t1", "Yes", 2.0, load_fixture_book(),
        event_key="2026-06-25T11:00:00+00:00",
    )

    # Same event, different market/strike -- must be blocked.
    assert broker.has_open_position_for_event("2026-06-25T11:00:00+00:00") is True
    # A different event is unaffected.
    assert broker.has_open_position_for_event("2026-06-25T12:00:00+00:00") is False


def test_has_open_position_for_event_ignores_empty_key(tmp_path):
    broker = make_broker(tmp_path)
    broker.buy("m1", "t1", "Yes", 2.0, load_fixture_book())  # no event_key passed
    assert broker.has_open_position_for_event("") is False


def test_repeat_buy_averages_position(tmp_path):
    # Each call is given the same static book snapshot (the broker doesn't
    # mutate it), so both buys fill against the cheapest 0.90 level.
    broker = make_broker(tmp_path)
    book = load_fixture_book()

    broker.buy("m1", "t1", "Up", 4.50, book)  # 5.0 shares @ 0.90
    broker.buy("m1", "t1", "Up", 0.90, book)  # 1.0 more share @ 0.90

    position = broker.state.positions["t1"]
    assert position.shares == pytest.approx(6.0)
    assert position.avg_price == pytest.approx(0.90)
