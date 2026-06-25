# Polymarket BTC Trading Bots (Paper Trading)

Three bot strategies trading Polymarket's short-duration "Bitcoin Up or
Down" markets (`btc-updown-5m-*`, `btc-updown-15m-*`, etc.), all running in
**paper-trading mode only** -- no real funds, no wallet, no private key
anywhere in this codebase yet.

| Bot | File | Strategy |
|---|---|---|
| 1 | `bots/directional_bot.py` | Buys the favored outcome when priced $0.85-$0.99 within the final ~2 minutes before resolution |
| 2 | `bots/oracle_bot.py` | Same as Bot 1, gated by an independent Chainlink BTC/USD freshness + stability check |
| 3 | `bots/market_maker_bot.py` | Posts a resting bid/ask around mid price, requotes on movement, tracks long-only inventory |

## Setup

```bash
cd polymarket-bot
python -m venv .venv
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
cp .env.example .env         # only POLYGON_RPC_URL is needed for paper mode
```

No API keys or wallet are required to run any bot in paper mode -- Polymarket's
Gamma API (market discovery) and CLOB API (order books/prices) are both
public/unauthenticated for reads.

## Running a bot

```bash
# single scan, then exit -- good for testing
python -m bots.directional_bot --once
python -m bots.oracle_bot --once
python -m bots.market_maker_bot --once

# continuous loop (interval from config.json -> scan_interval_seconds), Ctrl+C to stop
python -m bots.directional_bot
```

Each run reads/writes its own state under `logs/` (gitignored):
- `logs/trades.jsonl` -- every simulated trade and resolution, all bots, one JSON line each (`kind: "entry"` or `kind: "resolution"`)
- `logs/paper_state.json` -- Bot 1's virtual balance + open positions
- `logs/oracle_paper_state.json` -- Bot 2's virtual balance + open positions (kept separate from Bot 1's so PnL isn't mixed between the two)
- `logs/mm_state.json` -- Bot 3's resting quotes + inventory per market
- `logs/oracle_state.json` -- Bot 2's last-observed BTC/USD price (for the divergence guard)
- `logs/learnings.md` -- free-text notes (e.g. why a scan was skipped)

Every scan also checks whether any open position/quote has resolved (see
"Settlement & PnL" below) before looking for new trades.

## Settlement & PnL

Run this anytime to see win/loss counts and realized PnL per bot:

```bash
python -m scripts.report
```

**Known limitation:** Polymarket's Gamma API does not reliably report
settlement for the short-duration `btc-updown-5m`/`btc-updown-15m` markets --
confirmed by checking a market that ended 6+ months ago, which still
reports `closed: false` and `outcomePrices: null`. Other market types
(single-day BTC threshold markets, etc.) resolve normally through the same
check. Positions on unresolvable markets are left open rather than guessed
at, and after 30 minutes past expected resolution they log a one-time
warning rather than being silently stuck forever. See `core/resolution.py`
and `BUILD_INTELLIGENCE_REPORT.md` for the full investigation -- this means
PnL tracking currently works correctly for longer-dated BTC markets but is
incomplete for the 5m/15m up/down markets specifically, pending either a
better API or a different settlement-detection approach.

## Config (`config.json`)

```jsonc
{
  "mode": "paper",              // "live" is not implemented -- see below
  "starting_balance": 100.0,    // virtual USD for paper trading
  "scan_interval_seconds": 60,
  "directional_bot": {
    "min_entry_price": 0.85,
    "max_entry_price": 0.99,
    "max_seconds_to_resolution": 120,
    "min_seconds_to_resolution": 5,  // don't trade in the very last seconds, fill risk
    "max_bet": 2.0,
    "max_bankroll_fraction": 0.02    // position size = min(max_bet, balance * this)
  },
  "oracle_bot": {
    "max_divergence_pct": 0.15,      // skip trading if BTC moved >15% since last scan
    "chainlink_feed_address": "0xc907E116054Ad103354f2D350FD2514433D57F6f"  // BTC/USD on Polygon
  },
  "market_maker_bot": {
    "quote_size": 1.0,
    "target_spread": 0.04,
    "requote_threshold": 0.01,
    "max_inventory": 10.0
  }
}
```

## Running tests

```bash
python -m pytest tests/ -v
```

`test_paper_broker.py` and `test_directional_bot.py` run against fixture
order-book data (`tests/fixtures/sample_orderbook.json`) and never touch the
network. `test_markets.py` hits Polymarket's real public Gamma API to verify
market discovery still works against live data -- no funds at risk, since
it's a read-only GET.

## What's deferred (not in this codebase)

Going from paper to live trading is a separate, deliberate step that
involves real money and a private key, so it's intentionally **not**
implemented here. When you're ready:

1. Create a dedicated burn wallet yourself (e.g. with a hardware wallet, or
   any tool you trust) -- never reuse a wallet that holds other funds.
2. Fund it manually with a small amount of USDC.e + a little POL for gas.
3. Add authenticated order placement to `core/clob_client.py` (py-clob-client
   supports this once you provide a private key) and a `live` branch of
   `execute_trade()` in each bot that calls it instead of `PaperBroker`.
4. Only grant token approvals to Polymarket's actual exchange contracts,
   scoped if possible, and verify each contract address independently
   rather than trusting it from a blog post or social media guide.

This project deliberately does **not** follow the pattern (seen in some
"how I built a Polymarket bot" guides) of putting a private key in a
plaintext `.env` file and granting `max uint256` approvals to addresses
copy-pasted from an article -- both are real fund-draining risks if the
source or your own machine is ever compromised.
