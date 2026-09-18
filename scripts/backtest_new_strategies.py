"""Backtest two new BTC up/down strategy ideas against real historical
Polymarket resolutions. Reuses the market-fetching/price-reconstruction
approach validated in backtest_lag_edge.py (Gamma /events + CLOB
prices-history + Binance klines) -- see that file for the methodology and
its known limitation: market_p is reconstructed from last-traded price,
forward-filled, which can go stale for tens of seconds with no trades, so
treat results here as directional evidence, not a live-order-book-accurate
simulation.

Strategy A ("lottery"): small bets AGAINST the market when it's pricing a
token near 100:1, but only EARLY in the 5-minute window and only when the
underlying BTC price is still close to where the window started (i.e. our
own model would call it close to a coinflip despite the market's extreme
price). Thesis: an extreme quote this early, before price has moved much,
may be a thin-liquidity/quote artifact rather than a real signal. Expected
to lose small and often, hoping rare wins pay out large enough to matter.

Strategy B ("trend-confirm"): bet WITH the market's early lean, but only
when a 15-minute BTC trend (computed independently, before the 5-min
window even starts) agrees with that lean. "Agree/disagree" is built with
the same driftless-GBM machinery as core/probability.py: the 15-minute
trend is scored as prob_up(price_15min_ago, price_now, 900s,
sigma_per_second) -- the exact same function used everywhere else in this
repo, just applied over a longer, separate lookback window. Also runs an
unconditional baseline ("always follow the market's early lean, ignore
trend") so we can tell whether the trend filter adds real information or
if the market's own early price already captures it.

Both hold to resolution -- no mid-position sell/hold logic (that needs a
defined exit rule, which is a separate design question, not modeled here).

No live orders. No wallet access. Read-only historical data only.
"""
from __future__ import annotations

import csv
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import probability  # noqa: E402
from scripts.backtest_lag_edge import (  # noqa: E402
    BINANCE_HOST,
    CLOB_HOST,
    ResolvedMarket,
    fetch_price_history,
    load_or_fetch_markets,
    price_at_or_before,
)

VOL_LOOKBACK_SECONDS = 120

LOTTERY_CSV = Path(__file__).resolve().parent.parent / "logs" / "backtest_lottery.csv"
TREND_CSV = Path(__file__).resolve().parent.parent / "logs" / "backtest_trend.csv"


# ----------------------------------------------------------- binance data --

def fetch_binance_klines_1s(start: datetime, end: datetime, symbol: str = "BTCUSDT") -> list[tuple[float, float]]:
    """1s klines covering [start - vol_lookback, end] -- fits Binance's
    1000-row cap easily for a 5-minute market (120 + 300 = 420 rows)."""
    range_start_ms = int((start - timedelta(seconds=VOL_LOOKBACK_SECONDS)).timestamp() * 1000)
    range_end_ms = int(end.timestamp() * 1000)
    resp = requests.get(
        f"{BINANCE_HOST}/api/v3/klines",
        params={"symbol": symbol, "interval": "1s", "startTime": range_start_ms, "endTime": range_end_ms, "limit": 1000},
        timeout=15,
    )
    resp.raise_for_status()
    return [(k[0] / 1000.0, float(k[4])) for k in resp.json()]


