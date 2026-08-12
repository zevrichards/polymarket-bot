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

---

## SESSION 5 — The Session 4 correlation fix had a gap; py-clob-client has no timeout (2026-06-25)

**What happened:** Restarted the bots after Session 4's fixes, checked back ~2-3 hours later, and found the exact correlation bug again -- two different "Bitcoin above $X" strikes sharing the same `11am ET` resolution timestamp were both bought, 57 seconds apart.

### Critical bugs & fixes (this session)
- **Problem:** Session 4's `select_candidates()` only caps correlated candidates found within the *same* scan call. It missed the case where strike A is the only qualifying candidate on scan N, gets bought, and strike B (same resolution timestamp) becomes the only qualifying candidate on scan N+1 (60s later) -- each scan sees only one candidate, so the within-scan grouping never has anything to group.
  - **Root Cause:** Designed the fix around "candidates found together in one scan" when the actual bug is about "exposure to one resolution event over the position's whole lifetime," which spans many scans.
  - **Fix:** Added `Position.event_key` (the market's `end_date.isoformat()`) and `PaperBroker.has_open_position_for_event(event_key)`, checked before considering a market as a candidate at all -- alongside the existing same-scan cap, not instead of it. **Lesson: when fixing a "two things are secretly correlated" bug, check whether the correlation can manifest across separate decision cycles, not just within one. A within-batch fix is not the same as a within-lifetime fix.**
  - **Tokens wasted:** medium -- caught quickly this time because the BUILD_INTELLIGENCE_REPORT.md habit from Session 4 meant the report's "open/unresolved" section was already being checked as a matter of course.

- **Problem (found while restarting, not from the trading data):** `market_maker_bot`'s first scan appeared to hang indefinitely on its very first order-book fetch.
  - **Root Cause:** Inspected `py_clob_client`'s source directly -- zero occurrences of "timeout" anywhere in its `http_helpers/`. It sets no timeout on any HTTP call, so a slow/degraded Polymarket endpoint can stall a call forever, with no exception for `core/scheduler.py`'s retry-on-failure loop to catch.
  - **Fix:** `socket.setdefaulttimeout(15)` once in `core/clob_client.py` -- a process-wide default that any socket without its own explicit timeout falls back to. Confirmed via direct testing that this was masking a real (if temporary) Polymarket API slowdown, not a true infinite hang: a `market_maker_bot` scan that normally takes ~6s took 4m10s but still completed successfully end to end once watched all the way through.
  - **Tokens wasted:** medium -- required directly reading the third-party library's source to confirm absence of a timeout, since the symptom (apparent hang) could equally have been our own bug.

### Loss prevention features (this session, additive to prior sessions' lists)
- **Event-level correlation guard now spans the position's full lifetime, not just one scan.** `has_open_position_for_event()` is checked independently of (and in addition to) the within-scan `select_candidates()` cap. **NON-NEGOTIABLE**, and a reminder that this class of bug needs testing across multiple consecutive scan cycles, not just a single-scan unit test -- the regression test for this (`test_has_open_position_for_event_catches_cross_scan_correlation`) deliberately calls `buy()` once and then checks the guard separately, mirroring two different scans.
- **Process-wide socket timeout** (`core/clob_client.py`, 15s) -- any future code that touches a third-party HTTP client should not assume it sets its own timeout. Check the library's source if in doubt; don't assume.

### Known limitation observed, not a bug
- Pre-existing open positions from before this session's fix don't have `event_key` set (defaults to `""`), so they won't retroactively block a third correlated strike from slipping through until they resolve and clear out. This is an acceptable one-time gap for already-open state, not a flaw in the fix itself -- new positions opened after this fix are always tagged correctly.

---

## SESSION 6 — The Session 4/5 correlation fix was bot-specific; market_maker_bot still had it (2026-06-25)

**What happened:** Did a clean reset (archived all pre-fix trades/state, fresh $100 balance) specifically to verify Sessions 4-5's fixes in isolation. After 30 minutes: directional_bot/oracle_bot correctly had zero entries (their fixes held -- the rare entry condition just hadn't fired, not a bug). But `market_maker_bot` had quietly built up inventory across **18 different "Bitcoin above $X" strikes, all resolving at the same 12pm ET timestamp** -- the exact same correlated-event risk pattern, just never patched in this bot.

