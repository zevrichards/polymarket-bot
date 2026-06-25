"""Checks whether a specific outcome token has settled, via Gamma's per-market
closed/outcomePrices fields.

Known limitation (confirmed by direct API probing, see BUILD_INTELLIGENCE_REPORT.md):
Polymarket's short-duration "btc-updown-5m/15m" markets do NOT reliably
populate `closed`/`outcomePrices` on Gamma, even long after they expire --
verified against a market that ended 6+ months ago and still reports
`closed: false`, `outcomePrices: null`. Other market types (single-day BTC
threshold markets, etc.) resolve normally. There is currently no other
public API found that reliably reports settlement for the 5m/15m markets --
the CLOB's order book disappears after expiry (404) and get_last_trade_price
returns a stale pre-resolution value, not the settlement price.

check_token_resolution() returns None for "not resolved yet" exactly the
same as "can't tell" -- callers are expected to apply their own staleness
grace period (see resolve_stale_grace_seconds usage in the bots) rather than
treating a None forever as "still open."
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import requests

from core import journal

GAMMA_HOST = "https://gamma-api.polymarket.com"
STALE_WARNING_SECONDS = 1800  # 30 min past being eligible for resolution check

log = logging.getLogger(__name__)


def check_token_resolution(market_id: str, token_id: str) -> bool | None:
    """Returns True (this token won), False (lost), or None (not resolved /
    can't determine -- see module docstring for the known data gap)."""
    try:
        resp = requests.get(f"{GAMMA_HOST}/markets/{market_id}", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    if not data.get("closed"):
        return None

    raw_prices = data.get("outcomePrices")
    raw_token_ids = data.get("clobTokenIds")
    if not raw_prices or not raw_token_ids:
        return None

    try:
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        token_ids = json.loads(raw_token_ids) if isinstance(raw_token_ids, str) else raw_token_ids
        idx = token_ids.index(token_id)
        price = float(prices[idx])
    except (ValueError, IndexError, TypeError):
        return None

    if price >= 0.95:
        return True
    if price <= 0.05:
        return False
    return None  # closed but ambiguous price -- don't guess


def resolve_broker_positions(broker, bot_name: str) -> list[dict]:
    """Settle any of a PaperBroker's open positions whose market has
    resolved. Positions that can't be resolved (see module docstring -- the
    btc-updown-5m/15m data gap) are left open and, once stale, get a single
    logged warning per scan rather than being silently ignored forever.
    """
    results = []
    now = datetime.now(timezone.utc)

    for token_id, position in list(broker.state.positions.items()):
        won = check_token_resolution(position.market_id, token_id)
        if won is None:
            age_seconds = _position_age_seconds(position, now)
            if age_seconds is not None and age_seconds > STALE_WARNING_SECONDS:
                log.warning(
                    "%s: position %s/%s (%s) still unresolved after %.0fs -- "
                    "Polymarket's API may not expose settlement data for this "
                    "market type, see core/resolution.py",
                    bot_name, position.market_id, position.outcome, token_id[:12], age_seconds,
                )
            continue

        result = broker.resolve(token_id, won)
        record = journal.log_trade(bot_name, kind="resolution", **result)
        log.info(
            "RESOLVED %s %s | won=%s payout=$%.2f pnl=%+.2f",
            result["market_id"], result["outcome"], won, result["payout"], result["pnl"],
        )
        results.append(record)

    return results


def _position_age_seconds(position, now: datetime) -> float | None:
    if not position.opened_at:
        return None
    try:
        opened = datetime.fromisoformat(position.opened_at)
    except ValueError:
        return None
    return (now - opened).total_seconds()
