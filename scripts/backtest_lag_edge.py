"""Backtest the lag_bot thesis against real historical Polymarket resolutions.

Pulls N recently-closed BTC up/down markets from Gamma, reconstructs both
sides of the signal at fixed "seconds before resolution" checkpoints:

  - market_p_up: Polymarket's own Up-token price at that moment, from the
    CLOB's /prices-history endpoint (real trade/quote events, not synthetic).
  - model_p_up: our GBM model's estimate, from Binance 1s klines, using the
    exact same baseline_price/current_price/sigma logic as bots/lag_bot.py.

...then checks the actual outcome (Gamma's `outcomePrices`, authoritative)
and reports:
  1. Market calibration -- when Polymarket prices a token at X, does it win
     X% of the time, or does the market run "hotter" (more accurate) than
     its own price at the extremes?
  2. Simulated PnL for several filter regimes (current config, the old
     tighter config, and a "fade the crowd" variant) so we know whether any
     of them would have been net positive BEFORE risking more live capital.

No live orders. No wallet access. Read-only historical data only.
"""
from __future__ import annotations

import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import probability  # noqa: E402

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
BINANCE_HOST = "https://api.binance.com"

CHECKPOINTS_SECONDS_LEFT = [5, 10, 15, 20, 25, 30]
VOL_LOOKBACK_SECONDS = 120
OUT_CSV = Path(__file__).resolve().parent.parent / "logs" / "backtest_lag_edge.csv"
CACHE_PATH = Path(__file__).resolve().parent.parent / "logs" / "resolved_btc_markets_cache.json"
CACHE_MAX_AGE_HOURS = 6


@dataclass
class ResolvedMarket:
    slug: str
    start: datetime
    end: datetime
    up_token: str
    down_token: str
    up_won: bool


def fetch_resolved_btc_markets(target_count: int) -> list[ResolvedMarket]:
    """Gamma's generic /markets listing + sort/offset pagination proved
    unreliable for these short-duration markets in practice (recently-ended
    5m markets didn't surface even with end_date_max set correctly -- likely
    the same active/closed flag staleness noted in BUILD_INTELLIGENCE_REPORT
    Session 1, just biting a different query shape here).

    Instead, exploit the fact that these markets' slugs are deterministic:
    "btc-updown-5m-<start_epoch>", aligned to 300-second boundaries
    (confirmed directly: eventStartTime's epoch == the slug suffix). Walk
    backward from now in 300s steps and look each one up individually by
    slug -- slower (one request per candidate) but far more reliable than
    trusting Gamma's sort/filter combination for this market type.

    Also confirmed directly: once a market is fully archived, /markets?slug=
    stops returning it at all (empty list) even though it settled cleanly --
    but /events?slug= still has it, nested under event["markets"][0]. Use
    /events, not /markets, for anything more than a few minutes old."""
    out: list[ResolvedMarket] = []
    now = datetime.now(timezone.utc)
    # Stay clear of markets whose oracle settlement hasn't finalized yet --
    # a market that ended a few minutes ago often still shows ~0.995/0.005
    # instead of a clean 0/1.
    cutoff_epoch = int((now - timedelta(hours=1)).timestamp())
    aligned_start = cutoff_epoch - (cutoff_epoch % 300)

    max_candidates = target_count * 3  # some slugs may 404 / not resolve cleanly
    for i in range(max_candidates):
        epoch = aligned_start - i * 300
        slug = f"btc-updown-5m-{epoch}"
        try:
            resp = requests.get(f"{GAMMA_HOST}/events", params={"slug": slug}, timeout=10)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        raw_events = resp.json()
        if not raw_events or not raw_events[0].get("markets"):
            continue
        raw = raw_events[0]["markets"][0]

        raw_start = raw.get("eventStartTime")
        raw_end = raw.get("endDate")
        if not raw_start or not raw_end:
            continue
        try:
            start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
            end = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
            outcomes = json.loads(raw.get("outcomes", "[]"))
            token_ids = json.loads(raw.get("clobTokenIds", "[]"))
            outcome_prices = json.loads(raw.get("outcomePrices", "[]"))
        except (ValueError, json.JSONDecodeError):
            continue

        if "Up" not in outcomes or "Down" not in outcomes:
            continue
        if len(outcome_prices) != len(outcomes):
            continue
        up_price = float(outcome_prices[outcomes.index("Up")])
        if not (up_price <= 0.02 or up_price >= 0.98):
            continue  # not cleanly settled yet -- skip

        out.append(ResolvedMarket(
            slug=slug,
            start=start,
            end=end,
            up_token=token_ids[outcomes.index("Up")],
            down_token=token_ids[outcomes.index("Down")],
            up_won=(up_price >= 0.98),
        ))
        if len(out) >= target_count:
            break
        if (i + 1) % 40 == 0:
            print(f"  scanned {i+1} candidate slugs, found {len(out)} resolved markets so far")
        time.sleep(0.05)
    return out


