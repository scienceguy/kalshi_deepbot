# Comprehensive Fix List - Post-Audit

## MUST FIX (Critical bugs, security holes, data loss)

### 1. Bid history race condition — single-process global mutable state (`signals.py:141-235`)
- **Problem:** Module-level `_bid_hist` dict with no file locking on read. Multi-process would corrupt JSON.
- **Fix:** Add fcntl lock on read in `_load_bid_hist()` and `_track_bid()`.
- **Status:** PENDING

### 2. Settlement P&L double-count window (`trader.py:1225-1255`)
- **Problem:** `state.session_pnl += pnl_to_add` happens BEFORE `set_bot_state_batch()`; if DB fails after local PnL added but before DB write, local PnL inflated permanently.
- **Fix:** Move `state.session_pnl += pnl_to_add` INSIDE `if applied:` block (after `set_bot_state_batch()` succeeds).
- **Status:** PENDING

### 3. `mid_yes` property returns `None` when both bid/ask are 0 (`market_finder.py:57-61`)
- **Problem:** Returns `None` when both bid/ask are 0/None. Pre-filter uses `_compute_mid` which returns `None` → market silently dropped.
- **Fix:** `_compute_mid` should return `None` when both sides are 0/None. Pre-filter should reject `mid is None` explicitly.
- **Status:** PARTIALLY FIXED (mid_yes returns None, but pre-filter needs explicit check)

---

## SHOULD FIX (Risky code, missing tests, scale issues)

### 4. `sigma_at` O(n²) API calls — precompute `sigma_curve` instead (`crossvenue_history.py:171-182, 176`)
- **Problem:** `sigma_at()` called per-minute in `event_basis()` loop, each fetches 60 klines. 6 events = 1,440 Binance calls.
- **Fix:** Use `sigma_curve()` precomputation in `event_basis()` loop.
- **Status:** PENDING

### 5. `sigma_per_minute` single point of failure — no fallback venue (`binance_feed.py:184-200`)
- **Problem:** `sigma_per_minute` uses only Binance. `composite_sigma()` with OKX/Coinbase fallback exists but unused in `ladder_strategy.py`.
- **Fix:** Use `composite_sigma()` in `ladder_strategy.py` instead of `sigma_per_minute`.
- **Status:** PENDING

### 6. `sigma_at` re-fetches Binance klines per-minute inside `event_basis` loop (`crossvenue_history.py:176`)
- **Same as #4** - same fix.

### 7. Missing tests for risky logic
- **Missing tests:**
  - `trader.py:_ml_filter_signals` (ML integration)
  - `signals.py:_evaluate_panic_fade` (panic fade strategy)
  - `signals.py:_evaluate_hedge` (hedge strategy)
  - `market_finder.py:find_active_markets` (integration)
  - `kalshi_client.py:get_balance` sanity guard
  - `supabase_client.py:set_bot_state` single-row upsert
- **Status:** PENDING

### 8. Config validation auto-called on import (`config.py:453`)
- **Problem:** Already added but verify it works correctly.
- **Status:** VERIFY

### 9. `stream_trades` reconnect logic verify (`binance_feed.py:277-293`)
- **Problem:** Verify reconnect logic works correctly with exponential backoff.
- **Status:** VERIFY

---

## NICE TO HAVE (Dead code, duplication, cleanup)

### 10. `poly` feature mirrors `kalshi` — no cross-venue signal (`ml_model.py:212`, `crossvenue_history.py:210`)
- **Problem:** `"poly": mid` mirrors Kalshi mid. No real Polymarket data.
- **Fix:** Remove `poly` feature from ML features and crossvenue_history.
- **Status:** PARTIALLY FIXED (removed from ML features)

### 11. Dead code cleanup verify
- **Files removed:** `latency_arb.py`, `add_n8n_credentials.py`, `inject_n8n_workflows.py` ✓
- **Verify:** No imports of these remain.
- **Status:** VERIFY

### 12. Duplicate `floor_contracts` calls removed (`signals.py`) ✓
- **Status:** DONE

