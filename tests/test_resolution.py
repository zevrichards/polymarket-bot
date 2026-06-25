"""Resolution-checking logic tested against fixture API responses -- no network."""
from unittest.mock import patch

from core import journal, resolution
from core.paper_broker import PaperBroker


def fake_response(json_data, status=200):
    class FakeResponse:
        status_code = status

        def raise_for_status(self):
            pass

        def json(self):
            return json_data

    return FakeResponse()


def test_check_token_resolution_open_market_returns_none():
    with patch("requests.get", return_value=fake_response({"closed": False})):
        assert resolution.check_token_resolution("m1", "t1") is None


def test_check_token_resolution_won():
    payload = {
        "closed": True,
        "outcomePrices": '["1", "0"]',
        "clobTokenIds": '["t1", "t2"]',
    }
    with patch("requests.get", return_value=fake_response(payload)):
        assert resolution.check_token_resolution("m1", "t1") is True
        assert resolution.check_token_resolution("m1", "t2") is False


def test_check_token_resolution_ambiguous_price_returns_none():
    payload = {
        "closed": True,
        "outcomePrices": '["0.5", "0.5"]',
        "clobTokenIds": '["t1", "t2"]',
    }
    with patch("requests.get", return_value=fake_response(payload)):
        assert resolution.check_token_resolution("m1", "t1") is None


def test_check_token_resolution_missing_outcome_prices_returns_none():
    # Mirrors the real "btc-updown" data gap: closed flag never flips and
    # outcomePrices stays null even long after the market expired.
    payload = {"closed": False, "outcomePrices": None}
    with patch("requests.get", return_value=fake_response(payload)):
        assert resolution.check_token_resolution("m1", "t1") is None


def test_resolve_broker_positions_settles_and_pays_out(tmp_path, monkeypatch):
    from core.paper_broker import Position

    # Redirect journal writes so this test doesn't pollute the real
    # logs/trades.jsonl (it did, silently, before this fix).
    monkeypatch.setattr(journal, "LOG_DIR", tmp_path)
    monkeypatch.setattr(journal, "TRADES_PATH", tmp_path / "trades.jsonl")

    broker = PaperBroker(starting_balance=100.0, state_path=tmp_path / "state.json")
    broker.state.positions["t1"] = Position(
        market_id="m1", token_id="t1", outcome="Up", shares=10.0, avg_price=0.90
    )

    payload = {
        "closed": True,
        "outcomePrices": '["1", "0"]',
        "clobTokenIds": '["t1", "t2"]',
    }
    with patch("requests.get", return_value=fake_response(payload)):
        results = resolution.resolve_broker_positions(broker, "test_bot")

    assert len(results) == 1
    assert results[0]["won"] is True
    assert results[0]["pnl"] == 1.0  # 10 shares * $1 payout - 10*0.90 cost basis
    assert "t1" not in broker.state.positions
    assert broker.balance == 100.0 + 10.0  # payout credited
