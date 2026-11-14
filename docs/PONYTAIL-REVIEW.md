# PONYTAIL-REVIEW — recent fix batch (2026-11-14)

Scope: commits `f0f73c1` (marketSides), `09591d7` (3% limit), `d372dd1` ($35 bankroll), `02dcfb1`+`9527ea0` (24h expiry + 5m filter), `c5c00fb`+`5ff7277` (spot/vol cache), plus direct verification of surrounding code this session.

## What this change does

Hardens the Polymarket US paper engine: correct side quotes, 3%-of-balance position cap, $35 reset, 24h expiry gate with stale-position rotation, and REST-call caching. Goal was freeing paper cash for fast 15m/hourly settlement compounding at 92-99c.

## Findings

### 1. marketSides mapping — `poly_us_trader.py:281-298` (entry), `:541-557` (MTM) — correct, with one reservation
- What this is: Explicit long/short assignment from `marketSides[].long`/`description`, YES-complement fallback; replaces blind `outcomePrices[0]`.
- Fix quality: Right shape — `long is True` or desc in (yes, long) ⇒ YES side; else NO side; missing quote falls back to outcomePrices with same mapping. Handles the single-quote inversion (the actual bug).
- Reservation: multi-outcome (>2 sides) picks the first two quotes arbitrarily — fine for binary crypto, not fine if a 3-way lands in the scan. Over-broad `-5m-` substring and missing end-date markets also bypass downstream gates unchanged (see audit #5).
- If we skip the reservation: silent wrong-leg pricing on any non-binary market that slips past the category filter.

### 2. 3% balance limit per position — `poly_us_trader.py:385-396` (`compute_tier_size`), `:419-422` (enforcement) — correct control, inert caps
- What this is: `min(tier_cap, 3% of total_balance, cash above 30% floor)`; absolute caps $30/$300/$3000/$20k ($100k+ uses 2%).
- Fix quality: Matches the 2-5%/position and ≤15%-correlated-cluster rules; on $35, 3% = $1.05 = one 92-99c contract, and the `max_trade_dollars < 0.90` escape hatch intentionally permits exactly that.
- Reservation: tier caps never bind (3% is always tighter below $100k, then 2% takes over) — dead code, harmless. Total_balance includes open MTM so the floor is procyclical: in a drawdown, size shrinks exactly when Kelly would too — acceptable, keep it.
- If we skip it: nothing — the control itself is the fix; do not "simplify" the 30% cash floor against it, they are the same risk on two axes.

### 3. $35 bankroll init/reset — `poly_us_trader.py:44-85`, `:660-664` — clean, one audit hole
- What this is: `initial_cash=35.0` everywhere (arg default, reset, main), milestones seeded with start balance, ledger events for reset and milestone.
- Fix quality: Right — the bankroll is now one constant; `--reset` restores it deterministically; start-balance is persisted so restarts don't drift.
- Reservation: corrupt state JSON is silently overwritten by a fresh $35 state (warning logged, but the corrupt file is lost). Copy the bad file aside (`*.corrupt-<ts>`) before overwrite.
- If we skip it: the one run where paper P&L matters most is the one whose state you cannot reconstruct.

### 4. 24h expiry filter + stale rotation — `poly_us_trader.py:251-259`, `:492-531` — right intent, two holes
- What this is: Entry filter `hours_left <= 0 or > 24 → skip`; cycle rotates positions with >24h left out at cached `current_price`; 5m contracts string-filtered both at scan and entry.
- Fix quality: Direction is correct and the 5m exclusion matches the data (Pantera: 56% of 5m cycles finish within 0.05% of open — noise, not signal). Settlement-latency doc (`docs/poly_us_settlement_latency.md`, untracked) motivated the free-cash rotation.
- Reservation (a): markets with **no** parseable end date pass the filter forever (`if hours_left is not None` guards both entry and rotation) — default-deny instead. Reservation (b): rotation books `exit_price = pos.get("current_price", pos["price"])` — last cached MTM, not an executable quote; a stale 30d market can book a fabricated win/loss into the ledger. Use the venue bid or skip rotation for quotes older than ~60s.
- If we skip it: the compounding metric is polluted by phantom exits on exactly the positions the rotation was built for.

### 5. Spot/volatility caching — `crossvenue_edge.py:28-60` — fixes the REST thrash, reintroduces the stale-price class
- What this is: 5s spot cache, 60s annualized-vol cache (composite_sigma × √525600, clamped 0.20-1.50), stale-value fallback on fetch failure.
- Fix quality: Removes the 400-market HTTP fan-out that was tripping rate limits; caching windows are right for 15m markets given ~347ms median quote lag to Binance (vol barely moves in 60s).
- Reservation (a): on feed failure both caches return the stale value **with no age bound** — a dead feed serves ghost spot forever; `gates.py` does this correctly with `GATES_STALE_OK_SECONDS=300`, reuse that pattern. Reservation (b): vol fallback is a silent constant `0.60` when the feed has never succeeded — the same unvalidated-constant class as `MODEL_VOLATILITY=0.80`; return None and let the caller skip the market instead.
- If we skip it: an outage mid-cycle trades on prices from before the outage; edge sign flips with the ghost spot.

### 6. Observed while reviewing — one new Must, in the same files
- `polymarket_us_client.py:267-329`: order `price.value` is the YES side for both intents; NO buys must send `1 - price`. Paper preview currently "validates" NO orders at the wrong price (see PONYTAIL-AUDIT #2). Fix before any live-capital intent.
- In-flight `gates.py` diff (log-`S/K` cushion + pin-risk deny) is consistent with BS digital theory (d2 = ln(S/K)/σ√T, driftless at short horizon) — review at commit, not now.

## Verdict

Directionally correct fixes for real failure modes; none are wrong, two are incomplete in the failure path (stale caches, fictional rotation exits) and one adjacent bug (NO price) was missed in the same files.

## Lean

~20 lines total: fsync the save, bound cache staleness to 300s, return None on uncached vol failure, require `end_date` for entry, book rotation at executable quote. No restructuring.

## Not checked

`tests/test_poly_us_trader.py` and `tests/test_crossvenue_edge.py` contents (462-passing claim not re-run), `trader.py` partial-fill top-up from commit `9527ea0`, the settlement-latency doc (untracked), live `orders.preview` call, n8n/Supabase.