def load_or_fetch_markets(target_count: int) -> list[ResolvedMarket]:
    """Cache the resolved-market list to disk (short TTL) so multiple
    backtest scripts run back-to-back against the same underlying markets
    (apples-to-apples comparisons) without re-paying the several-minute
    slug-walk cost every time."""
    if CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(cached["fetched_at"])
            age_hours = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
            if age_hours < CACHE_MAX_AGE_HOURS and len(cached["markets"]) >= target_count:
                print(f"Using cached market list ({len(cached['markets'])} markets, {age_hours:.1f}h old)")
                return [
                    ResolvedMarket(
                        slug=m["slug"], start=datetime.fromisoformat(m["start"]), end=datetime.fromisoformat(m["end"]),
                        up_token=m["up_token"], down_token=m["down_token"], up_won=m["up_won"],
                    )
                    for m in cached["markets"][:target_count]
                ]
        except (json.JSONDecodeError, KeyError, ValueError):
            pass  # corrupt/stale cache -- just refetch

    print(f"Fetching up to {target_count} resolved BTC up/down markets from Gamma...")
    markets = fetch_resolved_btc_markets(target_count)
    print(f"Got {len(markets)} resolved markets with clean binary outcomes.")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "markets": [
            {"slug": m.slug, "start": m.start.isoformat(), "end": m.end.isoformat(),
             "up_token": m.up_token, "down_token": m.down_token, "up_won": m.up_won}
            for m in markets
        ],
    }), encoding="utf-8")
    return markets


