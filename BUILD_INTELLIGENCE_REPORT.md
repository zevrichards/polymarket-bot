# BOT BUILD INTELLIGENCE REPORT

**Date:** 2026-06-24
**Bot Type:** Polymarket trading bots — directional sniper, oracle-gated sniper, market maker (all on short-duration BTC up/down markets)
**Stack:** Python, py-clob-client (Polymarket CLOB), Polymarket Gamma API (market discovery), web3.py + Chainlink (Polygon price feed), no DEX/swap code
**Session Summary:** Built and live-data-verified three paper-trading bots (directional, oracle-verified, market-maker) sharing a common core (market discovery, order-book client, paper fill simulator, trade journal). No wallet, private key, or live order placement implemented — paper mode only.

---

## 1. CRITICAL BUGS & FIXES (DO NOT REPEAT THESE MISTAKES)

- **Problem:** Sorting Gamma API markets by `order=end_date&ascending=true` to find soonest-resolving markets returned markets whose `endDate` was already in the past, despite `active=true&closed=false` filters.
  - **Root Cause:** Gamma's `active`/`closed` flags lag reality — markets aren't reliably flipped to `closed` immediately after their end date passes.
  - **Fix:** Explicitly filter to `endDate > now` client-side; never trust `active`/`closed` alone as a liveness signal.
  - **Tokens Wasted:** medium

- **Problem:** Sorting by `order=startDate&ascending=false` (newest-listed first) to find the next upcoming 5-minute market returned a market ~24h in the future, not the next slot.
  - **Root Cause:** Short-duration markets (`btc-updown-5m-<ts>`) are pre-listed in daily batches well before their trading window opens, so "most recently created" ≠ "next to resolve."
  - **Fix:** Paginate through results (`offset` in steps of `page_size`), collect all candidates with `endDate > now`, then sort that filtered set by soonest `endDate`. Don't rely on any single sort order from the API to mean "currently tradeable."
  - **Tokens Wasted:** high — required ~6 rounds of probing the live API to find the right combination of filter + sort + pagination.

- **Problem:** Requesting `limit=500` from `/markets` only returned 100 results.
  - **Root Cause:** Gamma API silently caps page size at 100 regardless of the requested `limit`.
  - **Fix:** Always paginate via `offset` in 100-row pages; never assume a large `limit` value will be honored.
  - **Tokens Wasted:** low

- **Problem:** Default Polygon RPC (`https://polygon-rpc.com`) returned `401 Unauthorized` on a plain `eth_call`.
  - **Root Cause:** That public endpoint now requires auth / has been deprecated for anonymous use; `https://polygon.llamarpc.com` also failed (DNS resolution failure in this environment); `https://rpc.ankr.com/polygon` requires an API key.
  - **Fix:** Use `https://polygon-bor-rpc.publicnode.com` as the default (confirmed working, also confirmed `1rpc.io/matic` and `polygon.gateway.tenderly.co` work as fallbacks). Always make the RPC URL configurable via env var, never hardcode one without a tested fallback.
  - **Tokens Wasted:** medium

- **Problem:** Assumed `OrderBookSummary.asks` from py-clob-client would be sorted ascending by price.
  - **Root Cause:** The live API returns asks in descending order by price (best/most-expensive-looking first in raw form).
  - **Fix:** Always explicitly `sorted(book.asks, key=lambda level: float(level.price))` before walking the book for a buy fill — never trust API-returned order.
  - **Tokens Wasted:** low (caught before it caused a wrong-price bug, by inspecting raw output before trusting it)

## 2. ARCHITECTURE DECISIONS THAT WORKED

- **Paper broker fills against real, live order books** (not synthetic/mocked ones) by walking actual price levels to compute a realistic average fill price, and raises `InsufficientLiquidity` if the book genuinely can't support the requested size. **Why it matters:** the only thing that changes when moving from paper → live is the execution backend (`PaperBroker` → real `ClobClient.post_order`); strategy code never has to be rewritten or re-validated for "did paper logic actually match live behavior."
- **One `core/` package shared by all three bots**, each bot file in `bots/` being a thin orchestration script (fetch markets → apply strategy-specific rule → call one `execute_trade`-style function). **Why it matters:** market discovery, order-book access, and trade journaling were each written and live-tested exactly once instead of three times with three chances to drift.
- **Per-concern JSON state files** (`paper_state.json`, `oracle_state.json`, `mm_state.json`) instead of one shared state blob. **Why it matters:** each bot's state is independently inspectable/resettable without risk of one bot's bug corrupting another's bookkeeping.
- **Config-driven thresholds in `config.json`** (entry price band, sizing, divergence limits, quote spread) rather than constants in code. **Why it matters:** every numeric judgment call is visible in one file and tunable without touching strategy logic, which matters a lot once a nightly self-review/learning loop (not yet built) starts proposing threshold changes.

## 3. ARCHITECTURE DECISIONS THAT FAILED