### Critical bugs & fixes (this session)
- **Problem:** `market_maker_bot` had no concept of correlated resolution events at all. It intentionally quotes both sides of every market it finds (that's the strategy), so when `fetch_btc_markets()` returns a full strike ladder, it happily quotes -- and accumulates inventory in -- all of them, with `max_inventory` only capping risk *per market*, never in aggregate across markets that are secretly the same underlying bet.
  - **Root Cause:** Sessions 4-5's correlation fixes were written and reasoned about entirely in terms of `directional_bot`/`oracle_bot`'s one-shot-buy model (`has_open_position_for_market`/`has_open_position_for_event` on `PaperBroker`). Nobody re-asked "does this same risk pattern exist in the third bot, which has a structurally different state model (continuous quoting/inventory vs. one-shot buys)?" until live data showed it directly.
  - **Fix:** Added `Quote.event_key`, `event_inventory(state, event_key)` (sums inventory across all quotes sharing a resolution timestamp), and a `max_inventory_per_event` cap (config, default 10.0, same as per-market `max_inventory`) enforced two ways: (1) refuse to create a *new* quote on a market if its event is already at the inventory cap, (2) refuse *buy* fills (which increase inventory) once the event cap is hit, while *sell* fills (which reduce inventory) are never blocked -- exiting risk should always be allowed. **MANDATORY: a portfolio-construction fix found in one bot must be explicitly re-evaluated against every other bot in the same repo with its own state model, not assumed to generalize. "We fixed the correlation bug" was true for 2 of 3 bots and false for the third until checked directly.**
  - **Tokens wasted:** medium -- found by deliberately auditing "why hasn't anything resolved yet" rather than just reading the top-line PnL number, which would have looked fine (0 resolved, $0 PnL) right up until the correlated ladder actually settled.

### What this means going forward
- The already-accumulated 18-strike inventory (built before this fix) could not be retroactively capped -- it rides to its 12pm ET resolution as-is, which is itself informative data about how that specific correlated exposure plays out. The fix only prevents *new* correlated buildups from this point on.
- **Process lesson:** whenever a "stop and rethink if the same error recurs" checkpoint is set, the check has to span every bot doing related work, not just the one(s) where the bug was first found. The user caught this by asking why a number was suspiciously flat (zero resolutions after 30 min) rather than trusting an all-green-looking report.

---

## SESSION 7 — Restarting a patched bot without resetting its own state re-contaminates the report; market maker has an adverse-selection problem, not a bug (2026-06-25)

### Critical process mistake (not a code bug)
- **Problem:** After patching `market_maker_bot` with the event cap (Session 6), I relaunched it but only restarted the *process* -- I didn't wipe `mm_state.json`/`trades.jsonl` the way I had for `directional_bot`/`oracle_bot` earlier in the same session. ~17 shares of pre-fix correlated inventory (bought before the fix went live) stayed in the state file and would have mixed into the next report, making it look like the fix might be failing when it wasn't.
  - **Root Cause:** Treated "patch one bot, restart that bot" as sufficient, without re-applying the same "reset for a clean baseline" discipline used earlier for the other two bots in the same session.
  - **Fix:** No code fix needed -- this is a procedure, not a bug. Confirmed via trade timestamps that the leftover inventory predated the fix going live, then did a full archive-and-reset across all three bots' state/logs.
  - **MANDATORY PROCESS RULE: whenever a bot's trading logic is patched mid-session, its state/logs must be reset before the next evaluation, every time, not just the first time.** The user caught this by asking directly "did we reset the logs and wallets" rather than assuming a restart implied a reset.

### Real finding: market maker's losses are adverse selection, confirmed via a clean 92-trade sample
- **Problem:** After a genuinely clean reset, `market_maker_bot` still lost money (-$13.64 over 92 resolved positions) despite a perfectly neutral 46W/46L split -- impossible to explain as "bad luck" at that sample size.
  - **Diagnosis:** `avg inventory settled on wins = 0.00` vs `avg inventory settled on losses = 0.65`, and even nominal "wins" had slightly negative PnL (-0.047 avg). This is the textbook signature of adverse selection: resting two-sided quotes get picked off by faster/informed flow specifically as the true outcome becomes predictable near resolution, so the bot ends up holding real inventory on losers while only ever capturing tiny, low/negative-margin scraps on winners.
  - **Root cause, not a bug:** `MIN_SECONDS_TO_RESOLUTION = 30` let the bot keep quoting into the highest-risk window (last 30s before resolution, when price is converging and easiest to predict). `target_spread = 0.04` (2c half-spread) wasn't enough compensation for that risk.
  - **Fix (parameter tuning, unverified yet):** `MIN_SECONDS_TO_RESOLUTION` 30 -> 120, `target_spread` 0.04 -> 0.10, `requote_threshold` 0.01 -> 0.02 (less constant re-centering/chasing). **This has NOT been validated against live results yet -- it's a hypothesis-driven change based on the adverse-selection diagnosis, not a confirmed fix.** Next report should specifically check whether the win/loss PnL asymmetry shrinks, not just whether total PnL improves (total PnL improving for unrelated reasons would be a false signal).
  - **Lesson for future strategy bots:** a numerically neutral win rate (46W/46L) does NOT mean a neutral-risk strategy -- always check whether wins and losses are systematically different in *size*, not just count, before concluding "no edge either way."

### Follow-up after the tuning fix: still failing, worse than first thought
A second clean run (after the spread/timing tuning above) confirmed the fix didn't solve the real problem. Filtering `market_maker_bot`'s resolutions to only ones where it actually held inventory (most "resolutions" were unfilled quotes with zero stakes, inflating the count): **real win rate was 12.4% (13W/92L)** on actual fills. Re-checking the *pre-tuning* data the same way showed it was even worse before: **0% of real fills were wins.** So tuning helped (0% -> 12.4%) but the structural problem (adverse selection -- can't tell at fill time whether the counterparty is informed) isn't fixable by spread/timing parameters alone.

Directional/oracle's cleanest sample (11 trades each) also stayed below breakeven (54.5%/63.6% win rate against an effective ~96% breakeven requirement at their ~$0.96 average entry price) -- consistent with the very first (bug-contaminated) sample's conclusion despite three different code states.

**All three bots were paused on 2026-06-25 pending a strategy rethink.** Full plain-language breakdown of what each strategy does and why it's failing is in `Strategies.txt` at the repo root -- read that before proposing or implementing any new strategy. The short version: none of the three has an independent information edge (a signal estimating true probability separately from the market's own price); they either trust the market's price outright (Bots 1/2) or assume balanced order flow that doesn't exist near resolution (Bot 3). Any new strategy needs to answer "what is the independent signal, and why would it disagree with the market in our favor" before being built.

---

## SESSION 8 -- First attempted real edge: order-flow imbalance for market_maker_bot (2026-06-26)

After researching four candidate edges (momentum/volatility, cross-exchange lag, order-flow imbalance, calendar/event signals -- see chat for the full feasibility comparison and sources), started with order-flow imbalance since it reuses 100% of existing infrastructure (no new data source) and directly targets the adverse-selection problem already measured in Session 7.

**What was built:**
- `core/orderflow.py` -- `compute_imbalance(book)` (bid_depth - ask_depth) / total_depth, in [-1, 1]; `compute_microprice(book)` (standard volume-weighted formula, exposed but not yet wired into trading logic).
- `bots/market_maker_bot.py`: `check_fills()` now refuses a BUY fill (inventory-increasing) when the book's imbalance is below `min_imbalance_to_buy` (config, default -0.2) -- i.e. don't buy when there's meaningfully more resting size on the ask side (selling pressure). SELL fills are still never blocked, consistent with the existing event-cap guard's philosophy: only ever restrict taking on more risk, never restrict reducing it.
- 11 new tests (`tests/test_orderflow.py`, plus 3 added to `tests/test_market_maker_bot.py`), all fixture-based, no network.

**Status: UNVERIFIED.** This is a hypothesis, not a confirmed fix, exactly like the Session 7 spread/timing tuning was before its own real-data check. The next report needs to repeat the Session 7 diagnostic (filter resolutions to only those with real inventory settled, compare real win rate and avg win/loss size before vs. after) to know whether this signal actually reduces adverse selection or just changes which fills get skipped without improving the underlying problem.

**Research note for when cross-exchange-lag (option 2) gets built next:** confirmed via Chainlink's own docs that Polymarket's BTC/USD resolution feed updates only every 10-30s or on 0.5% deviation moves -- a real, structural lag vs. continuous spot prices, not speculation. Found prior art worth reviewing as reference (not copying blindly): [txbabaxyz/polyrec](https://github.com/txbabaxyz/polyrec) (open-source order-flow/imbalance dashboard for Polymarket's 15-min BTC markets, aggregating Chainlink + Binance + orderbook data) and a Medium article describing a GBM/Monte-Carlo probability model compared against Polymarket's implied odds, entering in the 240-270s window rather than last-second. Treat the article's specific backtest numbers (55-60% win rate) with the same skepticism as every other "guide" article in this project -- the mechanism is plausible and independently grounded, the claimed performance is not verified.

---

## SESSION 9 -- Bot 4 (lag_bot): the first attempt at a real information edge (2026-06-26)

**Correction to Session 8:** the user asked to verify [txbabaxyz/polyrec](https://github.com/txbabaxyz/polyrec) was "just a dashboard" before trusting it as reference -- it isn't. Read `fade_impulse_backtest.py` and `replicate_balance.py` directly: both are real backtest engines (impulse detection, simulated limit-order fills, PnL against actual Gamma resolution data), not visualization-only code. No wallet/key handling in either (clean, unlike the weatherbot guide). **Lesson: "evaluate the code, don't trust the description" applies to prior-art repos too, not just the hype articles -- a one-line GitHub description ("dashboard") undersold what was actually in there.**

**What was built:** `bots/lag_bot.py` (Bot 4), the first bot with an actual independent probability estimate instead of trusting Polymarket's own price.

- `core/binance_client.py` -- `get_price_at(timestamp_ms)` and `get_recent_prices(lookback_seconds)`, both confirmed working against Binance's public klines API (no key, 1-second resolution, historical data available indefinitely by `startTime`).
- `core/probability.py` -- driftless (mu=0) GBM model: `prob_up(baseline_price, current_price, seconds_remaining, sigma_per_second)`. Deliberately does NOT assume momentum continues or reverts -- there's no validated evidence either way yet, so baking in a drift assumption would just be a different unproven guess. The only inputs are how far price has already moved from the window's baseline, how much time is left, and recent realized volatility. 12 tests covering symmetry, degenerate cases (zero time/volatility), and monotonicity (more confident with less time or lower volatility).
- `core/markets.py`: added `BtcMarket.start_date` from Gamma's `eventStartTime` field -- confirmed this is included in the bulk `/markets` list response already paginated through, so no extra API call needed per market. (Note: this is NOT the same as Gamma's `startDate` field, which is listing/creation time -- that distinction was the source of real confusion back in Session 1.)
- Entry logic deliberately differs from Bots 1/2: trades in a 30-270s-to-resolution window (mid-window, while the market is still uncertain), not the last seconds when price has already converged and any edge is gone. Only trades when the model and Polymarket's own implied price *disagree* by at least `min_edge` (0.07) -- agreement is not a signal.
- Reuses the existing correlation guards (`has_open_position_for_market`/`has_open_position_for_event`) and `PaperBroker` infrastructure -- no new portfolio-risk code needed since this bot's one-shot-buy execution model is identical in shape to Bots 1/2.

**Status: UNVERIFIED**, same as every other bot at first. Smoke-tested against live data (clean run, zero candidates this scan -- confirmed honestly correct by checking no market was actually in the 30-270s window at scan time, not a bug) and validated the full math pipeline against live Binance data with a synthetic in-window scenario (sane, non-degenerate output: a small realized price drop against low volatility produced a confidently bearish P(Up)=6.3%, exactly the expected shape).

**Left running:** only `lag_bot`, alongside the already-in-progress `market_maker_bot` order-flow test (left undisturbed rather than reset, since that test needs more time to build a sample). Bots 1/2 remain paused.

---

## SESSION 10 -- Major market-discovery bug found: core/markets.py was silently missing live, currently-tradeable markets (2026-06-26/27)

**What happened:** Checked back after ~6 hours of `lag_bot` running. Zero entries, zero errors across 403 clean scans. Rather than accept "the edge threshold is just strict" at face value, directly verified whether a real, currently-tradeable market existed in the bot's target window at that exact moment -- it did (176 seconds left to resolution, via direct slug lookup) -- while `fetch_btc_markets()` returned **zero** markets in that window at the same instant. That's a real bug, not strategy rarity.

### Critical bug & fix
- **Problem:** `fetch_btc_markets()` paginated Gamma's `/markets` endpoint sorted by `startDate` (creation time) descending, then filtered client-side for future end dates. Polymarket pre-lists many markets far in advance, so a market resolving in the next few minutes can have been *created* hours or days ago -- by creation-time-descending order, it ends up buried past page 10 by every more-recently-created, further-future market ranked ahead of it. This had been silently capping how many genuinely-live markets every bot in this repo (1-4) could ever see, intermittently, depending on how many future markets happened to be queued ahead of the live one at scan time.
  - **How it was caught:** directly compared "does a live market exist right now" (yes, via direct slug query) against "does our discovery function find it" (no) at the same instant, rather than assuming the bot's silence meant the strategy wasn't finding edges.
  - **First fix attempt that didn't work:** switched to Gamma's `end_date_min`/`end_date_max` server-side date-range filter (confirmed via direct testing that these params exist and work). But with no explicit `order` param and a wide horizon, results were dominated by markets across **every** Polymarket category (sports, politics, etc.), and BTC markets got pushed past page 1 again -- same symptom, different cause. At a 1-4 hour horizon, 100/100 results per page were non-BTC.
  - **Actual fix:** added `order=endDate&ascending=true` to the same query, and narrowed the default horizon to 4 hours (every bot in this repo only acts on markets resolving in minutes, never hours, so a longer horizon was never needed and only added more non-BTC noise to page through). Confirmed: a market with 242.8s left now appears as the #1 result.
  - **Tokens wasted:** high -- required directly comparing ground truth (a manual slug lookup) against the function's output to even realize this was a bug rather than a real "no candidates" result, then two more rounds of testing different Gamma query parameter combinations against live data to find a combination that actually worked.

### Mandatory lesson
**A clean run with zero errors and zero trades is not the same as "the strategy found no edge." Before concluding a strategy has no opportunities, verify independently that opportunities existed for it to find in the first place.** This is the same root mistake as Session 3 (generalizing "the API doesn't support X" from a bad example) but inverted: there, a bug was wrongly blamed on the platform; here, an absence of trades was almost wrongly attributed entirely to the strategy/threshold instead of checking whether the discovery layer beneath it was even working. Every bot's "no candidates" log line going forward should be read with this in mind -- it can mean "looked and found nothing good" or "found nothing to look at," and those require completely different responses.

**Open question, not yet resolved:** this bug likely affected the historical results for Bots 1-3 too (directional/oracle/market-maker), to an unknown degree -- they did find real trades across multiple sessions, so discovery wasn't *always* broken, but it's now unclear how many additional opportunities were missed intermittently. The negative conclusions already reached about Bots 1-3 (Strategies.txt) are not necessarily invalidated -- the trades that did happen still showed a clear win-rate/payout mismatch -- but the *sample sizes* behind those conclusions may have been smaller than they should have been.

---

## SESSION 11 -- Risk controls added to lag_bot; report.py's own "real fill" filter was wrong (2026-06-27)

### Two fixes this session

**1. `scripts/report.py`'s market-maker "real fills" filter (added Session 7-9, "fixed" again earlier this session) was itself inaccurate.** It filtered on `inventory_settled > 0` at resolution time, but a quote can be bought into and sold back out *before* resolution -- a real, PnL-bearing round trip -- and still end at exactly zero inventory. That filter silently excluded 71 real trades. Correct test: was this `token_id` ever filled at all (does any fill record exist for it), not whether it happens to be holding inventory at the moment of resolution. Verified the fix is consistent by confirming the corrected "real" PnL total now exactly equals the old unfiltered raw total once truly-never-filled quotes are excluded (they're the only ones that are exactly $0 by construction). **Lesson: when building a "filter out the non-events" diagnostic, the definition of "non-event" needs to be checked against the actual state machine, not assumed from the field that happens to be most convenient (final inventory) -- a position passing through zero is not the same as a position that was never touched.**

**2. `lag_bot` got the risk controls flagged as missing back in Session 9** (it had originally just reused Bots 1/2's flat sizing with no stop-loss, daily cap, or liquidity filter):
- **Kelly-fraction sizing** (`core paper_broker` unchanged; `bots/lag_bot.size_position()` rewritten) -- bet size now scales with `(model_p - market_p) / (1 - market_p)` scaled by `kelly_fraction` (0.25 = quarter-Kelly), still hard-capped by `max_bet`/`max_bankroll_fraction`.
- **Stop-loss** -- required adding `PaperBroker.sell()` (the broker previously could only buy-and-hold-to-resolution; there was no way to exit a position early). `check_stop_losses()` runs every scan, exits via `sell()` if the current best bid implies a loss beyond `stop_loss_pct` (0.5) of cost basis.
- **Daily trade cap** (`max_daily_trades`, 20) -- counts today's entries from the journal before allowing new ones.
- **Minimum-liquidity filter** (`min_book_depth_usd`, 50.0) -- skips a candidate if its order book's total notional (both sides) is too thin.

Smoke-tested live: correctly resolved an old position, correctly retroactively stopped out 3 pre-existing risky positions (combined -$6.00, replacing whatever uncontrolled outcome they'd have had at resolution), and correctly enforced the daily cap once today's count (carried over from the pre-reset run) exceeded 20. All 67 tests pass (13 new: `PaperBroker.sell()` + `lag_bot` risk-control tests).

**Status: UNVERIFIED**, same as everything else -- these are sensible, standard risk controls (Kelly sizing and stop-losses are textbook techniques), but their actual effect on this specific bot's results hasn't been measured yet. Next check should compare win rate / PnL distribution before vs. after to see whether the stop-loss is cutting losses effectively or just realizing losses that would have recovered by resolution -- that's an empirical question this run will start to answer.

---

## SESSION 12 -- Processes died silently overnight (likely system sleep, not a code bug); a real stop-loss re-entry bug found and fixed (2026-06-28)

### Both bot processes were dead, not stuck
**Checked back ~23.5 hours after launch; both `market_maker_bot` and `lag_bot` processes were gone entirely** (confirmed via `Get-CimInstance Win32_Process` returning nothing). Both logs end cleanly at the same timestamp (2026-06-27 18:00) with zero Python tracebacks -- no unhandled exception, the processes were terminated externally. `Win32_OperatingSystem.LastBootUpTime` was unchanged since 2026-06-24, ruling out a reboot. Most likely cause: the machine went to sleep (lid closed / idle timeout) for an extended period, and Windows terminated the background console processes during or after that sleep -- this is a known behavior for detached console apps, not something the bot's own retry/exception handling can prevent. **Process lesson: `Get-CimInstance Win32_Process` should be the first check on any "seems stuck" report, before assuming the strategy/code is at fault** -- a dead process and a live-but-silent one look identical from the report's output alone, but require completely different responses (restart vs. investigate).

### Real bug found while evaluating the data: stop-loss exits don't block re-entry into the same market
- **Problem:** `lag_bot` got stopped out of market `2692183`, and then **re-entered the exact same market on the very next 60-second scan**, then got stopped out of it again a minute later at an even worse price (exit price fell from 0.17 to 0.03). The only thing blocking re-entry into a market is `has_open_position_for_market()` -- which checks for an *open* position, and the stop-loss had just closed it, making the market immediately eligible again.
  - **Root Cause:** the stop-loss and the re-entry guard were designed independently and never cross-checked against each other -- closing a position via stop-loss was treated as "this slot is free again" rather than "we just learned this specific market is moving against our model."
  - **Fix:** `check_stop_losses()` now adds the market_id to a persisted blacklist (`logs/lag_stopout_blacklist.json`) on every stop-loss exit; `run_once()` checks this blacklist before considering a market as a candidate at all. The blacklist is permanent (not time-windowed) since these markets resolve and stop appearing in `fetch_btc_markets()` within minutes anyway -- there's no real cost to never reconsidering one once it's gone.
  - **Verified via real outcome data, not just the bug itself:** checked all 5 unique markets `lag_bot` was stopped out of against their actual final resolution. 4 of 5 stop-losses correctly anticipated a real loss (the position would have paid $0 at resolution). **1 of 5 (`2692264`) was premature -- the market actually resolved in the held position's favor, and the stop-loss sold out of what would have been a full winning payout.** This is the real, unresolved cost of the stop-loss mechanism flagged as an open question in Session 11 -- it's not free, it trades off some genuine recoveries to cut genuine losses, and a 50% threshold checked only once every 60 seconds is necessarily a coarse instrument on a 5-minute market.
  - **Tokens wasted:** medium -- required directly looking up each stopped-out market's real resolution via Gamma to determine whether the stop-loss helped or hurt, since the journal alone can't answer that counterfactual.

### Mandatory process reminder for next session
**The bots cannot be assumed to survive indefinitely on this machine as currently launched -- always verify with `Get-CimInstance Win32_Process` before trusting that a long gap between checks means the bots kept running.**

**Correction on root cause:** initially guessed system sleep, but the user confirmed the machine was not asleep. Checked Windows System event log instead -- found the Claude desktop app auto-updated (`Service Name: Claude`, new version installed) at 2026-06-27 18:00:40, within seconds of both bot processes' last log lines (18:00:11 and 18:00:29-30). **The actual cause was almost certainly the Claude app's own auto-update restarting its host session, which killed the detached bot processes as a side effect** -- not sleep, not a crash (no traceback in either log), and not anything in the bots' own code. **Lesson: don't default to the first plausible-sounding explanation (sleep) without checking -- `Get-WinEvent` on the System log around the death timestamp found the real cause in one query.**

Separately (a different, later, unrelated event): the user manually rebooted the PC on 2026-06-28, which of course also stopped both processes -- that restart is not part of the investigation above and shouldn't be read as a second instance of the same root cause.

**Practical implication:** these bots will not survive a Claude app auto-update OR a PC reboot as currently launched (plain background process via `Start-Process`, no service/scheduled-task registration, no autostart). Anyone checking on a long-running session should expect to need to manually verify-and-relaunch after either event. A more durable setup (Windows Task Scheduler with autostart-on-boot, fully decoupled from any Claude Code session) would survive both, but hasn't been set up -- current runs are "until something kills the process" by design, not indefinite.

**Update:** autostart was added via the Windows Startup folder (no admin rights needed -- a real Scheduled Task was tried first and denied, this shell runs with a restricted token even on an admin account). `scripts/autostart_market_maker.ps1` / `scripts/autostart_lag_bot.ps1` run at every logon. Still doesn't survive a Claude app auto-update (no logon event fires for that).

---

## SESSION 14 -- market_maker_bot full rewrite: real-time WebSocket + Avellaneda-Stoikov pricing (2026-06-30)

After a 2-day run produced a large, conclusive sample (1,002 real fills, 41.5% win rate, -$306.27) confirming the order-flow-imbalance gate from Session 8 never fixed the underlying problem, did a full rewrite instead of further tuning. Diagnosis (see chat for full reasoning, grounded in the Avellaneda-Stoikov and Glosten-Milgrom market-microstructure literature): the old bot's 60-second REST-polling cycle meant every quote was up to a minute stale, which no amount of pricing sophistication can compensate for -- inventory skew and volatility-scaled spreads both assume the maker can react in near-real-time, an assumption the old architecture violated by design. The old bot also tracked 40-90 markets simultaneously, which is *why* a cycle took 60+ seconds -- narrowing scope was as important as speeding up the reaction itself.

### What was built
- `core/live_orderbook.py` -- persistent WebSocket client for Polymarket's public CLOB market channel (`wss://ws-subscriptions-clob.polymarket.com/ws/market`, no auth required, confirmed via direct testing to push many updates per second on an active market). Maintains live best-bid/best-ask per subscribed token in a background thread.
- `core/marketmaking.py` -- Avellaneda-Stoikov reservation price (skews away from mid based on inventory) and time/volatility-scaled spread, replacing the old fixed 0.10 spread quoted symmetrically around mid regardless of conditions.
- `bots/market_maker_bot.py` -- full rewrite: tracks only `max_tracked_markets` (2) soonest-resolving markets, ticks every 1 second reading the live WebSocket state (no REST calls in the hot loop), recomputes quotes fresh every tick instead of requoting on a threshold.
- 24 new tests (`test_live_orderbook.py`, `test_marketmaking.py`, rewritten `test_market_maker_bot.py`), 94/94 total passing.

### Real bug found via live smoke test, not unit tests
**Problem:** first live smoke test bought the same near-$0.01 "longshot" side 10 times in a row, stopping only because `max_inventory_per_event` happened to equal `max_inventory` in config -- coincidence, not a real guard.
- **Root cause #1:** used `core/probability.py`'s log-return volatility estimator (correct for an unbounded price like BTC) on a *bounded* [0,1] contract price. Near a $0/$1 boundary, a tiny absolute move (e.g. $0.01 -> $0.015) is a huge percentage move (50%), so log-return sigma exploded to 0.3-0.58 on a contract sitting at $0.01. That blew the AS spread/reservation formula into a degenerate state (`bid >= ask` after clamping) whose fallback ignored inventory entirely -- defeating the one mechanism that was supposed to prevent exactly this.
  - **Fix:** added `core/marketmaking.estimate_contract_volatility()` -- standard deviation of *absolute* price changes, not log returns. Confirmed via test that this gives the same volatility estimate for the same-sized absolute jitter whether the price is near a boundary or mid-range, unlike the log-return version.
- **Root cause #2:** `check_fill()` had no independent `max_inventory` enforcement -- the per-market cap only ever worked indirectly through `compute_quotes()`'s inventory clamping, which is exactly the mechanism that broke down in root cause #1. Added an explicit, pricing-model-independent `max_inventory` check directly in the fill logic, so a future pricing bug can't cause unbounded inventory growth again.
- **Lesson:** a hard risk cap should never depend on the pricing/strategy math behaving correctly -- it needs to be enforced independently, as a backstop for exactly the case where the smarter logic fails in a way nobody anticipated.

### Status: UNVERIFIED, more so than usual
This is a full architecture rewrite, not a parameter tune -- it needs real trading data before trusting any result, more so than previous sessions' incremental changes. Re-ran the smoke test after both fixes: clean, sane bid/ask pairs even near price boundaries (e.g. 0.96/0.99, 0.01/0.04), no runaway fills. Left running via `run_loop()` (continuous, not `--once`) for real data collection.

---

## SESSION 15 -- lag_bot: real-time market_p via WebSocket + edge persistence (2026-06-30)

`lag_bot` is the one bot with a real, confirmed-positive result (63.3% true win rate, +$21.32 over 59 trades as of Session 13). Before touching it, backed it up properly: copied (not moved -- it was still running) `logs/lag_paper_state.json`, the stop-loss blacklist, and its 123 trade records into `logs/backup_lag_bot_profitable_2026-06-30/`, and tagged the commit as `lag-bot-profitable-2026-06-30` (pushed). This is a real rollback point, not just a note in this file.

**Important framing the user pushed back on correctly before this was built:** the point of the WebSocket here is NOT to out-race other Polymarket traders on Polymarket's own price -- once we read `market_p` via WebSocket, we see exactly the same number every other Polymarket trader sees. The actual problem being fixed is a measurement one: `market_p` was being read via REST once every 60s, so by the time it was compared to a freshly-computed `model_p`, the comparison could be apples-to-oranges (today's model vs. a minute-old market reading), manufacturing a fake disagreement. Going faster could reveal the edge is real and structural (Polymarket's own pricing genuinely lags spot, consistent with the already-confirmed Chainlink resolution-feed lag) OR reveal that some/most of the previously-measured edge was an artifact of our own staleness. **This was explicitly flagged as not yet known before building it** -- the backup above is what lets that question actually get answered instead of just asserted.

### What was built
- `core/live_orderbook.py` extended with `get_book()` (returns a `PaperBroker`-compatible object from the last "book" snapshot, so `buy()`/`sell()` can walk real price levels exactly as they did with REST) and `depth_usd()` (full-book notional, for the liquidity filter) -- both only update on `book` events, not `price_change` deltas, since deltas don't carry full depth. This means depth can be staler than best-bid/ask; acceptable for a liquidity sanity check, not for fill pricing (which still uses the freshest snapshot available).
- `bots/lag_bot.py`: `market_p` now comes from the live WebSocket instead of REST. Stop-loss checks also moved to the live book for the same staleness reason.
- **New, not just a port:** edge persistence. The old version acted on a single scan's disagreement; this version requires the edge to stay in the same direction for `min_consecutive_ticks` (3, ~9 seconds at a 3s tick) before trading, via `edge_confirmed()`. This is a real filter the old 60s-snapshot architecture structurally couldn't support -- there's no concept of "persistence" when you only get one reading a minute. `confirm_and_build_candidate()` then re-checks live price/liquidity at the moment of confirmation (not the historical tick values) before actually trading, so a confirmed-but-now-vanished edge doesn't get chased.
- 22 new/updated tests (`edge_confirmed`, `confirm_and_build_candidate`, stop-loss against a fake live book), 111/111 total passing.

### Status: UNVERIFIED -- this is the critical one to watch
Verified the full pipeline against live data (`compute_signal` returned sane, well-formed output: `model_p=0.517`, `market_p=0.535`, small edge correctly below threshold, no spurious trade). But the open question from the framing above is exactly what the next real trading sample needs to answer: **does the measured win rate/edge hold up, shrink, or vanish now that market_p is current instead of stale?** Compare directly against the `lag-bot-profitable-2026-06-30` tag's numbers (63.3% true win rate, +$21.32/59 trades) once a comparable sample size accumulates -- don't just look at whether it's still PnL-positive, look at whether the *edge magnitude* (`avg edge taken` in `scripts/report.py`) moved in either direction.

---

## SESSION 16 -- Live trading deployment: Polymarket CLOB V2 migration (2026-07-07)

### Goal
Switch lag_bot from paper trading to real money on Polymarket's CLOB V2.

### What was built
- `core/live_broker.py` — LiveBroker class mirroring PaperBroker interface; FOK market orders for entries, `cancel_order(OrderPayload)` for the V2 API, positions persisted to `logs/live_state.json`.
- `core/clob_client.py` — migrated to `py_clob_client_v2`; `get_authenticated_client()` now uses `signature_type=3` (POLY_1271) and `funder=POLY_DEPOSIT_WALLET`.
- `scripts/test_live_order.py` — smoke test: places a non-marketable GTC limit order and cancels it. Run before every live launch to verify credentials and deposit wallet are still valid.
- `scripts/kill.ps1` / `unkill.ps1` — kill switch that blocks new entries without killing the process.
- `scripts/report.py` — updated to detect live vs paper mode from config.json; in live mode queries the CLOB API for balance (not a state file) and uses `live_starting_balance` ($53.77) as the baseline.
- `config.json` — `mode` switched to `live`, `live_starting_balance: 53.77` added.

### The V2 migration was not straightforward — full error chain documented here

**Root error:** py-clob-client V1 rejected all orders with "invalid order version, please use the latest clob-client." Polymarket made V2 mandatory on April 28, 2026.

**After installing py-clob-client-v2 (version 1.0.2):**

| sig_type tried | Error | Meaning |
|---|---|---|
| 1 (POLY_PROXY) | `maker address not allowed, please use the deposit wallet flow` | V2 no longer accepts the old V1 proxy wallet as maker |
| 3 (POLY_1271) | `the order signer address has to be the address of the API KEY` | order.signer = wrong proxy address |
| 0 (EOA, no proxy) | `maker address not allowed, please use the deposit wallet flow` | V2 rejects plain EOA as maker too |

**The unlock:** all three sig_types were tried with the V1 proxy wallet address (`0x3BD8fe...`). The actual V2 deposit wallet is a **different address** (`0xa23d2F995FD9C03AAC2eBefa79795Df2365CA32D`). Found by placing a trade in the Polymarket web UI and reading `order.maker` from the POST `/order` payload in Chrome DevTools Network tab. Once the correct deposit wallet was used as `funder` with `signature_type=3`, the first order was accepted immediately.

### Key lessons

1. **The V2 deposit wallet address differs from the V1 proxy wallet.** They are not the same contract. Do not assume the address shown anywhere in the Polymarket UI as your "wallet" is the correct maker address for API orders — verify by intercepting an actual web UI order.

2. **How to find your deposit wallet:** Polymarket web UI → place any trade → Chrome DevTools → Network → find POST to `clob.polymarket.com/order` → Request payload → read `order.maker`. That address is your deposit wallet.

3. **API keys bind to the EOA, not the deposit wallet — and that is correct.** The CLOB accepts POLY_1271 orders where `order.signer == deposit_wallet` and the API key is registered to the EOA, as long as the deposit wallet is registered in V2. Do not attempt to re-derive API keys against the deposit wallet address — the L1 auth endpoint only supports ECDSA and will reject it.

4. **py-clob-client V1 is permanently dead.** No workaround exists; V2 migration is required for all order placement.

5. **Gemini hallucinated two things during debugging:** (a) a Rust SDK called `rs-clob-client-v2` at `github.com/Polymarket/rs-clob-client-v2` — this repo does not exist; (b) a PyPI package called `kuest-py-clob-client` — also does not exist. Both were presented confidently with code samples. Always verify a repo/package exists before spending time on it.

6. **The real unlock came from DevTools, not any SDK or LLM.** All theoretical approaches (Rust SDK, EIP-1271 header patching, update_balance_allowance, counterfactual contract deployment) failed. The single correct action was intercepting a working web UI order and reading the payload directly.

### Status
- First live order placed and cancelled successfully.
- One test order resolved in our favor (+$0.03) before cancel could fire (cancel method bug during debugging).
- Two test orders lost $0.05 each (expired unfilled while cancel was broken during debugging). Total debugging cost: -$0.10, acceptable.
- config.json is now `mode: live`. Bot is ready to run with `python -m bots.lag_bot`.
- `scripts/report.py` now queries the live CLOB balance and reports against `live_starting_balance: $53.77`.

---

## SESSION 17 -- Multi-coin expansion + duplicate process investigation (2026-07-08)

### Goal
Expand lag_bot from BTC-only to multi-coin (ETH, SOL, BNB, XRP) and investigate why two lag_bot processes always appeared after every restart.

### What was built

**Multi-coin support (core change):**
- `core/markets.py` — Added `COIN_CONFIG` dict mapping coin keys → Gamma keywords + Binance symbol. `BtcMarket` dataclass gains a `coin` field and `binance_symbol` property. Added `_detect_coin()`, `_matches_coins()`, and `fetch_crypto_markets(coins=[...])` as the new entry point. `fetch_btc_markets()` now accepts `coins` param and filters accordingly.
- `core/binance_client.py` — Added `symbol=` param to both `get_price_at()` and `get_recent_prices()`. Default remains `BTCUSDT` for backward compat.
- `bots/lag_bot.py` — `select_tracked_markets()` reads `coins` from config and calls `fetch_crypto_markets(coins=...)`. `tick()` now receives `recent_prices_by_symbol: dict[str, list[float]]` instead of a flat list. `run_loop()` builds a per-symbol price dict each tick by calling `get_recent_prices(symbol=sym)` for each unique Binance symbol in the tracked market set.
- `config.json` — `"coins": ["btc", "eth"]` in lag_bot section. SOL/BNB/XRP available via `other_coins` key (not read by bot; thin-book coins are auto-skipped by `min_book_depth_usd: 50`).

**Duplicate process mitigations:**
- `bots/lag_bot.py` — `_acquire_lock()` / `_release_lock()` write a PID file at `logs/lag_bot.pid` and use `psutil.pid_exists()` to detect a live duplicate. `main()` exits immediately if a live PID is found.
- `scripts/watchdog.ps1` — `Start-Bot` now uses a write-check-compare pattern (write PID to lock file, sleep 300ms, re-read — if another PID overwrote it, yield to that instance). Also added a `Get-BotProcess` check after the race window.

### Key lessons

1. **ETH 5m up/down markets are liquid.** Verified $77–$200 volume per market and $1600+ bid depth — well above the `min_book_depth_usd: $50` filter. Safe to trade alongside BTC. SOL/BNB/XRP had thin or zero volume but are harmless to include (depth filter skips them automatically).

2. **Per-symbol price fetches, not a shared list.** When trading multiple coins, each must have its own independent Binance price history — a single `recent_prices` list only makes sense for BTC. The refactor builds a `{BTCUSDT: [...], ETHUSDT: [...]}` dict per tick.

3. **Duplicate bot process root cause never identified.** Every lag_bot launch (even direct from terminal, no watchdog) spawns a second `python.exe -m bots.lag_bot` as a child of the first, at the same second, with identical command line. No subprocess/multiprocessing calls exist in our code. Most likely a dependency (websocket-client or py_clob_client_v2 on Windows) does something platform-specific at import time. The PID lock file mitigates the impact but does not prevent the spawn.

4. **`run_loop()` was accidentally called twice in main() after the lock refactor.** The `try/finally` block was added correctly, but the original bare `run_loop()` call below it was left in. Since `run_loop()` is an infinite loop this was unreachable in practice, but it was a real bug. Fixed by removing the redundant call.

5. **Bot reads config once at startup.** `run_loop()` loads config.json at call time and uses those values for the entire session. Changing config.json requires a bot restart to take effect. Hot-reload is not implemented.

6. **Watchdog config is hot-reloadable; bot config is not.** The watchdog's `$Bots` array is defined at script start and doesn't reload either, but the watchdog itself has a 60s poll — effectively immediate compared to the bot's indefinite run duration.

### Status
- Multi-coin expansion committed and ready. Bot scans BTC + ETH; SOL/BNB/XRP enabled when liquidity appears.
- Duplicate process issue deprioritized — PID lock file limits practical impact.
- 3 commits unpushed to origin/main at session end.

---

## SESSION 18 -- Trade loss analysis + entry filter tightening (2026-07-08)

### Goal
Diagnose why the bot was consistently stop-lossing or losing money, and apply fixes.

### Bugs found and fixed

**Bug 1 (Critical): SELL takingAmount/makingAmount swapped in `core/live_broker.py`**

In Polymarket V2 CLOB, for a SELL order the semantics are:
- `takingAmount` = USDC received (you take USDC from the bid)
- `makingAmount` = shares you give up (you make shares available)

The original code had the comment saying the opposite and assigned them in reverse. Every stop-loss exit recorded `proceeds = original_share_count` (a large number) instead of the actual USDC received (cents). Example: a stop-loss that received $0.77 USDC was logged as pnl=+$1.33. Real pnl was −$1.23. All stop-loss exits were recording large fake profits while losing real money.

Fix: swapped the assignment in `sell()`:
```python
raw_proceeds = float(resp.get("takingAmount") or 0)   # USDC received
raw_sold     = float(resp.get("makingAmount") or position.shares)  # shares given
```

**Bug 2 (Medium): Stop-loss used bid, not mid, causing immediate triggers**

`loss_frac = (avg_price - bid) / avg_price`. Fill price is near mid/ask. For low-priced tokens (market_p 0.18–0.46) with wide spreads, bid is already 50%+ below fill price the moment you buy → stop-loss fires 19 seconds after entry.

Fix: changed to `mid = (bid + ask) / 2` for the loss_frac comparison.

### Root cause of directional losses

Computed z-score = `log(current/baseline) / (sigma × √T)` for every entry:

| |z| range | model_p | Outcome |
|---|---|---|---|
| ≥ 0.75 | ≥ 0.78 | ALL WON |
| < 0.55 | ≤ 0.69 | ALL STOP-LOSSED |

The model is accurate when price has moved far relative to remaining uncertainty. When |z| is small (entry 164–246s before resolution, small displacement), model_p is 55–70% which is noise around the 50% anchor. The market prices these correctly; the bot was trading noise.

**Entry timing**: every loss entered at 164–246s to resolution. Every win entered under 156s. Waiting until closer to resolution makes the same displacement more decisive.

**Extreme market_p**: the ETH trade at market_p=0.205 (z=−0.07) had a 32% apparent edge, but the model's 52.7% was essentially a coin flip. The market at 20.5% correctly priced that ETH had been UP for most of the 5-minute window; our model only saw the current-vs-baseline snapshot.

### Config changes applied

```json
"max_seconds_to_resolution": 120,   // was 270
"min_model_p": 0.75,                // new -- blocks model_p < 0.75
"entry_price_range": [0.30, 0.70],  // was [0.05, 0.95]
"coins": ["btc"]                    // reverted from ["btc","eth"]
```

`min_model_p` is wired into `confirm_and_build_candidate()` in `bots/lag_bot.py` — checked immediately after the edge direction is resolved, before any live book reads.

### Key lessons

1. **SELL takingAmount/makingAmount is opposite to BUY.** For BUY: takingAmount=shares, makingAmount=USDC. For SELL: takingAmount=USDC, makingAmount=shares. The asymmetry is because in maker/taker terms, on a sell you take USDC (from bids) and make shares available. Always verify with a real test sell before trusting accounting.

2. **The z-score is the real signal, not model_p or edge alone.** model_p is just N(z). When |z| < 0.5 the model is saying "55–65% likely" which is not actionable in a market full of informed traders. Gate on model_p ≥ 0.75 (z ≈ 0.67) as a minimum.

3. **Entry timing matters as much as signal strength.** With 240 seconds left, even a 66% model conviction can be undone by a BTC reversal. With 60 seconds left, a 70% model is much harder to beat. The z-score naturally captures this (√T in denominator), but an explicit `max_seconds_to_resolution` cap is a clean second line of defense.

4. **Wide entry_price_range is dangerous.** Tokens at 18–30¢ have proportionally huge bid-ask spreads, thin books, and markets where informed traders have already priced in strong directional information the GBM model can't see (like 4 minutes of accumulated price movement within the window). Restrict to [0.30, 0.70].

5. **Compare BUY vs SELL API responses with a controlled test.** The takingAmount/makingAmount inversion was not caught during Session 16 because the first test sell was masked by other bugs. Add a `scripts/test_live_sell.py` that places a tiny GTC sell and logs the raw response fields before trusting the accounting.

### Status
- All four fixes committed and pushed.
- Bot reverted to BTC-only, entry filters tightened.
- Restart required to pick up config changes.

---

## SESSION 19 -- Strategy rethink: final-window-only entries (2026-08-04)

### Goal
Analyze post-filter trade results and reconsider the fundamental strategy thesis.

### Trade analysis (15 post-filter trades, Jul 21 – Aug 2)

9W / 3L / 3 stops. Net recorded PnL: +$1.34. Actual balance change: +$0.40 (gap = fees + simultaneous balance fetch timing). Starting balance ~$48.73.

z-score vs outcome showed no clean separation in this dataset — both wins and losses occurred across the z-score range. High model_p trades (0.99+) lost just as readily as moderate ones.

### The key strategic insight

The original thesis: Polymarket's book price lags behind real BTC moves because traders price off Chainlink (which updates every 10–30s). We exploit that lag.

**Problem identified:** if Chainlink has already updated to reflect the BTC move, there is no lag to exploit. The market participants see the same Chainlink price we do. A market_p well below model_p in that scenario means the market is *correctly* pricing reversion risk — not missing information. Our GBM model has no reversion term, so it consistently overstates certainty on high-displacement, long-horizon entries.

**The resolution:** the genuine lag play only exists in the **final 10–30 seconds** before resolution. At that point:
- Reversion risk is mathematically negligible (sigma × √20 ≈ $8–15 for BTC — tiny relative to a $30+ displacement)
- Whatever Chainlink shows next IS the resolution price — the outcome is essentially locked in
- If market_p is still showing a stale/moderate price, that is a real mispricing, not the market wisely pricing reversion

This also resolves the "bog down the bot" concern: with max_seconds_to_resolution=30, `tracked` is empty almost all the time (markets only enter the window for ~25 seconds every 5 minutes). A 1-second tick is cheaper than the previous 3-second tick with max=120, because no Binance fetch fires when tracked is empty.

### Config changes applied

```json
"min_seconds_to_resolution": 5,    // was 30
"max_seconds_to_resolution": 30,   // was 120
"tick_interval_seconds": 1,        // was 3
"refresh_interval_seconds": 5,     // was 20
```

`min_model_p: 0.75` and `entry_price_range: [0.30, 0.70]` retained — at 20 seconds left any meaningful displacement naturally meets min_model_p anyway.

### Key lessons

1. **The lag thesis only holds in the final window.** Entering at 60–240 seconds gives enough time for BTC to revert. At 10–30 seconds the outcome is mathematically nearly locked in and reversion can't meaningfully change it. This is the only window where our information (Binance spot price) is genuinely ahead of what the resolution oracle will confirm.

2. **Large model_p vs market_p disagreement is a warning, not an opportunity, at longer horizons.** When market_p is 42% and model_p is 99%, the market knows about the BTC move too — they're discounting for reversion. At 20 seconds left, that same disagreement IS an exploitable edge because there's no time left to revert.

3. **Narrow entry windows make 1-second ticks cheaper, not more expensive.** Binance fetches only fire when tracked markets exist in the window. With max=30s, the bot is idle for ~97% of each 5-minute market cycle.

4. **Refresh interval must be shorter than the entry window.** With a 30-second window, a 20-second refresh could miss most of it. Changed to 5 seconds so a market entering the window is discovered within 5 seconds of doing so.

5. **Aug 2 degenerate sigma case.** sigma=9.6e-8 (essentially zero) drove model_p to 1.0 via a z-score of 59. This is a GBM edge case, not a real signal. A minimum sigma filter (e.g. sigma < 1e-5 → skip) should be added to guard against flat-price lookback periods.

### Status
- Config updated and pushed. Bot restart required.
- Aug 2 sigma edge case not yet filtered in code — worth adding next session.

---

## SESSION 20 -- Why no trades after 8 days; min_model_p diagnosis (2026-08-12)

### Goal
Diagnose zero trades over 8+ days since deploying the final-window config (max_seconds=30, min=5, tick=1s, refresh=5s).

### Root cause: min_model_p=0.75 blocking all genuine edge trades

Added DEBUG-level logging to `compute_signal` and `confirm_and_build_candidate` to show exact computed values every tick. The first market to enter the 30-second window (`btc-updown-5m-1786563000`) revealed:

```
t=26.4s: model_p=0.433, market_p=0.165, edge=+0.268 → REJECT (model_p=0.433 < min_model_p=0.750)
t=23.3s: model_p=0.429, market_p=0.125, edge=+0.304 → REJECT
t=18.5s: model_p=0.417, market_p=0.105, edge=+0.312 → REJECT
t=10.7s: model_p=0.891, market_p=0.935, edge=-0.044 → no edge (market caught up)
t=7.7s:  model_p=0.927, market_p=0.975, edge=-0.048 → no edge
```

**UP won this market.** The opportunity was in ticks 1–3 (edge 0.268–0.312), but min_model_p=0.750 rejected them because model was only 43% confident on UP (despite strong market mispricing at 10–16.5%). By the time model_p reached 0.891, the market had repriced to 0.935 — no exploitable edge remained.

### Why min_model_p=0.75 is wrong for edge trading

`min_model_p=0.75` requires the chosen outcome to have > 75% model probability. But positive-EV trading requires:
- market_p < model_p by at least min_edge (0.05)
- model_p > 0 (some plausible probability)

Buying UP at 16.5¢ when model says UP=43% is strongly positive-EV:
  EV = 0.43 × $1 - $0.165 = $0.265 per dollar risked

The min_edge filter already prevents noise trades (e.g., model=0.51 vs market=0.45, edge=0.06 — those pass). min_model_p was an additional, unjustified constraint that filtered out high-edge, moderate-confidence bets.

The Session 19 reasoning "at 20 seconds left any meaningful displacement naturally meets min_model_p" was wrong. With sigma=1.87e-5/s (observed), the denominator sigma×√T = 1.87e-5 × √26 = 9.5e-5. A $1 BTC displacement on a $63,380 baseline = log_displacement = 1.58e-5 → z = 0.166 → model_p = 0.566. You'd need a $4.27 move (z=0.674) for model_p=0.75 with this sigma. BTC was only $1 away at the start of the window.

### Additional finding: market vs Chainlink timing

The market repriced dramatically between t=18.5s and t=15.5s even though Binance barely moved ($63379.33 → $63379.80). The market went from 0.105 to 0.725 for UP — a massive swing triggered by what appeared to be a Chainlink oracle update. This confirms:
- Polymarket liquidity providers watch Chainlink directly, not Binance spot
- The market reprices IMMEDIATELY on Chainlink updates, before our Binance-based model reflects them
- Our model can have large edge vs. market_p when Chainlink shows one price but BTC will move to another — but we can't easily tell when that window exists

This doesn't invalidate the strategy: in ticks 1–3 the market was at 10–16.5% for UP and UP ultimately won. Whether this was because the market was wrong about Chainlink or because Chainlink hadn't updated yet is unclear. The edge was real regardless.

### Fixes applied

1. **min_model_p: 0.75 → 0.35** — allow edge trades where chosen outcome has ≥ 35% model probability. Rejects nonsense (model=0.15, market=0.10, edge=0.05) while allowing strong-edge trades like model=0.43, market=0.165.

2. **entry_price_range: [0.30, 0.70] → [0.15, 0.85]** — the original 0.70 cap blocked trades where market_p is 0.72 but model says 0.87 (genuine lag of 15%). The spread concern motivating [0.30, 0.70] was for long-horizon holds (minutes); in the final 30 seconds the position settles immediately and spread is irrelevant.

3. **min_sigma: 5e-6** (new config key) — rejects degenerate near-zero volatility readings where the GBM model breaks down. Added to `compute_signal` alongside the existing `sigma <= 0` guard.

4. **DEBUG logging added** to both `compute_signal` (logs every signal: secs_left, model_p, market_p, edge, sigma, prices) and `confirm_and_build_candidate` (logs which filter rejected the candidate and the exact values). The lag_bot logger is set to DEBUG level; root logger remains INFO so other modules stay quiet.

### Status
- All changes committed and pushed.
- Bot restarted with new config and code.
- Debug logging left in place to observe trades in the new regime.
