"""Thin wrapper around Binance's public market-data API (no key required).

Used by bots/lag_bot.py to estimate the true probability of a BTC
up/down market's outcome independently of Polymarket's own price --
this is the "real edge" piece missing from Bots 1-3 (see Strategies.txt).

Two things needed for that model:
  1. The exact BTC price at the market's window-start timestamp (the
     baseline the market resolves against).
  2. Recent realized volatility, to know how much weight to put on the
     current price's distance from that baseline.

Binance keeps historical 1-second klines indefinitely and serves them
with no auth -- confirmed by direct testing (see BUILD_INTELLIGENCE_REPORT.md
Session 9).
"""
from __future__ import annotations

import requests

BINANCE_HOST = "https://api.binance.com"
SYMBOL = "BTCUSDT"


def get_price_at(timestamp_ms: int) -> float | None:
    """Returns BTC/USDT's close price for the 1-second kline starting at
    or just after the given timestamp. None if Binance has no data there
    (e.g. timestamp is in the future)."""
    resp = requests.get(
        f"{BINANCE_HOST}/api/v3/klines",
        params={"symbol": SYMBOL, "interval": "1s", "startTime": timestamp_ms, "limit": 1},
        timeout=10,
    )
    resp.raise_for_status()
    klines = resp.json()
    if not klines:
        return None
    return float(klines[0][4])  # close price


def get_recent_prices(lookback_seconds: int = 120) -> list[float]:
    """Returns the last `lookback_seconds` worth of 1-second close prices,
    oldest first. Used to estimate recent realized volatility."""
    resp = requests.get(
        f"{BINANCE_HOST}/api/v3/klines",
        params={"symbol": SYMBOL, "interval": "1s", "limit": lookback_seconds},
        timeout=10,
    )
    resp.raise_for_status()
    klines = resp.json()
    return [float(k[4]) for k in klines]