- **Tried:** Comparing Polymarket's $0–$1 contract price directly against Chainlink's $-denominated BTC/USD spot price as an "oracle divergence" check for Bot 2.
  - **Why abandoned:** the two numbers aren't on the same scale — a contract priced at $0.92 has no defined "divergence" from a $60,783 BTC price. This would have produced a meaningless number that always failed or always passed.
  - **Replaced with:** a feed-freshness check (reject if Chainlink hasn't updated in >300s) plus a price-stability check (reject if BTC moved >X% since the bot's own last scan) — both are real, computable signals that map to "don't trust this trade if the oracle is stale or the market is currently repricing."
  - **Prevents:** the next instance from re-attempting a direct contract-price-vs-spot-price comparison and wasting a cycle discovering it's a unit mismatch.

## 4. LOSS PREVENTION FEATURES (MANDATORY IN ALL FUTURE BUILDS)

- **Position sizing = `min(max_bet, balance * max_bankroll_fraction)`**, both configurable. Prevents bet size from silently scaling unbounded as paper/live balance grows, and prevents a single large bet when balance is small. **NON-NEGOTIABLE before any live testing.**
- **`min_seconds_to_resolution` guard** (default 5s) — refuses to enter a position too close to market resolution, where a fill might not even land before settlement. Prevents trades that can't be confirmed in time.
- **Oracle feed staleness guard** (Bot 2) — refuses to trade if the Chainlink feed's `updatedAt` is more than 300s old. Prevents trading on a dead/disconnected oracle.
- **Oracle price-stability guard** (Bot 2) — refuses to trade if BTC moved more than `max_divergence_pct` (default 15%) since the bot's last observation. Prevents entering a "near-certain" priced contract during a moment where the underlying is actively repricing and the favored side may be about to flip.
- **Long-only inventory cap** (`max_inventory`, Bot 3) — market maker never accumulates more than a fixed inventory and never shorts. Prevents unbounded directional exposure from a strategy that's supposed to be market-neutral.
- **No wallet, private key, or live order placement exists anywhere in this codebase.** Mode is hardcoded to read `config.json["mode"]` and *raise `NotImplementedError`* if it's ever set to `"live"`, by design. **NON-NEGOTIABLE until a deliberate, separate live-trading phase is explicitly built and reviewed.**
- **Never store a private key in plaintext `.env`, and never grant `max uint256` token approvals to addresses copy-pasted from a blog post/social media guide without independently verifying them.** This was an explicit anti-pattern found in a "how to build a Polymarket bot" guide reviewed before this build — flagged and deliberately not followed. **NON-NEGOTIABLE in any future build that touches a real wallet.**

## 5. API / LIBRARY / CHAIN GOTCHAS

- Gamma API (`https://gamma-api.polymarket.com`) has **no free-text search** — you can only filter by `slug`, `tag_id`, or client-side keyword matching on `question`/`slug` after fetching a page.
- Gamma API **caps page size at 100** even when `limit` is set higher — must paginate with `offset`.
- Gamma's `active`/`closed` flags are **not a reliable liveness signal** — always cross-check `endDate` against current time.
- `clobTokenIds` and `outcomes` fields on a Gamma market are **JSON-encoded strings**, not native arrays — must `json.loads()` them.
- py-clob-client: `ClobClient(host)` with no credentials gives full read access (order books, prices, simplified markets) — **no API key needed for any read operation**, only for `post_order`/auth-required writes.
- py-clob-client `get_order_book(token_id).asks` is **not guaranteed sorted ascending** — sort explicitly before walking levels.
- Chainlink BTC/USD feed address on **Polygon mainnet**: `0xc907E116054Ad103354f2D350FD2514433D57F6f`. Standard `AggregatorV3Interface` ABI (`decimals()`, `latestRoundData()`) works directly against it.
- Public Polygon RPC reliability (tested live, this session): `polygon-rpc.com` → 401; `rpc.ankr.com/polygon` → requires API key; `polygon.llamarpc.com` → DNS failure (environment-specific, may work elsewhere); `polygon-bor-rpc.publicnode.com`, `1rpc.io/matic`, `polygon.gateway.tenderly.co` → all worked.

## 6. CONFIGURATION & ENVIRONMENT

- `mode: "paper" | "live"` in `config.json` is the single switch intended to route execution between `PaperBroker` and a future real `ClobClient` order-placement path — chosen specifically so strategy code never needs to change between phases.
- `POLYGON_RPC_URL` is read from environment (`.env`), defaulting to `polygon-bor-rpc.publicnode.com` (the one confirmed working in this session) rather than the commonly-suggested `polygon-rpc.com`.
- Initial threshold values (`min_entry_price=0.85`, `max_entry_price=0.99`, `max_seconds_to_resolution=120`, `min_seconds_to_resolution=5`, `max_bet=2.0`, `max_bankroll_fraction=0.02`, `max_divergence_pct=0.15`, market-maker `target_spread=0.04`/`requote_threshold=0.01`/`max_inventory=10.0`) are **starting guesses carried over from the source strategy description, not yet tuned against real trade history** — no live trades have been collected yet to validate or adjust them.