def fetch_binance_klines_1m(start: datetime, end: datetime, symbol: str = "BTCUSDT") -> list[tuple[float, float]]:
    """1-minute klines -- used for the 15-min pre-window trend, where
    second-level resolution isn't needed and would blow the 1000-row cap
    over a wider timespan."""
    resp = requests.get(
        f"{BINANCE_HOST}/api/v3/klines",
        params={"symbol": symbol, "interval": "1m",
                "startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000), "limit": 1000},
        timeout=15,
    )
    resp.raise_for_status()
    return [(k[0] / 1000.0, float(k[4])) for k in resp.json()]


def price_at(klines: list[tuple[float, float]], ts: float) -> float | None:
    result = None
    for t, p in klines:
        if t <= ts:
            result = p
        else:
            break
    return result


def prices_window(klines: list[tuple[float, float]], end_ts: float, lookback: int) -> list[float]:
    return [p for t, p in klines if end_ts - lookback <= t <= end_ts]


# --------------------------------------------------------- strategy A data --

LOTTERY_CHECKPOINTS_SINCE_START = [15, 30, 45, 60, 90, 120]


def collect_lottery_records(markets: list[ResolvedMarket]) -> list[dict]:
    records = []
    for i, m in enumerate(markets):
        try:
            klines = fetch_binance_klines_1s(m.start, m.end)
            up_history = fetch_price_history(m.up_token, m.start, m.end)
        except requests.RequestException:
            continue
        time.sleep(0.08)
        if not klines or not up_history:
            continue

        baseline = price_at(klines, m.start.timestamp())
        if baseline is None:
            continue

        for secs_since_start in LOTTERY_CHECKPOINTS_SINCE_START:
            ts = m.start.timestamp() + secs_since_start
            if ts >= m.end.timestamp():
                continue
            secs_left = m.end.timestamp() - ts
            current = price_at(klines, ts)
            market_p_up = price_at_or_before(up_history, ts)
            if current is None or market_p_up is None:
                continue

            recent = prices_window(klines, ts, VOL_LOOKBACK_SECONDS)
            sigma = probability.estimate_volatility_per_second(recent)
            if sigma <= 0:
                continue

            # Session 21: TWAP-aware, not spot-only -- matters a lot here
            # since "price near baseline" needs to account for the realized
            # average over the elapsed portion, not just the live tick.
            realized_prices = [p for t, p in klines if m.start.timestamp() <= t <= ts]
            if not realized_prices:
                continue
            realized_avg = sum(realized_prices) / len(realized_prices)
            model_p_up = probability.prob_up_twap(
                baseline_price=baseline, realized_avg_price=realized_avg, current_price=current,
                elapsed_seconds=secs_since_start, remaining_seconds=secs_left, sigma_per_second=sigma,
            )

            records.append({
                "slug": m.slug, "seconds_since_start": secs_since_start,
                "model_p_up": model_p_up, "market_p_up": market_p_up, "up_won": m.up_won,
            })

        if (i + 1) % 50 == 0:
            print(f"  [lottery] [{i+1}/{len(markets)}] processed")
    return records


def simulate_lottery(records: list[dict], extreme_threshold: float, coinflip_band: float, stake: float) -> dict:
    """Takes the FIRST checkpoint (earliest seconds_since_start) per market
    where price is still near baseline AND one side is priced under
    extreme_threshold. Bets the extreme underdog for `stake` dollars."""
    seen = set()
    trades = []
    for r in sorted(records, key=lambda x: (x["slug"], x["seconds_since_start"])):
        if r["slug"] in seen:
            continue
        if not (0.5 - coinflip_band <= r["model_p_up"] <= 0.5 + coinflip_band):
            continue

        if r["market_p_up"] <= extreme_threshold:
            side, price, won = "Up", r["market_p_up"], r["up_won"]
        elif (1 - r["market_p_up"]) <= extreme_threshold:
            side, price, won = "Down", 1 - r["market_p_up"], not r["up_won"]
        else:
            continue

        seen.add(r["slug"])
        shares = stake / price if price > 0 else 0
        payout = shares if won else 0.0
        trades.append({"slug": r["slug"], "side": side, "price": price, "won": won,
                        "pnl": payout - stake, "seconds_since_start": r["seconds_since_start"]})

    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    total_pnl = sum(t["pnl"] for t in trades)
    max_losing_streak = 0
    cur_streak = 0
    for t in trades:  # trades list preserves market chronological order (slug embeds start epoch)
        if not t["won"]:
            cur_streak += 1
            max_losing_streak = max(max_losing_streak, cur_streak)
        else:
            cur_streak = 0
    return {
        "n_trades": n, "wins": wins, "win_rate": wins / n if n else None,
        "total_pnl": total_pnl, "avg_pnl": total_pnl / n if n else None,
        "total_staked": n * stake, "max_losing_streak": max_losing_streak,
        "biggest_win": max((t["pnl"] for t in trades), default=0.0),
        "trades": trades,
    }


# --------------------------------------------------------- strategy B data --

TREND_LOOKBACK_SECONDS = 900  # 15 minutes
EARLY_LEAN_CHECKPOINT_SECONDS = 20  # how far into the window we read the market's early lean


def collect_trend_records(markets: list[ResolvedMarket]) -> list[dict]:
    records = []
    for i, m in enumerate(markets):
        try:
            klines_1s = fetch_binance_klines_1s(m.start, m.end)
            klines_1m = fetch_binance_klines_1m(
                m.start - timedelta(seconds=TREND_LOOKBACK_SECONDS + 120), m.start)
            up_history = fetch_price_history(m.up_token, m.start, m.end)
        except requests.RequestException:
            continue
        time.sleep(0.08)
        if not klines_1s or not klines_1m or not up_history:
            continue

        price_now = price_at(klines_1s, m.start.timestamp())
        price_15m_ago = price_at(klines_1m, m.start.timestamp() - TREND_LOOKBACK_SECONDS)
        if price_now is None or price_15m_ago is None:
            continue

        trend_prices = prices_window(klines_1m, m.start.timestamp(), TREND_LOOKBACK_SECONDS + 60)
        sigma_per_minute = probability.estimate_volatility_per_second(trend_prices)
        if sigma_per_minute <= 0:
            continue
        sigma_per_second = sigma_per_minute / (60 ** 0.5)  # variance scales linearly with time

        trend_p_up = probability.prob_up(price_15m_ago, price_now, TREND_LOOKBACK_SECONDS, sigma_per_second)

        checkpoint_ts = m.start.timestamp() + EARLY_LEAN_CHECKPOINT_SECONDS
        market_p_up_early = price_at_or_before(up_history, checkpoint_ts)
        if market_p_up_early is None:
            continue

        records.append({
            "slug": m.slug, "trend_p_up": trend_p_up,
            "market_p_up_early": market_p_up_early, "up_won": m.up_won,
        })

        if (i + 1) % 50 == 0:
            print(f"  [trend] [{i+1}/{len(markets)}] processed")
    return records


def simulate_trend(records: list[dict], trend_margin: float, market_margin: float, stake: float,
                    require_trend_agreement: bool) -> dict:
    trades = []
    for r in records:
        trend_up = r["trend_p_up"] > 0.5 + trend_margin
        trend_down = r["trend_p_up"] < 0.5 - trend_margin
        market_leans_up = r["market_p_up_early"] > 0.5 + market_margin
        market_leans_down = r["market_p_up_early"] < 0.5 - market_margin

        if market_leans_up:
            side, price, won = "Up", r["market_p_up_early"], r["up_won"]
            agrees = trend_up
        elif market_leans_down:
            side, price, won = "Down", 1 - r["market_p_up_early"], not r["up_won"]
            agrees = trend_down
        else:
            continue  # market has no early lean either way

        if require_trend_agreement and not agrees:
            continue

        shares = stake / price if price > 0 else 0
        payout = shares if won else 0.0
        trades.append({"slug": r["slug"], "side": side, "price": price, "won": won, "pnl": payout - stake})

    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    total_pnl = sum(t["pnl"] for t in trades)
    return {"n_trades": n, "wins": wins, "win_rate": wins / n if n else None,
            "total_pnl": total_pnl, "avg_pnl": total_pnl / n if n else None, "trades": trades}


def main():
    target_count = 300
    markets = load_or_fetch_markets(target_count)
    if len(markets) < 50:
        print("Too few markets -- aborting.")
        return

    # --- Strategy A: lottery ---
    print("\n=== Strategy A: lottery (small bets against ~100:1 odds, early, price near baseline) ===")
    lottery_records = collect_lottery_records(markets)
    print(f"Built {len(lottery_records)} lottery checkpoint records.")
    with LOTTERY_CSV.open("w", newline="", encoding="utf-8") as f:
        if lottery_records:
            w = csv.DictWriter(f, fieldnames=list(lottery_records[0].keys()))
            w.writeheader()
            w.writerows(lottery_records)
    print(f"Raw dataset saved to {LOTTERY_CSV}")

    for extreme, band in [(0.01, 0.05), (0.01, 0.10), (0.02, 0.05), (0.02, 0.10), (0.03, 0.10)]:
        result = simulate_lottery(lottery_records, extreme_threshold=extreme, coinflip_band=band, stake=0.50)
        if result["n_trades"] == 0:
            print(f"\n  threshold<={extreme:.2f}, coinflip_band=±{band:.2f}: no trades")
            continue
        print(f"\n  threshold<={extreme:.2f}, coinflip_band=±{band:.2f} ($0.50/trade):")
        print(f"    trades={result['n_trades']}  wins={result['wins']}  win_rate={result['win_rate']:.1%}")
        print(f"    total_staked=${result['total_staked']:.2f}  total_pnl=${result['total_pnl']:+.2f}  "
              f"avg_pnl/trade=${result['avg_pnl']:+.3f}")
        print(f"    max_losing_streak={result['max_losing_streak']}  biggest_single_win=${result['biggest_win']:+.2f}")

    # --- Strategy B: trend-confirm ---
    print("\n\n=== Strategy B: trend-confirm (bet with market's early lean, gated by 15-min trend) ===")
    trend_records = collect_trend_records(markets)
    print(f"Built {len(trend_records)} trend records.")
    with TREND_CSV.open("w", newline="", encoding="utf-8") as f:
        if trend_records:
            w = csv.DictWriter(f, fieldnames=list(trend_records[0].keys()))
            w.writeheader()
            w.writerows(trend_records)
    print(f"Raw dataset saved to {TREND_CSV}")

    print("\n  Baseline -- always follow market's early lean, ignore trend:")
    baseline = simulate_trend(trend_records, trend_margin=0.0, market_margin=0.05, stake=2.0, require_trend_agreement=False)
    if baseline["n_trades"]:
        print(f"    trades={baseline['n_trades']}  wins={baseline['wins']}  win_rate={baseline['win_rate']:.1%}")
        print(f"    total_pnl=${baseline['total_pnl']:+.2f}  avg_pnl/trade=${baseline['avg_pnl']:+.3f}")
    else:
        print("    no trades")

    print("\n  Trend-confirmed subset -- only when 15-min trend agrees with market's early lean:")
    for trend_margin in [0.05, 0.10, 0.15]:
        confirmed = simulate_trend(trend_records, trend_margin=trend_margin, market_margin=0.05, stake=2.0,
                                    require_trend_agreement=True)
        if confirmed["n_trades"] == 0:
            print(f"\n    trend_margin=±{trend_margin:.2f}: no trades")
            continue
        print(f"\n    trend_margin=±{trend_margin:.2f}:")
        print(f"      trades={confirmed['n_trades']}  wins={confirmed['wins']}  win_rate={confirmed['win_rate']:.1%}")
        print(f"      total_pnl=${confirmed['total_pnl']:+.2f}  avg_pnl/trade=${confirmed['avg_pnl']:+.3f}")


if __name__ == "__main__":
    main()