def fetch_price_history(token_id: str, start: datetime, end: datetime) -> list[tuple[float, float]]:
    """Returns [(unix_ts, price), ...] sorted ascending, from CLOB prices-history."""
    resp = requests.get(
        f"{CLOB_HOST}/prices-history",
        params={
            "market": token_id,
            "startTs": int(start.timestamp()) - 5,
            "endTs": int(end.timestamp()) + 5,
            "fidelity": 1,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return []
    data = resp.json().get("history", [])
    return sorted((float(p["t"]), float(p["p"])) for p in data)


def price_at_or_before(history: list[tuple[float, float]], ts: float) -> float | None:
    """Forward-fill: last known price at or before ts."""
    result = None
    for t, p in history:
        if t <= ts:
            result = p
        else:
            break
    return result


def fetch_binance_klines(start: datetime, end: datetime, symbol: str = "BTCUSDT") -> list[tuple[float, float]]:
    """Returns [(open_time_seconds, close_price), ...] for 1s klines covering
    [start - vol_lookback, end], in one call (fits under Binance's 1000-row cap)."""
    range_start_ms = int((start - timedelta(seconds=VOL_LOOKBACK_SECONDS)).timestamp() * 1000)
    range_end_ms = int(end.timestamp() * 1000)
    resp = requests.get(
        f"{BINANCE_HOST}/api/v3/klines",
        params={"symbol": symbol, "interval": "1s", "startTime": range_start_ms, "endTime": range_end_ms, "limit": 1000},
        timeout=15,
    )
    resp.raise_for_status()
    klines = resp.json()
    return [(k[0] / 1000.0, float(k[4])) for k in klines]


def binance_price_at(klines: list[tuple[float, float]], ts: float) -> float | None:
    """Nearest kline close at or before ts (klines are 1s apart)."""
    result = None
    for t, p in klines:
        if t <= ts:
            result = p
        else:
            break
    return result


def binance_prices_window(klines: list[tuple[float, float]], end_ts: float, lookback: int) -> list[float]:
    return [p for t, p in klines if end_ts - lookback <= t <= end_ts]


def build_dataset(markets: list[ResolvedMarket]) -> list[dict]:
    records = []
    for i, m in enumerate(markets):
        try:
            up_history = fetch_price_history(m.up_token, m.start, m.end)
            klines = fetch_binance_klines(m.start, m.end)
        except requests.RequestException as exc:
            print(f"  [{i+1}/{len(markets)}] {m.slug}: fetch failed ({exc}), skipping")
            continue
        time.sleep(0.1)

        if not up_history or not klines:
            continue

        baseline_price = binance_price_at(klines, m.start.timestamp())
        if baseline_price is None:
            continue

        for secs_left in CHECKPOINTS_SECONDS_LEFT:
            checkpoint_ts = m.end.timestamp() - secs_left
            if checkpoint_ts < m.start.timestamp():
                continue

            market_p_up = price_at_or_before(up_history, checkpoint_ts)
            current_price = binance_price_at(klines, checkpoint_ts)
            if market_p_up is None or current_price is None:
                continue

            recent = binance_prices_window(klines, checkpoint_ts, VOL_LOOKBACK_SECONDS)
            sigma = probability.estimate_volatility_per_second(recent)
            if sigma <= 0:
                continue

            # Session 21: resolution is TWAP-over-the-window, not terminal
            # spot -- use prob_up_twap with the realized average of the
            # elapsed portion, not prob_up's spot-only comparison.
            elapsed_seconds = checkpoint_ts - m.start.timestamp()
            realized_prices = [p for t, p in klines if m.start.timestamp() <= t <= checkpoint_ts]
            if not realized_prices:
                continue
            realized_avg_price = sum(realized_prices) / len(realized_prices)

            model_p_up = probability.prob_up_twap(
                baseline_price=baseline_price,
                realized_avg_price=realized_avg_price,
                current_price=current_price,
                elapsed_seconds=elapsed_seconds,
                remaining_seconds=secs_left,
                sigma_per_second=sigma,
            )

            records.append({
                "slug": m.slug,
                "seconds_left": secs_left,
                "model_p_up": model_p_up,
                "market_p_up": market_p_up,
                "sigma": sigma,
                "baseline_price": baseline_price,
                "current_price": current_price,
                "realized_avg_price": realized_avg_price,
                "up_won": m.up_won,
            })

        if (i + 1) % 25 == 0:
            print(f"  [{i+1}/{len(markets)}] processed, {len(records)} checkpoint records so far")

    return records


def simulate_strategy(records: list[dict], min_edge: float, min_model_p: float, min_sigma: float,
                       price_range: tuple[float, float] | None) -> dict:
    """Applies one filter regime across all checkpoints (treating each
    market's first passing checkpoint as the entry -- mirrors edge_confirmed
    with min_consecutive_ticks=1). Returns win rate and simulated $ PnL at
    $2/trade flat stake (matches live config's typical size)."""
    seen_markets = set()
    trades = []
    for r in sorted(records, key=lambda x: (x["slug"], -x["seconds_left"])):
        if r["slug"] in seen_markets:
            continue
        if r["sigma"] < min_sigma:
            continue

        up_edge = r["model_p_up"] - r["market_p_up"]
        if up_edge >= min_edge:
            outcome, model_p, market_p, won = "Up", r["model_p_up"], r["market_p_up"], r["up_won"]
        elif -up_edge >= min_edge:
            outcome, model_p, market_p, won = "Down", 1 - r["model_p_up"], 1 - r["market_p_up"], not r["up_won"]
        else:
            continue

        if model_p < min_model_p:
            continue
        if price_range is not None and not (price_range[0] <= market_p <= price_range[1]):
            continue

        seen_markets.add(r["slug"])
        stake = 2.0
        shares = stake / market_p if market_p > 0 else 0
        payout = shares if won else 0.0
        trades.append({"slug": r["slug"], "outcome": outcome, "market_p": market_p,
                        "model_p": model_p, "won": won, "pnl": payout - stake})

    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    total_pnl = sum(t["pnl"] for t in trades)
    return {"n_trades": n, "wins": wins, "win_rate": wins / n if n else None,
            "total_pnl": total_pnl, "avg_pnl": total_pnl / n if n else None, "trades": trades}


def calibration_report(records: list[dict]) -> None:
    """Buckets the price of whichever side WOULD be bought (per current
    min_edge=0.05 logic) and checks realized win rate per bucket, regardless
    of min_model_p/sigma filters -- pure 'is the market's own price honest'
    check."""
    buckets = [(0.0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.50)]
    bucket_stats = {b: [0, 0] for b in buckets}  # [wins, total]

    for r in records:
        up_edge = r["model_p_up"] - r["market_p_up"]
        if up_edge >= 0.05:
            market_p, won = r["market_p_up"], r["up_won"]
        elif -up_edge >= 0.05:
            market_p, won = 1 - r["market_p_up"], not r["up_won"]
        else:
            continue
        for lo, hi in buckets:
            if lo <= market_p < hi:
                bucket_stats[(lo, hi)][1] += 1
                if won:
                    bucket_stats[(lo, hi)][0] += 1
                break

    print("\n--- Calibration: when the market prices our chosen side at X, does it win ~X%? ---")
    print(f"{'price bucket':>15} | {'n':>5} | {'actual win rate':>16} | {'implied by price':>17}")
    for (lo, hi), (wins, total) in bucket_stats.items():
        if total == 0:
            continue
        actual = wins / total
        mid = (lo + hi) / 2
        print(f"  [{lo:.2f},{hi:.2f})  | {total:5d} | {actual:15.1%} | {mid:16.1%}")


def main():
    target_count = 300
    markets = load_or_fetch_markets(target_count)

    if len(markets) < 20:
        print("Too few markets found -- Gamma may not retain closed short-duration "
              "markets very far back. Try again or check the closed=true filter.")
        return

    print(f"\nReconstructing model_p/market_p at checkpoints {CHECKPOINTS_SECONDS_LEFT}s before resolution...")
    records = build_dataset(markets)
    print(f"Built {len(records)} checkpoint records across {len(markets)} markets.")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        if records:
            writer = csv.DictWriter(f, fieldnames=list(records[0].keys()))
            writer.writeheader()
            writer.writerows(records)
    print(f"Raw dataset saved to {OUT_CSV}")

    calibration_report(records)

    print("\n--- Simulated PnL by filter regime ($2/trade flat stake) ---")
    regimes = {
        "current config (min_model_p=0.35, no price range, min_sigma=5e-6)":
            dict(min_edge=0.05, min_model_p=0.35, min_sigma=5e-6, price_range=None),
        "old tight config (min_model_p=0.75, range=[0.30,0.70])":
            dict(min_edge=0.05, min_model_p=0.75, min_sigma=0.0, price_range=(0.30, 0.70)),
        "old wide-price config (min_model_p=0.75, range=[0.15,0.85])":
            dict(min_edge=0.05, min_model_p=0.75, min_sigma=0.0, price_range=(0.15, 0.85)),
        "moderate (min_model_p=0.55, range=[0.10,0.90])":
            dict(min_edge=0.05, min_model_p=0.55, min_sigma=5e-6, price_range=(0.10, 0.90)),
        "edge-only, no model_p floor (min_model_p=0.0)":
            dict(min_edge=0.05, min_model_p=0.0, min_sigma=5e-6, price_range=None),
    }

    for name, params in regimes.items():
        result = simulate_strategy(records, **params)
        if result["n_trades"] == 0:
            print(f"\n{name}: no trades generated")
            continue
        print(f"\n{name}:")
        print(f"  trades={result['n_trades']}  wins={result['wins']}  win_rate={result['win_rate']:.1%}")
        print(f"  total_pnl=${result['total_pnl']:+.2f}  avg_pnl/trade=${result['avg_pnl']:+.3f}")


if __name__ == "__main__":
    main()
