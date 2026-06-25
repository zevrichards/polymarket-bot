"""Simulated execution backend for paper trading.

Fills are computed against *real* live order books (fetched the same way
the live backend would), walking price levels to get a realistic average
fill price and rejecting trades the book can't actually support -- this is
what lets strategy code be written once and swapped from paper to live
later without rewriting the entry logic.

State (virtual balance + open positions) persists to logs/paper_state.json
so repeated `--once` runs accumulate a track record instead of resetting.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent.parent / "logs" / "paper_state.json"


@dataclass
class Position:
    market_id: str
    token_id: str
    outcome: str
    shares: float
    avg_price: float
    opened_at: str = ""  # ISO timestamp, used for stale-position warnings
    event_key: str = ""  # market.end_date.isoformat() -- shared by correlated strike-ladder markets


@dataclass
class PaperState:
    balance: float
    positions: dict[str, Position] = field(default_factory=dict)  # keyed by token_id

    def to_json(self) -> dict:
        return {
            "balance": self.balance,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
        }

    @classmethod
    def from_json(cls, data: dict) -> "PaperState":
        positions = {k: Position(**v) for k, v in data.get("positions", {}).items()}
        return cls(balance=data["balance"], positions=positions)


class InsufficientLiquidity(Exception):
    pass


class InsufficientBalance(Exception):
    pass


class PaperBroker:
    def __init__(self, starting_balance: float, state_path: Path = STATE_PATH):
        self.state_path = state_path
        self.state = self._load(starting_balance)

    def _load(self, starting_balance: float) -> PaperState:
        if self.state_path.exists():
            with self.state_path.open(encoding="utf-8") as f:
                return PaperState.from_json(json.load(f))
        return PaperState(balance=starting_balance)

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.state_path.open("w", encoding="utf-8") as f:
            json.dump(self.state.to_json(), f, indent=2)

    @property
    def balance(self) -> float:
        return self.state.balance

    def has_open_position_for_market(self, market_id: str) -> bool:
        """True if we already hold a position in either outcome of this
        market. Checking this before entering prevents two real bugs seen
        in testing: pyramiding into the same token every scan, and buying
        both outcomes of the same binary market (a guaranteed loss on the
        combination once both legs' prices sum to more than $1)."""
        return any(p.market_id == market_id for p in self.state.positions.values())

    def has_open_position_for_event(self, event_key: str) -> bool:
        """True if we already hold a position in ANY market sharing this
        resolution event (e.g. a strike-ladder of "Bitcoin above $X"
        markets all settling off one price observation at one timestamp).

        This is a separate check from has_open_position_for_market because
        the bug it fixes is cross-scan: a within-scan correlation cap (see
        bots/directional_bot.select_candidates) only sees candidates found
        in the *same* scan, but two correlated strikes can each become the
        sole qualifying candidate on consecutive scans 60s apart, slipping
        past a same-scan-only check entirely. This catches that case by
        looking at the full set of currently open positions, not just the
        current scan's batch."""
        if not event_key:
            return False
        return any(p.event_key == event_key for p in self.state.positions.values())

    def buy(
        self,
        market_id: str,
        token_id: str,
        outcome: str,
        usd_amount: float,
        order_book,
        event_key: str = "",
    ) -> dict:
        """Simulate a market buy by walking the book's ask levels.

        order_book is a py_clob_client OrderBookSummary (has .asks, ascending
        or descending depending on the API -- we sort explicitly to be safe).
        Returns a fill summary dict. Raises InsufficientLiquidity if the book
        can't fill the requested USD amount, or InsufficientBalance if the
        paper account doesn't have enough virtual cash.
        """
        if usd_amount > self.state.balance:
            raise InsufficientBalance(
                f"requested ${usd_amount:.2f}, only ${self.state.balance:.2f} available"
            )

        asks = sorted(order_book.asks, key=lambda level: float(level.price))
        remaining_usd = usd_amount
        shares_bought = 0.0
        cost = 0.0

        for level in asks:
            price = float(level.price)
            size = float(level.size)
            if price <= 0:
                continue
            level_usd_capacity = price * size
            usd_to_take = min(remaining_usd, level_usd_capacity)
            if usd_to_take <= 0:
                continue
            shares_bought += usd_to_take / price
            cost += usd_to_take
            remaining_usd -= usd_to_take
            if remaining_usd <= 1e-9:
                break

        if remaining_usd > 1e-6:
            raise InsufficientLiquidity(
                f"book only supports ${cost:.2f} of the requested ${usd_amount:.2f}"
            )

        avg_price = cost / shares_bought if shares_bought else 0.0

        self.state.balance -= cost
        existing = self.state.positions.get(token_id)
        if existing:
            total_shares = existing.shares + shares_bought
            existing.avg_price = (
                existing.avg_price * existing.shares + cost
            ) / total_shares
            existing.shares = total_shares
        else:
            self.state.positions[token_id] = Position(
                market_id=market_id,
                token_id=token_id,
                outcome=outcome,
                shares=shares_bought,
                avg_price=avg_price,
                opened_at=datetime.now(timezone.utc).isoformat(),
                event_key=event_key,
            )
        self.save()

        return {
            "market_id": market_id,
            "token_id": token_id,
            "outcome": outcome,
            "shares": shares_bought,
            "avg_price": avg_price,
            "cost": cost,
            "balance_after": self.state.balance,
        }

    def resolve(self, token_id: str, won: bool) -> dict:
        """Settle a position once its market has resolved. Pays out
        shares * $1 if won, $0 if lost, and removes the position."""
        position = self.state.positions.pop(token_id, None)
        if position is None:
            raise KeyError(f"no open position for token_id={token_id}")

        payout = position.shares * (1.0 if won else 0.0)
        cost_basis = position.shares * position.avg_price
        pnl = payout - cost_basis

        self.state.balance += payout
        self.save()

        return {
            "market_id": position.market_id,
            "token_id": token_id,
            "outcome": position.outcome,
            "shares": position.shares,
            "won": won,
            "payout": payout,
            "cost_basis": cost_basis,
            "pnl": pnl,
            "balance_after": self.state.balance,
        }
