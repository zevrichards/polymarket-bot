"""Live execution backend — places real orders on Polymarket's CLOB.

Interface mirrors PaperBroker exactly so lag_bot.py routes to this
at runtime by swapping the broker object; no changes to strategy code.

State (open positions + cost basis) is persisted locally to
logs/live_state.json for stop-loss checking and PnL journaling — the
canonical balance is always queried live from the CLOB API, never cached.

Orders use FOK (Fill or Kill): either fill completely at the best
available price or reject immediately with no partial fill. This matches
the strategy's intent — we want the trade at our edge or not at all.

IMPORTANT: this module loads credentials from .env (POLY_PRIVATE_KEY,
POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE). The .env file must
never be committed to git — it is listed in .gitignore. See .env.example
for the expected format, and scripts/setup_credentials.py to generate keys.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from py_clob_client_v2.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderPayload,
    OrderType,
)

from .clob_client import get_authenticated_client

STATE_PATH = Path(__file__).resolve().parent.parent / "logs" / "live_state.json"
log = logging.getLogger(__name__)


@dataclass
class Position:
    market_id: str
    token_id: str
    outcome: str
    shares: float
    avg_price: float
    opened_at: str = ""
    event_key: str = ""


@dataclass
class LiveState:
    positions: dict[str, Position] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"positions": {k: asdict(v) for k, v in self.positions.items()}}

    @classmethod
    def from_json(cls, data: dict) -> "LiveState":
        positions = {k: Position(**v) for k, v in data.get("positions", {}).items()}
        return cls(positions=positions)


class OrderRejected(Exception):
    """Raised when the CLOB rejects or fails to fill a FOK order."""


class LiveBroker:
    def __init__(self, state_path: Path = STATE_PATH):
        self.state_path = state_path
        self.client = get_authenticated_client()
        self.state = self._load()

    def _load(self) -> LiveState:
        if self.state_path.exists():
            with self.state_path.open(encoding="utf-8") as f:
                return LiveState.from_json(json.load(f))
        return LiveState()

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.state_path.open("w", encoding="utf-8") as f:
            json.dump(self.state.to_json(), f, indent=2)

    @property
    def balance(self) -> float:
        """Live USDC balance from Polymarket — never read from cache."""
        result = self.client.get_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
            )
        )
        return float(result.get("balance", 0))

    def has_open_position_for_market(self, market_id: str) -> bool:
        return any(p.market_id == market_id for p in self.state.positions.values())

    def has_open_position_for_event(self, event_key: str) -> bool:
        if not event_key:
            return False
        return any(p.event_key == event_key for p in self.state.positions.values())

    def buy(
        self,
        market_id: str,
        token_id: str,
        outcome: str,
        usd_amount: float,
        order_book,  # accepted for interface parity with PaperBroker; not used here
        event_key: str = "",
    ) -> dict:
        """Place a FOK market buy for usd_amount USDC worth of token_id.

        The CLOB finds the best available price; we don't specify one.
        FOK means we fill completely or not at all — no partial fills.
        Raises OrderRejected if the CLOB returns status != "matched".
        """
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=usd_amount,
            side="BUY",
            order_type=OrderType.FOK,
        )
        order = self.client.create_market_order(order_args)
        resp = self.client.post_order(order, OrderType.FOK)
        log.debug("BUY response: %s", resp)

        status = resp.get("status", "")
        if status not in ("matched", "delayed"):
            raise OrderRejected(f"buy order not filled (status={status!r}): {resp}")

        # V2: takingAmount = shares received, makingAmount = USDC paid
        # Both are in 6-decimal micro-units; divide to get human-readable values.
        raw_shares = float(resp.get("takingAmount") or 0)
        raw_cost   = float(resp.get("makingAmount") or usd_amount)
        shares = raw_shares / 1e6 if raw_shares > 1000 else raw_shares
        cost   = raw_cost   / 1e6 if raw_cost   > 1000 else raw_cost
        if not shares:
            shares = usd_amount / 0.5  # fallback: assume mid price
            cost = usd_amount
        avg_price = cost / shares

        existing = self.state.positions.get(token_id)
        if existing:
            total = existing.shares + shares
            existing.avg_price = (existing.avg_price * existing.shares + cost) / total
            existing.shares = total
        else:
            self.state.positions[token_id] = Position(
                market_id=market_id,
                token_id=token_id,
                outcome=outcome,
                shares=shares,
                avg_price=avg_price,
                opened_at=datetime.now(timezone.utc).isoformat(),
                event_key=event_key,
            )
        self.save()

        return {
            "market_id": market_id,
            "token_id": token_id,
            "outcome": outcome,
            "shares": shares,
            "avg_price": avg_price,
            "cost": cost,
            "balance_after": self.balance,
            "order_id": resp.get("orderID"),
        }

    def sell(self, token_id: str, order_book) -> dict:
        """Place a FOK market sell for the full position in token_id.

        order_book is accepted for interface parity but not used — the CLOB
        handles price. If FOK fails (no liquidity), raises OrderRejected
        rather than leaving a partial exit, which is the right outcome for
        stop-loss: either we exit cleanly or we keep the position.
        """
        position = self.state.positions.get(token_id)
        if position is None:
            raise KeyError(f"no open position for token_id={token_id}")

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=position.shares,  # for SELL, amount = shares to sell
            side="SELL",
            order_type=OrderType.FOK,
        )
        order = self.client.create_market_order(order_args)
        resp = self.client.post_order(order, OrderType.FOK)
        log.debug("SELL response: %s", resp)

        status = resp.get("status", "")
        if status not in ("matched", "delayed"):
            raise OrderRejected(f"sell order not filled (status={status!r}): {resp}")

        # V2 SELL: takingAmount = shares we gave up, makingAmount = USDC we received
        raw_sold     = float(resp.get("takingAmount") or position.shares)
        raw_proceeds = float(resp.get("makingAmount") or 0)
        shares_sold = raw_sold     / 1e6 if raw_sold     > 1000 else raw_sold
        proceeds    = raw_proceeds / 1e6 if raw_proceeds > 1000 else raw_proceeds
        avg_exit_price = proceeds / shares_sold if shares_sold else 0.0
        cost_basis = position.shares * position.avg_price
        pnl = proceeds - cost_basis

        del self.state.positions[token_id]
        self.save()

        return {
            "market_id": position.market_id,
            "token_id": token_id,
            "outcome": position.outcome,
            "shares": shares_sold,
            "avg_exit_price": avg_exit_price,
            "proceeds": proceeds,
            "cost_basis": cost_basis,
            "pnl": pnl,
            "balance_after": self.balance,
        }

    def resolve(self, token_id: str, won: bool) -> dict:
        """Record a resolution. Polymarket auto-settles winning shares to
        USDC — no on-chain action needed here. We update local state and
        let the journal record the outcome; the live balance will already
        reflect the payout on the next balance query."""
        position = self.state.positions.pop(token_id, None)
        if position is None:
            raise KeyError(f"no open position for token_id={token_id}")

        payout = position.shares * (1.0 if won else 0.0)
        cost_basis = position.shares * position.avg_price
        pnl = payout - cost_basis
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
            "balance_after": self.balance,
        }
