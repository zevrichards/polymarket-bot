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

---

## SESSION 2 — Resolution tracking & PnL reporting (2026-06-24)

**What was added:** `core/resolution.py` (settlement checking), `PaperBroker.resolve()`, a resolve step wired into all 3 bots' scan loops, and `scripts/report.py` for win/loss/PnL summaries.

### Critical bugs & fixes (this session)
- **Problem:** Bot 1 and Bot 2 both defaulted to `core.paper_broker.STATE_PATH` (`logs/paper_state.json`) with no override, silently sharing one balance/position pool between two bots that are supposed to be tracked independently.
  - **Root Cause:** Neither bot passed a `state_path` to `PaperBroker(...)` when constructing it.
  - **Fix:** Bot 2 now uses its own `logs/oracle_paper_state.json`. **Any new bot added to this repo must pass its own `state_path` to `PaperBroker` — never rely on the default if it's meant to be tracked independently.**
  - **Tokens Wasted:** medium — not caught until deliberately building the PnL report and asking "whose balance is this."

- **Problem:** A flaky test (`test_results_are_sorted_soonest_first`) called `market.seconds_to_resolution()` twice (once to build the list, implicitly again via repeated `datetime.now()` calls), so two calls microseconds apart could disagree and make an already-correctly-sorted list look unsorted.
  - **Root Cause:** `seconds_to_resolution()` stamps `datetime.now()` fresh on every call rather than taking a shared reference time.
  - **Fix:** Test now compares `market.end_date` directly (a fixed value) instead of calling a time-dependent method twice. **General lesson: never call a "time since now" method more than once per comparison in a test — compute the fixed timestamp once and compare that.**
  - **Tokens Wasted:** low

### Architecture decisions that failed (this session)
- **Tried:** Using Polymarket Gamma's `closed`/`outcomePrices` fields to detect settlement on `btc-updown-5m`/`-15m` markets.
  - **Why abandoned:** Empirically false for this market type. Verified directly: a `btc-updown-5m` market with `endDate` 6+ months in the past still returns `closed: false`, `active: true`, `outcomePrices: null` from Gamma. Cross-checked against other market types (e.g. `ethereum-above-2275-on-april-21-2026-3pm-et`) which DO resolve correctly via the same fields (`closed: true`, `outcomePrices: ["1","0"]`) — so this is specific to the short-duration crypto up/down markets, likely because they settle via a Chainlink data-stream path that doesn't write back through Gamma's normal UMA-resolution flow.
  - **Also tried and failed:** CLOB's `get_order_book`/`get_midpoint` on an expired token → 404 (book is removed after expiry). CLOB's `get_last_trade_price` on the same token → returned a stale `0.5`, not the actual settlement price. Neither is a usable settlement signal.
  - **Current state:** `core/resolution.check_token_resolution()` works correctly for market types where Gamma's fields are reliable (confirmed: single-day BTC threshold markets). For `btc-updown-5m/15m` specifically, positions are left open and flagged with a one-time staleness warning after 30 min rather than guessed at. **This is a real, unresolved data-availability gap, not a bug to "fix" by guessing — do not invent a settlement price from a proxy signal (e.g. last live order-book price right before expiry) without being explicit that it's an approximation, not ground truth.**
  - **Prevents:** the next instance from re-discovering this the hard way, or worse, silently fabricating win/loss outcomes for the most commonly-traded market type in this bot.

