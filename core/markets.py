"""Discover crypto price-resolution markets via Polymarket's public Gamma API.

The Gamma API (https://gamma-api.polymarket.com) is unauthenticated and is
the source of market metadata (question, outcomes, clobTokenIds, end date).
The CLOB API (core/clob_client.py) is the source of live order books/prices
for the token IDs Gamma gives us.

Gamma's /events endpoint doesn't support free-text search, so we pull a page
of active markets sorted by volume/end-date and filter client-side for
crypto price markets (covers slugs like "btc-updown-15m-...", "eth-updown-5m-...",
and plain-English questions mentioning the coin).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

GAMMA_HOST = "https://gamma-api.polymarket.com"

# Coin -> (Gamma slug/question keywords, Binance symbol)
COIN_CONFIG: dict[str, dict] = {
    "btc": {"keywords": ("bitcoin", "btc"), "binance_symbol": "BTCUSDT"},
    "eth": {"keywords": ("ethereum", "eth"),  "binance_symbol": "ETHUSDT"},
    "sol": {"keywords": ("solana", "sol"),    "binance_symbol": "SOLUSDT"},
    "bnb": {"keywords": ("bnb",),             "binance_symbol": "BNBUSDT"},
    "xrp": {"keywords": ("xrp", "ripple"),   "binance_symbol": "XRPUSDT"},
}


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
    coin: str = "btc"  # which crypto this market resolves against

    @property
    def binance_symbol(self) -> str:
        return COIN_CONFIG.get(self.coin, COIN_CONFIG["btc"])["binance_symbol"]

    def seconds_to_resolution(self, now: datetime | None = None) -> float | None:
        if self.end_date is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (self.end_date - now).total_seconds()


def _detect_coin(question: str, slug: str) -> str:
    """Detect which coin a market is for based on slug/question keywords."""
    haystack = f"{question} {slug}".lower()
    for coin, cfg in COIN_CONFIG.items():
        if any(kw in haystack for kw in cfg["keywords"]):
            return coin
    return "btc"


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

    question = raw.get("question", "")
    slug = raw.get("slug", "")
    return BtcMarket(
        market_id=str(raw.get("id")),
        question=question,
        slug=slug,
        end_date=end_date,
        outcomes=outcomes,
        token_ids=token_ids,
        active=bool(raw.get("active")),
        closed=bool(raw.get("closed")),
        start_date=start_date,
        coin=_detect_coin(question, slug),
    )


def _is_btc_market(market: BtcMarket) -> bool:
    haystack = f"{market.question} {market.slug}".lower()
    return any(keyword in haystack for keyword in COIN_CONFIG["btc"]["keywords"])


def _matches_coins(market: BtcMarket, coins: list[str]) -> bool:
    haystack = f"{market.question} {market.slug}".lower()
    for coin in coins:
        cfg = COIN_CONFIG.get(coin)
        if cfg and any(kw in haystack for kw in cfg["keywords"]):
            return True
    return False


def fetch_crypto_markets(
    coins: list[str] | None = None,
    max_pages: int = 10,
    page_size: int = 100,
    only_active: bool = True,
    horizon_hours: float = 4,
) -> list[BtcMarket]:
    """Fetch and filter crypto price-resolution markets for the given coins.

    coins: list of coin keys from COIN_CONFIG (e.g. ["btc", "eth"]).
           Defaults to ["btc"] for backward compatibility.
    """
    return fetch_btc_markets(
        max_pages=max_pages,
        page_size=page_size,
        only_active=only_active,
        horizon_hours=horizon_hours,
        coins=coins or ["btc"],
    )


def fetch_btc_markets(
    max_pages: int = 10,
    page_size: int = 100,
    only_active: bool = True,
    horizon_hours: float = 4,
    coins: list[str] | None = None,
) -> list[BtcMarket]:
    """Fetch and filter Bitcoin-resolution markets, soonest-resolving first.

    CONFIRMED BUG, FIXED HERE (see BUILD_INTELLIGENCE_REPORT.md Session 10):
    the previous version paginated by "startDate" (creation time) descending
    and filtered client-side for future end dates. That silently missed
    markets that were *created* a while ago but are *resolving* very soon --
    Polymarket pre-lists many markets far in advance, so a market due in the
    next few minutes can be created hours or days before that and end up
    buried past page 10 by all the more-recently-created, further-future
    markets ranked ahead of it. Caught by directly comparing: a market with
    176 seconds left to resolve existed and was tradeable, but this function
    returned zero markets in that window at the same moment.

    Fix: query Gamma directly with `end_date_min`/`end_date_max` -- this
    asks the API to filter server-side by actual resolution time, which is
    exactly what we want, instead of guessing from creation order. Confirmed
    these params work via direct testing. With no explicit "order" and a
    wide horizon, results are dominated by non-BTC markets across every
    Polymarket category (sports, politics, etc.) and our BTC ones get
    pushed past page 1 -- fixed by also sorting end_date ascending, so the
    soonest-resolving markets surface first regardless of category. A
    horizon longer than ~4 hours isn't needed since every bot in this repo
    only acts on markets resolving in minutes, not hours.

    Session 16 fix: this used to always page through all `max_pages`
    (1000 results) on every call, even though BTC markets consistently
    cluster in the first page or two (sorted soonest-first). With two
    bots both calling this every ~20s for 30+ minutes, that's a lot of
    unnecessary request volume -- almost certainly what triggered a
    Gamma 403 in practice. Now stops early once a full page passes with
    zero new BTC matches after at least one match has been found --
    we've moved past the cluster, no need to keep scanning sports/politics
    markets for the rest of the 4-hour horizon.
    """
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=horizon_hours)
    markets: list[BtcMarket] = []
    found_any = False
    filter_coins = coins or ["btc"]

    for page in range(max_pages):
        params = {
            "limit": page_size,
            "offset": page * page_size,
            "end_date_min": now.isoformat(),
            "end_date_max": horizon.isoformat(),
            "order": "endDate",
            "ascending": "true",
        }
        if only_active:
            params["active"] = "true"
            params["closed"] = "false"

        resp = requests.get(f"{GAMMA_HOST}/markets", params=params, timeout=10)
        resp.raise_for_status()
        raw_markets = resp.json()
        if not raw_markets:
            break

        page_had_match = False
        for raw in raw_markets:
            market = _parse_market(raw)
            if not market or not _matches_coins(market, filter_coins):
                continue
            seconds_left = market.seconds_to_resolution(now)
            if seconds_left is not None and seconds_left > 0:
                markets.append(market)
                page_had_match = True

        if page_had_match:
            found_any = True
        elif found_any:
            break  # past the BTC cluster -- no need to keep paginating

    markets.sort(key=lambda m: m.seconds_to_resolution(now) or float("inf"))
    return markets


def fetch_market_by_slug(slug: str) -> BtcMarket | None:
    resp = requests.get(f"{GAMMA_HOST}/markets", params={"slug": slug}, timeout=10)
    resp.raise_for_status()
    raw_markets = resp.json()
    if not raw_markets:
        return None
    return _parse_market(raw_markets[0])
