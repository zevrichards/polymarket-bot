"""Strategy-logic tests against fixture order-book data -- no network calls."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from core import clob_client
from core.markets import BtcMarket
from bots.directional_bot import find_candidates, size_position

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_orderbook.json"

BOT_CFG = {
    "min_entry_price": 0.85,
    "max_entry_price": 0.99,
    "max_seconds_to_resolution": 120,
    "min_seconds_to_resolution": 5,
    "max_bet": 2.0,
    "max_bankroll_fraction": 0.02,
}


def make_market(seconds_left, outcomes=("Up", "Down"), token_ids=("t-up", "t-down")):
    end_date = datetime.now(timezone.utc) + timedelta(seconds=seconds_left)
    return BtcMarket(
        market_id="m1",
        question="Bitcoin Up or Down - test",
        slug="btc-updown-test",
        end_date=end_date,
        outcomes=list(outcomes),
        token_ids=list(token_ids),
        active=True,
        closed=False,
    )


def fake_book(asks):
    return SimpleNamespace(asks=[SimpleNamespace(price=str(p), size="100") for p in asks], bids=[])


def test_finds_candidate_in_entry_price_band(monkeypatch):
    market = make_market(seconds_left=60)
    monkeypatch.setattr(clob_client, "get_order_book", lambda token_id: fake_book([0.90]))

    candidates = find_candidates(market, BOT_CFG)

    assert len(candidates) == 2  # both outcomes return the same fake book in this test
    assert candidates[0]["ask_price"] == 0.90


def test_rejects_price_outside_band(monkeypatch):
    market = make_market(seconds_left=60)
    monkeypatch.setattr(clob_client, "get_order_book", lambda token_id: fake_book([0.50]))

    candidates = find_candidates(market, BOT_CFG)

    assert candidates == []


def test_rejects_when_too_far_from_resolution(monkeypatch):
    market = make_market(seconds_left=600)  # outside max_seconds_to_resolution=120
    monkeypatch.setattr(clob_client, "get_order_book", lambda token_id: fake_book([0.90]))

    candidates = find_candidates(market, BOT_CFG)

    assert candidates == []


def test_rejects_when_inside_min_seconds_to_resolution(monkeypatch):
    market = make_market(seconds_left=2)  # inside min_seconds_to_resolution=5
    monkeypatch.setattr(clob_client, "get_order_book", lambda token_id: fake_book([0.90]))

    candidates = find_candidates(market, BOT_CFG)

    assert candidates == []


def test_size_position_respects_max_bet_and_bankroll_fraction():
    broker = SimpleNamespace(balance=1000.0)
    # 2% of 1000 = 20.0, but max_bet caps it at 2.0
    assert size_position(broker, BOT_CFG) == 2.0

    broker = SimpleNamespace(balance=50.0)
    # 2% of 50 = 1.0, below max_bet, so bankroll fraction wins
    assert size_position(broker, BOT_CFG) == 1.0
