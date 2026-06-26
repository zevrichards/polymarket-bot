"""Discover Bitcoin-resolution markets via Polymarket's public Gamma API.

The Gamma API (https://gamma-api.polymarket.com) is unauthenticated and is
the source of market metadata (question, outcomes, clobTokenIds, end date).
The CLOB API (core/clob_client.py) is the source of live order books/prices
for the token IDs Gamma gives us.

Gamma's /events endpoint doesn't support free-text search, so we pull a page
of active markets sorted by volume/end-date and filter client-side for
Bitcoin-price markets (covers slugs like "btc-updown-15m-...", "bitcoin-up-or-down...",
and plain-English questions mentioning Bitcoin/BTC).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

GAMMA_HOST = "https://gamma-api.polymarket.com"
BTC_KEYWORDS = ("bitcoin", "btc")


@dataclass
class BtcMarket:
    market_id: str
    question: str
    slug: str
    end_date: datetime | None
    outcomes: list[str]
    token_ids: list[str]  # parallel to outcomes
    active: bool
    closed: bool
    start_date: datetime | None = None  # the window's actual start (eventStartTime),
    # NOT Gamma's "startDate" field (that's listing/creation time, see Session 1 notes)

    def seconds_to_resolution(self, now: datetime | None = None) -> float | None:
        if self.end_date is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (self.end_date - now).total_seconds()


def _parse_market(raw: dict) -> BtcMarket | None:
    try:
        outcomes = json.loads(raw.get("outcomes", "[]"))
        token_ids = json.loads(raw.get("clobTokenIds", "[]"))
    except (json.JSONDecodeError, TypeError):
        return None

    if not outcomes or not token_ids or len(outcomes) != len(token_ids):
        return None

    end_date = None
    raw_end = raw.get("endDate") or raw.get("end_date")
    if raw_end:
        try:
            end_date = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
        except ValueError:
            end_date = None

    start_date = None
    raw_start = raw.get("eventStartTime")
    if raw_start:
        try:
            start_date = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        except ValueError:
            start_date = None

    return BtcMarket(
        market_id=str(raw.get("id")),
        question=raw.get("question", ""),
        slug=raw.get("slug", ""),
        end_date=end_date,
        outcomes=outcomes,
        token_ids=token_ids,
        active=bool(raw.get("active")),
        closed=bool(raw.get("closed")),
        start_date=start_date,
    )


def _is_btc_market(market: BtcMarket) -> bool:
    haystack = f"{market.question} {market.slug}".lower()
    return any(keyword in haystack for keyword in BTC_KEYWORDS)


def fetch_btc_markets(
    max_pages: int = 10, page_size: int = 100, only_active: bool = True
) -> list[BtcMarket]:
    """Fetch and filter Bitcoin-resolution markets, soonest-resolving first.

    Gamma's "active": true / "closed": false flags lag reality -- markets
    whose end date is already in the past are sometimes still returned, and
    upcoming short-duration "btc-updown-5m-<ts>" markets are pre-listed well
    before their trading window opens. So we page through results (Gamma
    caps each page at 100 regardless of the requested limit) and explicitly
    filter to markets whose resolution time is still in the future, then
    sort by soonest-resolving.
    """
    now = datetime.now(timezone.utc)
    markets: list[BtcMarket] = []

    for page in range(max_pages):
        params = {
            "limit": page_size,
            "offset": page * page_size,
            "order": "startDate",
            "ascending": "false",
        }
        if only_active:
            params["active"] = "true"
            params["closed"] = "false"

        resp = requests.get(f"{GAMMA_HOST}/markets", params=params, timeout=10)
        resp.raise_for_status()
        raw_markets = resp.json()
        if not raw_markets:
            break

        for raw in raw_markets:
            market = _parse_market(raw)
            if not market or not _is_btc_market(market):
                continue
            seconds_left = market.seconds_to_resolution(now)
            if seconds_left is not None and seconds_left > 0:
                markets.append(market)

    markets.sort(key=lambda m: m.seconds_to_resolution(now) or float("inf"))
    return markets


def fetch_market_by_slug(slug: str) -> BtcMarket | None:
    resp = requests.get(f"{GAMMA_HOST}/markets", params={"slug": slug}, timeout=10)
    resp.raise_for_status()
    raw_markets = resp.json()
    if not raw_markets:
        return None
    return _parse_market(raw_markets[0])