### 13. `get_klines_range` pagination verify (`binance_feed.py:169-181`)
- **Status:** VERIFY

### 14. `stream_trades` reconnect verify (`binance_feed.py:277-293`)
- **Status:** VERIFY

### 15. Test assertions mismatched after fixes verify
- **Tests to verify:** `test_mid_yes_none_when_no_quotes`, `test_time_gate_blocks_the_settlement_race`, `test_row_to_features_has_six_values`
- **Status:** VERIFY

---

## NEWLY IDENTIFIED FROM FULL CODE READ

### 16. `_bid_hist` memory leak — unbounded growth without cleanup (`signals.py:229-233`)
- **Problem:** Cap of 1000 entries per ticker and 200 tickers, but cleanup only removes oldest 80 entries when >200 tickers. No TTL-based cleanup.
- **Fix:** Add time-based TTL cleanup (e.g., remove entries older than STABILITY_LOOKBACK_MIN + 1 hour).

### 17. `_stability_ok` fail-open on fresh tickers allows trades with no history (`signals.py:249-256`)
- **Problem:** Fresh tickers allowed with only a warning. This is by design per AGENTS.md #22, but should be documented.
- **Status:** BY DESIGN (documented in AGENTS.md #22)

### 18. `settled_events` pagination cursor handling fragile (`crossvenue_history.py:106-109`)
- **Problem:** Loop breaks on empty cursor but doesn't detect stuck cursor (same cursor returned repeatedly).
- **Fix:** Track previous cursor, break if unchanged.

### 19. `event_basis` uses spot from same minute as candle close — lookahead bias (`crossvenue_history.py:170, 217`)
- **Problem:** `ts = int(c["end_period_ts"])` then `spot = spots.get(ts - ts % 60)`. Kalshi candle end = Binance kline close. Spot at same minute = lookahead.
- **Fix:** Use `spots.get(ts - 60)` (previous minute's close) for alignment.

### 20. `sigma_at` window uses `end_ms = minute_ts * 1000` which EXCLUDES the current minute (`crossvenue_history.py:172-173`)
- **Problem:** Binance klines with `startTime`/`endTime` returns klines where `open_time < endTime`. So `end_ms = minute_ts * 1000` excludes the kline at `minute_ts`.
- **Fix:** Use `end_ms = (minute_ts + 60) * 1000` to include the current minute.

### 21. `candles()` wrong API path fixed but verify (`crossvenue_history.py:114`)
- **Fixed:** Changed to `/historical/markets/{ticker}/candlesticks` ✓

### 22. `_f()` reads `close_dollars` but API returns `close` — FIXED ✓

### 23. `expiration_value` parsing outside try/except — FIXED ✓

### 24. `close_time` None guard — FIXED ✓

### 25. `SERIES_ASSET` missing 15m series — FIXED ✓

### 26. `sigma_at` called per-minute O(n²) — same as #4

### 27. `MIN_MINUTES_TO_HOLD = 1.0` for 15M markets — FIXED ✓

### 28. Config validation auto-called — FIXED ✓

### 29. `MID_TARGET_CENTS=50`, `CONFIDENCE_STRENGTH_SCALE=100` — FIXED ✓

### 30. New configs: `PANIC_FADE_NO_BAND_MIN/MAX`, `HEDGE_STABILITY_ENABLED`, `SKEW_ENTRY_MIN/MAX` — FIXED ✓

---

## VERIFICATION NEEDED

- [ ] All 237 tests still pass after each fix
- [ ] No imports of deleted modules (`latency_arb`, `add_n8n_credentials`, `inject_n8n_workflows`)
- [ ] `composite_sigma` used in `ladder_strategy.py`
- [ ] `sigma_curve` used in `crossvenue_history.py:event_basis`
- [ ] `fcntl` lock on `_bid_hist` read
- [ ] Settlement P&L fix: `state.session_pnl += pnl_to_add` inside `if applied:`
- [ ] `mid_yes` None handling in pre-filter
- [ ] New tests added for untested functions