### Loss prevention features (this session, additive to Session 1's list)
- **Resolution checking never guesses.** `check_token_resolution()` returns `None` (not resolved / can't tell) unless the settlement price is unambiguous (`>= 0.95` or `<= 0.05` on a closed market). A closed-but-ambiguous price is treated the same as "not resolved" rather than rounded to a guess. **NON-NEGOTIABLE** — a wrong settlement guess corrupts every PnL number downstream of it.
- **Per-bot paper balances are isolated** (separate state files for Bot 1 vs Bot 2) specifically so the PnL report can attribute results to the correct strategy.

### Config & environment (this session)
- No new config fields. `core/resolution.STALE_WARNING_SECONDS = 1800` (30 min) is a hardcoded threshold, not yet in `config.json` — could move there if it needs tuning later.

---

## SESSION 3 — Correction: resolution tracking actually works (2026-06-24)

**Critical correction to Session 2.** The Session 2 conclusion ("Gamma never reliably reports settlement for btc-updown-5m/15m markets") was **wrong**, and the root cause is a classic one: generalizing from a single, unrepresentative data point without checking *why* it was different.

- **What happened:** Session 2 tested exactly one historical `btc-updown-5m` market (6+ months old) by ID, found `closed: false` / `outcomePrices: null`, and concluded the entire market type doesn't resolve via Gamma. Documented this as a platform-wide data gap in the README and this file.
- **Why it was wrong:** That specific market had `"liquidity": "0"` and `"volume": "0"` — it never had a single trade. It's a dead/orphaned market, not a representative example. **Never generalize "this API doesn't work for market type X" from one example without checking whether that example has some other distinguishing property (volume, liquidity, age, status flags) that could explain the anomaly on its own.**
- **How it was caught:** The user pushed back with a concrete claim from their own research ("the API's outcomePrices field will converge to [1.0, 0.0] once the window closes") and asked directly whether that matched what was tried. Re-tested properly: found a market that was minutes from resolving, polled it by ID every 20s across the resolution boundary, and watched `closed` flip `true` and `outcomePrices` converge to `["1","0"]` about 3-4 minutes after `endDate`. **This is exactly what the user's research described, and it directly contradicted the Session 2 conclusion.**
- **Fix:** No code changes were needed — `core/resolution.check_token_resolution()` was already correct (returns `True`/`False`/`None` based on exactly this signal). Only the docstrings, README, and this file's narrative needed correcting. `STALE_WARNING_SECONDS = 1800` (30 min) is now understood to be far more lenient than the real ~3-4 minute settlement delay, which is fine — it's a backstop for the genuine zero-volume edge case, not the normal path.
- **Tokens wasted:** high across two sessions — a full "known limitation" narrative was built, written into two docs, and reported to the user as fact, all from one bad example.

**Mandatory lesson for future builds: when an API appears to behave inconsistently for a specific entity (a market, a user, a token), check that entity's own metadata (volume, status, age, flags) for an explanation before concluding the API itself is broken for that category.** A single zero-volume/zero-liquidity outlier is not evidence of a systemic gap. When a user says "are you sure?" or cites their own research that contradicts a conclusion you reported, that is a strong signal to redo the test with a better sample, not to defend the original finding.

---

## SESSION 4 — First overnight paper-trading run exposes real portfolio bugs (2026-06-24/25)

**What happened:** Ran all three bots unattended for ~8 hours with $100 paper balance (fix from Session 3 confirmed working -- resolution tracking populated correctly all night). Result: directional_bot and oracle_bot both lost money (-$24.23 / -$24.22 on a $100 bankroll) at a 41.7% win rate (5W/7L) despite only entering trades priced $0.85-$0.99 -- a win rate that low at those prices is not "the strategy has weak edge," it's "something is structurally broken," because breakeven at ~$0.90 entry requires roughly 90% wins.

### Critical bugs & fixes (this session)
- **Problem:** All 7 losses traced back to ONE event: 7 separate "Bitcoin above $X on [same timestamp]" markets (a ladder of strike prices, e.g. $59,000/$59,200/.../$60,800) all entered simultaneously with "No" bets. BTC rallied through every strike by the resolution time, and all 7 lost together.
  - **Root Cause:** These aren't independent markets -- they all resolve off ONE underlying BTC price observation at one timestamp. The bot's `find_candidates`/`run_once` loop treated each Gamma market as an independent opportunity with its own `max_bankroll_fraction` slice, with no concept of "these 7 markets are the same bet." Effective result: 7 x 2% = 14% of bankroll on a single coin flip, dressed up as 7 diversified small bets.
  - **Fix:** Added `select_candidates()` to `bots/directional_bot.py` -- groups same-scan candidates by `market.end_date` (the shared resolution timestamp) and keeps only the single highest-confidence candidate per group (`max_correlated_markets_per_event` config field, default 1). **MANDATORY for any future entry-selection logic: before treating two opportunities as independent, check whether they share an underlying resolution event/timestamp/condition. Polymarket routinely lists what looks like N separate markets that are actually 1 underlying risk factor sliced into strikes.**
  - **Tokens wasted:** high -- this required pulling the full overnight `trades.jsonl`, reconstructing the timeline, and grouping by market_id/slug/timestamp by hand to find the pattern. Future sessions analyzing a paper-trading run should immediately group entries by `end_date`/resolution timestamp as a first step, not just compute an aggregate win rate.

- **Problem:** Within that same disaster cluster, one specific market (`bitcoin-above-60800...`) had BOTH "Yes" and "No" bought (`[('No', 0.97), ('Yes', 0.97), ('No', 0.99)]`) -- a guaranteed loss on the combination, since $0.97 + $0.99 = $1.96 paid for a position that can only ever pay out $1.00.
  - **Root Cause:** Near resolution, with thin order-book depth, BOTH outcomes' best-ask can independently spike toward $0.99 (no one left providing liquidity on either side) -- the entry rule reads "ask price is high" as "market is confident in this outcome," but here it actually means "the book is empty," which says nothing about which side will actually win. Nothing in `find_candidates`/`run_once` checked whether we already held a position (in either outcome) of the same market before adding another.
  - **Fix:** Added `PaperBroker.has_open_position_for_market(market_id)` -- checked before considering a market's candidates at all. This also fixes a second bug found in the same data: the bot was re-entering (pyramiding into) the same market on consecutive 60s scans, since nothing previously stopped a second `buy()` call on a market it already held.
  - **Tokens wasted:** medium -- found as a side effect of investigating the strike-ladder bug, not independently.

### What this means for the strategy itself (not just the bugs)
- Excluding the one correlated-cluster disaster, the remaining 4 trades were all independent, single-strike, isolated bets -- and all 4 won. Too small a sample to claim the $0.85-$0.99 entry signal has real edge, but it means the overnight loss was **not** primarily evidence the strategy itself is bad -- it was three concrete implementation bugs compounding into one oversized, self-inflicted blow-up. **Don't conflate "the bot lost money" with "the strategy doesn't work" until portfolio-construction bugs (correlation, double-entry, pyramiding) are ruled out first.**

### Loss prevention features (this session, additive to prior sessions' lists)
- **Never hold two positions in the same market.** `has_open_position_for_market()` is now checked before any new entry is considered, full stop. **NON-NEGOTIABLE** -- this is true for any future bot touching markets with mutually exclusive binary outcomes.
- **Correlated-event concentration cap.** `select_candidates()` with `max_correlated_markets_per_event` (default 1) is now mandatory before sizing any trade across multiple simultaneously-scanned candidates. **NON-NEGOTIABLE** for any strategy that scans multiple markets per cycle -- always check for shared resolution timestamps/conditions before treating candidates as independent.

### Config & environment (this session)
- Added `directional_bot.max_correlated_markets_per_event = 1` to `config.json` (shared by oracle_bot, which reuses the `directional_bot` config block). Not yet tuned/tested against a second overnight run.
