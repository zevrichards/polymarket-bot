"""Strategy-logic tests against fixture order-book data -- no network calls."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from core import clob_client
from core.markets import BtcMarket
from core.paper_broker import PaperBroker
from bots.directional_bot import find_candidates, select_candidates, size_position

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


def test_has_open_position_for_market_blocks_reentry(tmp_path):
    # Regression test for the overnight pyramiding bug: once a position
    # exists for a market, the bot must not be able to add to it or buy
    # the opposite outcome.
    broker = PaperBroker(starting_balance=100.0, state_path=tmp_path / "state.json")
    assert broker.has_open_position_for_market("m1") is False

    broker.buy("m1", "t-up", "Up", 2.0, fake_book([0.90]))

    assert broker.has_open_position_for_market("m1") is True
    assert broker.has_open_position_for_market("m2") is False


def test_select_candidates_caps_correlated_markets_per_event():
    # Regression test for the overnight strike-ladder bug: 7 markets all
    # resolving at the same timestamp must collapse to 1 trade, not 7.
    same_time = make_market(seconds_left=60).end_date
    markets_ = [
        BtcMarket(
            market_id=f"m{i}", question="q", slug=f"s{i}", end_date=same_time,
            outcomes=["Up", "Down"], token_ids=[f"t{i}-up", f"t{i}-down"],
            active=True, closed=False,
        )
        for i in range(7)
    ]
    # Each market has one candidate, with increasing confidence (ask_price)
    market_candidates = [
        (m, {"token_id": f"t{i}-up", "outcome": "Up", "ask_price": 0.85 + i * 0.01, "book": None})
        for i, m in enumerate(markets_)
    ]

    selected = select_candidates(market_candidates, max_per_event=1)

    assert len(selected) == 1
    # the highest-confidence candidate (last one, ask_price=0.91) should win
    assert selected[0][1]["ask_price"] == 0.85 + 6 * 0.01


def test_select_candidates_allows_uncorrelated_markets_through():
    market_a = make_market(seconds_left=60)
    market_b = make_market(seconds_left=120)  # different end_date
    market_candidates = [
        (market_a, {"token_id": "ta", "outcome": "Up", "ask_price": 0.90, "book": None}),
        (market_b, {"token_id": "tb", "outcome": "Up", "ask_price": 0.90, "book": None}),
    ]

    selected = select_candidates(market_candidates, max_per_event=1)

    assert len(selected) == 2
