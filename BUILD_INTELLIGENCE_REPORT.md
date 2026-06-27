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
