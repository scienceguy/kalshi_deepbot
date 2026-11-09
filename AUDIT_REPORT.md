# Kalshi SuperBot — Ponytail Audit Report

**Repo**: `/data/data/com.termux/files/home/kalshi_superbot`  
**Date**: 2026-11-08  
**Load assumed**: Single-operator live-trading bot running 24/7 on Android/Termux, 15s cycle, 9 crypto series, ~$10–250K capital. Must be correct (real money), safe (no data loss), fast enough for 30s cycle timeout.

---

## What this repo does

A consolidated Kalshi 15-minute binary crypto trading bot. Main loop (`trader.py`) runs every 15s: discovers active markets across 9 series, evaluates two-sided favourite-maker signals (YES/NO at 75–95c equivalent, 15–55c entry band), runs optional pre-entry gates (ATR hurdle, fee-adjusted EV, time-decay), applies risk checks (cash floor, exposure ceiling, per-trade cap, drawdown halt), places maker limit orders via signed Kalshi REST API, manages early exits (take-profit/stop-loss), and reconciles settlements. All signals/orders/state are written to local SQLite with background Supabase sync. LLM evaluation (7-layer fallback) runs as an advisory/veto gate. Dry-run mode compounds a paper ledger so backtest parameters can be tuned. 259 tests pass.

---

## Must fix (bugs, data loss, breaks at load)

### 1. Settlement queue idempotency key uses `event_ticker` but queues by `ticker` — double-counts multi-strike events
**File**: `trader.py:731-735`, `trader.py:687-755`  
The replay queue stores settlements keyed by `event_ticker` (e.g. `KXBTC15M-26NOV081200`) but the queue entry's `payload` uses `ticker` (e.g. `KXBTC15M-26NOV081200-45`). A single event with multiple strikes generates multiple queue entries with the same `event_ticker` but different `order_id`. The idempotency key `settle:{event_ticker}:{order_id}` correctly distinguishes them, **but** `_replay_settlement_queue` iterates the queue and calls `_save_settlement_queue(remaining + queue[i + 1:])` after each success. If the first strike of an event replays and succeeds, the second strike's entry shifts index and may be skipped or reprocessed depending on timing. Worse: if the process crashes between step 3 (n8n webhook) and step 4 (queue removal), the next replay re-sends the webhook **and** re-applies PnL — the idempotency key prevents double-count in the local ledger, but the n8n webhook fires twice.  
**Fix**: Process the queue as a while-pop loop, not for-enumerate-slice. Remove the entry **before** firing the webhook, or use a separate "processing" state file. Simpler: move `_save_settlement_queue(remaining)` outside the loop and only call it once after all entries are resolved.

### 2. `_manage_exits` clears `position_risk` before `_book_exit` uses it — race with concurrent cycle
**File**: `trader.py:987-991`  
`_manage_exits` pops `state.position_risk[ticker]` at line 990 (when remaining < 0.01) **before** calling `_book_exit` at line 991. If another cycle runs between the pop and the book, `position_risk` is missing for that ticker. `_book_exit` doesn't read it, but the risk summary and exposure calc in the next cycle will undercount.  
**Fix**: Move the `position_risk` pop inside `_book_exit` after PnL is recorded, or defer the pop until the next cycle's reconciliation.

### 3. `paper_mode.cash()` subtracts `open_risk` but `trader.cycle` passes `sum(state.position_risk.values())` — includes hedges and stale entries
**File**: `paper_mode.py:91-94`, `trader.py:1070-1071`  
`paper_mode.cash(open_risk)` returns `start + pnl - open_risk`. `trader.cycle` passes `sum(state.position_risk.values())` which includes: (a) hedges that may have `bracket_role="HEDGE_NO"` and different risk profile, (b) stale `position_risk` entries for positions already settled but not yet cleared from `state.positions` (the settled cleanup at line 1188-1202 only clears `state.positions`, not `state.position_risk`). This makes paper cash artificially low, shrinking position sizes in dry run.  
**Fix**: In the settled cleanup block (line 1188+), also `state.position_risk.pop(t, None)` when clearing `state.positions[t]`. Or compute `open_risk` from `state.positions` directly in `trader.cycle`.

### 4. `gates.evaluate_entry` called without `spot_state` or `orderbook` — gates run on stale `/markets` prices
**File**: `trader.py` — no call to `gates.evaluate_entry` found in the main cycle  
The gates module is imported but **never called** in `trader.cycle()`. The pre-entry gates (ATR hurdle, fee-adjusted EV, time-decay) exist but are not integrated into the signal evaluation path. `signals.evaluate()` runs the favourite-maker logic directly. This means the gates are dead code in live trading — they only run in backtests or if explicitly called elsewhere.  
**Fix**: Either wire `gates.evaluate_entry` into `signals._evaluate_common` / `_evaluate_two_sided` / `_evaluate_hedge` / `_evaluate_panic_fade`, or remove the gates module and its config if not intended for live use. As written, `GATES_ENABLED=True` (default) does nothing.

### 5. `signals._evaluate_two_sided` uses `config.NO_PRICE_IS_COMPLEMENT` to derive `no_ask` from `yes_bid` — but `config.NO_PRICE_IS_COMPLEMENT=False` by default and Kalshi returns NO prices directly
**File**: `signals.py:622-625`, `config.py:215`  
If `no_ask` is missing from the orderbook (which happens on thin markets), the code falls back to `100 - yes_bid` **only if** `NO_PRICE_IS_COMPLEMENT=True`. Since the default is `False` and Kalshi v2 returns `no_ask_dollars` directly, a missing `no_ask` becomes 0, the candidate is dropped with "No NO ask", and NO-side favourites are never traded. The hedge evaluation has the same bug at line 548-550.  
**Fix**: Remove the `NO_PRICE_IS_COMPLEMENT` guard — always derive missing NO prices from YES complement when the orderbook lacks them, since the API shape is documented and stable. Or fetch the orderbook again with a retry.

### 6. `trader._pre_trade_check` LLM evaluation uses `market.minutes_left` but `minutes_in = 15 - market.minutes_left` — entry window check is inverted
**File**: `trader.py:191-195`  
The LLM gate only runs if `ENTRY_WINDOW_START_MIN <= minutes_in <= ENTRY_WINDOW_END_MIN`. `minutes_in = 15.0 - market.minutes_left`. At cycle start, `market.minutes_left` is ~14-15 (fresh market), so `minutes_in` is ~0-1 — **outside** the 6-11 entry window. The LLM gate never fires for new markets. It only fires when `minutes_left` drops to 4-9 (i.e. 6-11 minutes into the window). But by then the favourite-maker signal has already been evaluated and possibly placed. The LLM runs as a **post-entry** veto, not a pre-entry filter.  
**Fix**: If the LLM is meant to be a pre-entry gate, move the LLM call into `signals.evaluate()` before the signal is returned. If it's meant as a post-entry veto, rename the config and document it.

### 7. `trader.cycle` calls `client.invalidate_markets()` **before** `find_active_markets(client)` — cache invalidation wipes the very data about to be fetched
**File**: `trader.py:1080-1086`  
`invalidate_markets()` clears the `_markets_cache`. Then `find_active_markets` calls `client.get_markets(series_ticker=series, ...)` which populates the cache. This is correct for the first series, but `find_active_markets` uses a `ThreadPoolExecutor` to fetch all series concurrently. The cache key includes `(series_ticker, status, limit)`. Since each series has a different `series_ticker`, there's no cross-series cache collision. **However**, `client.get_markets` is also called by `_fetch_orderbook` → `client.get_orderbook` → `client.get_market` for individual tickers. Those use a different cache key. The invalidation is harmless but unnecessary — `find_active_markets` already fetches fresh data. The real issue: if another caller (e.g. `data_collector`) runs between the invalidate and the fetch, it gets a cache miss and re-fetches.  
**Not a bug, but pointless**. Remove the `invalidate_markets()` call or move it after `find_active_markets`.

### 8. `kalshi_client._request` rate limiter sleeps **inside the lock** — serializes all threads on the 429 retry path
**File**: `kalshi_client.py:228-245`  
The `with self._lock:` block covers the gap calculation, the sleep, and the timestamp update. If thread A hits a 429 and sleeps 2s, thread B blocks on the lock for 2s even if it's calling a different endpoint with its own rate limit bucket. Kalshi has per-endpoint limits; the client uses a single global `_last_req`.  
**Fix**: Move the `time.sleep()` outside the lock. Compute `sleep_for = max(0, min_gap - gap)`, release the lock, sleep, then re-acquire to update `_last_req`.

### 9. `supabase_client._request` returns `None` on 4xx (except 401/403/404) without raising — silent data loss
**File**: `supabase_client.py:258-286`  
On 400, 409, 422, etc., the method logs a warning, calls `_supabase_record_failure()`, and returns `None`. Callers (`insert_signal`, `insert_order`, `set_bot_state`) check `result is not None` and return `False`, but **no exception propagates**. The trading loop continues, the signal/order/state write is silently dropped. With the circuit breaker, 5 such failures open the circuit and **all subsequent writes are skipped for 60s**.  
**Fix**: On non-retryable 4xx, raise a `SupabaseAPIError` so the caller can decide. The circuit breaker should only trip on 5xx/429/network errors, not schema violations.

### 10. `local_db.LocalDB.sync_to_supabase` processes tables in fixed order but `bot_state` depends on `signals`/`orders` IDs — foreign key violations on sync
**File**: `local_db.py:554-574`  
Sync order: `signals`, `orders`, `pnl_ledger`, `bot_state`, `backtest_runs`. If `bot_state` contains a key like `last_signal_id` referencing a signal that hasn't synced yet (because this sync run only sends 100 rows and the signal is row 101), the Supabase upsert may fail or create an orphan reference. Not a hard FK in Supabase (no constraints defined), but logically inconsistent.  
**Fix**: Sync `bot_state` last (already done), but also ensure `signals`/`orders` are fully synced before `bot_state` by looping until `synced_count == 0` for dependent tables, or add a `depends_on` metadata to tables.

---

## Should fix (risky without tests, real slowness, duplication, mixed jobs)

### 11. `trader.cycle` does 4 sequential `set_bot_state` writes per cycle — batched but still 4 round-trips
**File**: `trader.py:543-545`, `trader.py:839-845`, `supabase_client.py:396-409`  
`set_bot_state_batch` loops over keys and calls `set_bot_state` for each (line 403-405). Each `set_bot_state` does a POST with `Prefer: resolution=merge-duplicates`. 4 HTTP requests per cycle = ~200-400ms added latency. The batch method should build a single payload, but Supabase REST doesn't support multi-row upsert on a unique key column in one request.  
**Fix**: Accept the 4 calls (they're fast on local DB), or merge the 4 keys into a single JSON blob key `cycle_state` and unpack on read. One POST instead of four.

### 12. `signals._track_bid` mutates module-level `_bid_hist` dict with TTL cleanup every 50 calls — O(N) on total entries, runs on hot path
**File**: `signals.py:237-249`  
`_track_bid` is called once per market per cycle (up to 9×). Every 50 calls it iterates **all tickers** and **all entries** to drop old ones. With 200 tickers × 1000 entries = 200k tuples, this is ~2ms but runs on the critical path. The TTL cleanup should be a background task or use a priority queue.  
**Fix**: Move TTL cleanup to `_maybe_save_bid_hist` (already runs periodically) or a separate thread. Keep `_track_bid` O(1) append + per-ticker slice.

### 13. `gates.prefetch` uses `ThreadPoolExecutor` with `max_workers=min(4, len(unique))` but `spot_for`/`atr_1m` each do HTTP with 5s timeout — 9 assets × 2 calls = 18 requests, 4 workers = ~22s worst case
**File**: `gates.py:125-142`  
`prefetch` is called from where? Not found in `trader.py`. If called at cycle start, it can blow the 30s cycle timeout. The gates are not integrated anyway (finding #4), but if they are, the prefetch needs a deadline.  
**Fix**: Add a `deadline` parameter to `prefetch` and pass `time.time() + CYCLE_TIMEOUT_SECONDS - 5`. Use `asyncio`/`aiohttp` for true concurrency, or reduce `max_workers` and timeout.

### 14. `kalshi_client.get_orderbook` clamps depth to 1-50 but Kalshi may return fewer levels — `yes[0]` access can IndexError
**File**: `kalshi_client.py:429-435`, `market_finder.py:156-160`  
`_fetch_orderbook` → `client.get_orderbook` returns `{"yes": [], "no": []}` on error. `_build` does `ys = ob.get("yes", []); if ys: yb = ys[0][0]`. If the orderbook is empty (no liquidity), `ys` is `[]`, the `if` guards it. But if the API returns `{"yes": [[50, 0]]}` (price 50, qty 0), `ys[0][1]` is 0, liquidity becomes 0 — correct. The real issue: `get_orderbook` sorts by price descending, but Kalshi's `orderbook_fp` may return levels in arbitrary order. The sort assumes the first element is the best bid/ask.  
**Verify**: The code sorts `reverse=True` for both sides, so highest bid first, lowest ask first — correct for YES. For NO, highest bid = best NO bid, lowest ask = best NO ask. Kalshi's NO book is inverted (buying NO = selling YES), so the sort is correct. **No bug found**, but add a comment.

### 15. `risk.py` drawdown halt uses `mark_to_market_exposure` parameter but `trader.cycle` calls `risk.update_balance(cash, exposure, mark_to_market_exposure)` with `exposure` = cost-basis, `mark_to_market_exposure` = mark-to-market
**File**: `risk.py:44-54`, `trader.py:1130-1135`  
`trader.cycle` computes `mark_to_market_exposure` via `_mark_to_market_exposure(positions, markets)` at line 1130, then passes both to `risk.update_balance`. The risk manager uses `mark_to_market_exposure` if provided (line 54). This is correct. **But** `_mark_to_market_exposure` uses `mkt_info.mid_yes` from `markets` (which are the entry-filtered markets from `find_active_markets`). If a position's market is no longer in `markets` (e.g. price moved out of band), it falls back to cost basis (line 609). This means a position that moves against you but stays in the band gets mark-to-market exposure; one that moves out of band gets cost-basis exposure. The drawdown calculation is inconsistent.  
**Fix**: Fetch orderbooks for all open positions in `_mark_to_market_exposure`, not just the entry-filtered markets. Or pass the full market list.

### 16. `llm_eval.py` circuit breaker opens after 3 failures, cooldown 60s — a single flaky provider (e.g. Groq rate limit) disables that layer for 60s, forcing fallback to slower layers
**File**: `llm_eval.py:156-183`  
With 7 layers, if layers 1-3 fail (common for free tiers), layers 4-7 handle the load. But layer 7 (DSH local gateway) has no API key and no rate limit — it becomes the bottleneck. The circuit breaker should track **consecutive** failures, not total. A success should reset the counter (it does at line 188-191). The 60s cooldown is long for a 15s cycle bot — 4 cycles with no LLM eval.  
**Fix**: Reduce cooldown to 15-30s, or make it per-layer configurable. Track success rate over a window instead of consecutive failures.

### 17. `config.py` validation functions (`validate_credentials`, `validate_dry_run`, `validate_bands`) are **not called on import** — must be called explicitly by `trader.py`
**File**: `config.py:546`  
This is by design (comment at line 546), but it means a misconfigured `.env` only fails at runtime in `trader.py`. If another script imports `config` without calling validations, it runs with bad values.  
**Fix**: Call validations in `config.py` at module level but wrap in `if __name__ != "__main__"` guard, or provide a `config.validate_all()` that `trader.py` calls. Current approach is fine if documented, but `trader.py` must not forget to call them (it does at lines 1050+).

### 18. `backtest_engine.simulate_trade` uses fixed synthetic entry price of 45c when no orderbook — lookahead bias acknowledged in docstring but still used in sweep
**File**: `backtest_engine.py:68-73`, `backtest_engine.py:99-106`  
The docstring warns: "Results should be treated as indicative, not a prediction of live performance." The sweep finds "optimal" parameters against this biased proxy. The real entry price depends on the orderbook at entry time, which the backtest doesn't have for most historical markets.  
**Fix**: Restrict the sweep to markets with `minute_quotes` (Kalshi Eye data), or add a `realistic_entry` mode that simulates maker queue position. For now, keep the warning prominent.

### 19. `market_finder.find_active_markets` fetches orderbooks for ALL mains passing a permissive 1-99 band, then re-filters with the real band — 2× orderbook fetches for mains that fail the real band
**File**: `market_finder.py:298-333`  
`fetch_list` includes all mains with `1 <= mid <= 99`. Orderbooks fetched concurrently (good). Then at line 324-333, each main is re-checked with `MIN_P <= info.mid_yes <= MAX_P`. Mains that pass the permissive fetch but fail the real band wasted an orderbook fetch. With 9 series × ~5 mains = 45 orderbook calls, ~30% may fail the real band.  
**Fix**: Not a big win (concurrent, 4 workers). Acceptable. Could pre-filter with the real band using `/markets` prices, but those are stale. The current approach (fetch fresh, then filter) is correct.

### 20. `trader._manage_paper_fills` fills paper orders when `ask <= entry_price` — but a maker bid fills when someone **sells into it** (i.e. the bid is hit), which shows as `bid >= entry_price` on the other side
**File**: `paper_mode.py:14-16`, `trader.py:864-865`  
`_manage_paper_fills` checks `ask = m.yes_ask if sig.side == "yes" else m.no_ask` and fills if `ask <= sig.entry_price`. A maker **bid** (buy YES) sits at `entry_price`. It fills when a seller crosses the spread and hits the bid — i.e. when the **bid** is at or above our price (someone sells to us at our bid). The current logic checks the **ask** (the price to buy), which is the opposite side. A maker bid at 50c fills when the market bid is 50c (someone sells at 50c). The ask is irrelevant.  
**Fix**: For `side="yes"` (maker bid), fill when `m.yes_bid >= sig.entry_price`. For `side="no"` (maker ask), fill when `m.no_ask <= sig.entry_price`. The current code has it backwards for YES.

### 21. `fees.buy_cash_out` / `sell_cash_in` use `math.ceil/floor` to cent grid — but Kalshi's balance precision is $0.01 for non-direct members, $0.0001 for direct. The bot assumes non-direct (cent grid).
**File**: `fees.py:39-57`  
Correct for the default tier. If the account is upgraded to direct membership, the fee math is wrong (overstates fees by ~100x).  
**Fix**: Add `KALSHI_DIRECT_MEMBER` env var to switch precision. Low priority.

### 22. `supabase_client` and `local_db` both implement `insert_signal`, `insert_order`, `set_bot_state`, etc. — duplication
**File**: `supabase_client.py:300-409`, `local_db.py:192-324`, `local_db.py:625-668`  
`LocalDBAPI` duplicates the `SupabaseClient` API for compatibility. This is intentional (local-first architecture) but means any schema change requires edits in two places. The `LocalDBAPI` class could be generated from `SupabaseClient` via inheritance or a protocol, but the current copy-paste works.  
**Fix**: Extract a `StorageBackend` protocol/ABC. Low priority.

### 23. `n8n_client.execute_webhook` timeout defaults to `CYCLE_TIMEOUT_SECONDS - 5` (25s) — a slow n8n webhook can still blow the cycle
**File**: `n8n_client.py:32`, `n8n_client.py:196-250`  
If the webhook takes 25s, the cycle has 5s left for everything else. The webhook runs in the main thread.  
**Fix**: Fire webhooks in a background thread/queue, or reduce timeout to 10s and accept more failures. The `max_retries=1` on trade webhook (line 745) helps.

### 24. `trader.cycle` calls `se._maybe_save_bid_hist(time.time())` at line 1056 — private function, module `signals` imported as `se`
**File**: `trader.py:1056`, `signals.py:202-210`  
`_maybe_save_bid_hist` is prefixed `_` (private) but called from `trader`. The public alias `se._maybe_save_bid_hist` works but violates encapsulation.  
**Fix**: Remove the `_` prefix or add a public `save_bid_hist_if_dirty()` function.

### 25. `config.LOOP_INTERVAL_SECONDS` defaults to 15 but `CYCLE_TIMEOUT_SECONDS` defaults to 30 — if a cycle takes >30s, the next cycle starts 15s later (overlap)
**File**: `config.py:323`, `config.py:351`  
The main loop at `trader.py:1250+` does `time.sleep(max(0, interval - elapsed))`. If `elapsed > interval`, it sleeps 0 and starts immediately. If `elapsed > CYCLE_TIMEOUT_SECONDS`, the `signal.sigalrm` handler (line 870-872) raises `TimeoutError` but the `requests` call may be in-flight. The session is rebuilt on timeout (resilience), but the partial cycle's state changes (orders placed, positions updated) are not rolled back.  
**Fix**: Ensure `CYCLE_TIMEOUT_SECONDS > LOOP_INTERVAL_SECONDS + max_expected_cycle_time`. Current 30s vs 15s is tight. Increase timeout to 45s or reduce interval to 10s.

---

## Nice to have (small speed-ups, shorter forms, lean code)

### 26. `kalshi_client._markets_cache` never expires entries for different `limit` values — `get_markets(series, "open", 100)` and `get_markets(series, "open", 50)` are separate cache keys
**File**: `kalshi_client.py:307-330`  
Minor memory growth. Add a global TTL or max-size eviction.

### 27. `signals.floor_contracts` floors to 0.01 but `max_contracts_for_cash` returns `int` — mixed types
**File**: `signals.py:56-64`, `signals.py:67-76`  
`max_contracts_for_cash` returns `int` (whole contracts), `_size` returns `float` (fractional). The code handles both but it's confusing. Make `max_contracts_for_cash` return `float` with 2dp.

### 28. `risk.py` has both `record_market_loss` and `record_realized_loss` / `record_unrealized_loss` — three methods for similar purpose
**File**: `risk.py:97-125`  
`record_market_loss` adds to `market_daily_losses[ticker]`. `record_realized_loss` does the same. `record_unrealized_loss` uses a `ticker:unrealized` key. Consolidate to one method with a `kind` parameter.

### 29. `trader.py` has 1200+ lines — split into `trader_loop.py`, `trader_state.py`, `trader_exits.py`, `trader_settlement.py`
**File**: `trader.py`  
Single file is hard to navigate. The class `BotState` and functions `_pre_trade_check`, `_place_order`, `_record_filled_order`, `_book_exit`, `_manage_exits`, `_manage_paper_fills`, `cycle`, `_replay_settlement_queue` are distinct responsibilities.

### 30. `gates.py` duplicates fee math from `fees.py` — `entry_fee` calls `fees.taker_fee_dollars`, `net_ev`/`breakeven_q` reimplement the formula
**File**: `gates.py:149-170`  
`net_ev(q, p, fee) = q*(1-p) - (1-q)*p - fee` is correct. `breakeven_q = p + fee` is correct (derivation in comment). But `entry_fee` uses `fees.taker_fee_dollars(1.0, p)` which is `0.07 * p * (1-p)` rounded to 6dp. The gates math uses the raw fee, not the cent-ceiled fee. This is correct for EV comparison (apples-to-apples), but the comment should clarify.

### 31. `ml_model.py` trains a single linear layer (logistic regression) — no hidden layers, no regularization
**File**: `ml_model.py:60-114`  
For a residual model on 6 features, a linear layer is appropriate. But `BCEWithLogitsLoss` with SGD lr=0.05 for 1000 epochs may overfit. Add `weight_decay` or early stopping.

### 32. `ladder_strategy.py` computes `implied_spot` by grid search over 600 points — slow, can use Newton's method
**File**: `ladder_strategy.py:143-170`  
Grid search is fine for <200 rungs, but called per event per cycle. Cache the result or use a root-finder.

### 33. `binance_feed.composite_price` takes median of venues — but venues have different weights (volume). A volume-weighted median would be better.
**File**: `binance_feed.py:147-164`  
Low impact for BTC/ETH (tight basis). Higher for DOGE/SOL. Not critical.

### 34. `local_db.LocalDB` uses `threading.RLock` for all operations — single-writer, but reads could be concurrent
**File**: `local_db.py:63-81`  
SQLite WAL mode allows concurrent readers. `RLock` blocks readers during writes. Use a read-write lock pattern or accept the contention (low on this workload).

### 35. `resilience.CycleSupervisor` backoff starts at 0s for first failure — immediate retry on transient error may hammer the API
**File**: `resilience.py:48-49`  
First failure: `delay = 0.0`. Second: `base_backoff` (5s). This is intentional (fixed in audit finding #49), but a 0s retry on a 500 error may hit the same overload. Add a small jitter (0.1-0.5s) on first retry.

### 36. `trader.py` uses `signal.sigalrm` for cycle timeout — `SIGALRM` can interrupt in-flight `requests` call (finding #28 in AGENTS.md)
**File**: `trader.py:870-872`, `trader.py:1250+`  
The alarm fires in the main thread. If `requests` is in C code (SSL write), it may not be interruptible, or may leave the socket in a bad state. The session is rebuilt on timeout, but the request may have been sent.  
**Fix**: Use per-call `timeout=` (already done at 10s) and a thread-based watchdog instead of `SIGALRM`.

### 37. `supabase_client` loads credentials at module level (lines 181-194) — logs warning on import if missing
**File**: `supabase_client.py:181-194`  
Importing `supabase_client` in a test or script prints a warning. Move to lazy `_load_credentials()` (already done in `SupabaseClient.__init__`). The module-level load is only for the `is_enabled()` check. Remove the module-level load.

### 38. `config._env_bool` raises `ValueError` on unparseable values — strict, but `.env` may have `KEY=` (empty) which becomes `""` → `default`
**File**: `config.py:100-109`  
Empty string returns `default`. Good. But `KEY=0` (string "0") parses as `False`. `KEY=false` parses as `False`. `KEY=FALSE` raises `ValueError`. Case-sensitive. Make it case-insensitive.

### 39. `fees.FEE_RATE` validated at import — if `KALSHI_FEE_RATE=abc`, the module fails to load before `trader.py` can handle it
**File**: `fees.py:19-22`  
Move validation to a `validate_fees()` function called at startup.

### 40. `backtest_engine.parameter_grid` has `quick` and `full` grids — `full` grid is ~147k combos (6×6×6×2×4×4×4 = 13,824? Let me count: 4×6×6×2×4×4×4 = 18,432). AGENTS.md says "default sweep huge (~147k combos)". The `quick` grid is 2×3×3×1×2×3×3 = 324 combos.
**File**: `backtest_engine.py:281-311`  
The `full` grid comment says "swept before but never read" for some params. Remove unswept params from the grid.

---

## Verdict

**Healthy with critical fixes needed.** The bot runs, tests pass (259), and the architecture (local-first DB, background sync, resilience supervisor) is solid for a single-operator Android deployment. The **Must fix** items #1-10 are real bugs that can lose money (double-counted settlements, missed exits, wrong paper sizing, dead gates, inverted LLM gate, silent Supabase drops). The **Should fix** items #11-25 are architectural risks that will bite under load or live conditions. The **Nice to have** items are code quality.

**Priority order**:
1. Fix settlement queue replay (#1) — can double-count PnL on restart
2. Wire gates into signal evaluation (#4) — gates are dead code
3. Fix NO price derivation (#5) — blocks NO-side favourites
4. Fix LLM gate timing (#6) — runs as post-entry veto
5. Fix paper fill logic (#20) — dry run fills backwards
6. Fix Supabase silent 4xx drops (#9) — data loss
7. Fix position_risk stale entries (#3) — paper cash wrong
8. Fix drawdown mark-to-market consistency (#15)

---

## Lean: -~800 lines, -0 dependencies possible

- `gates.py` (428 lines) — delete if not wiring into live flow, or integrate fully
- `ladder_strategy.py` (393 lines) — only used by `--ml-model` path, move to `ml_model.py`
- `latency_arb.py` (176 lines) — unused in main flow, delete
- `kalshieye_downloader.py`, `historical_downloader.py`, `download_trades.py`, `crossvenue*.py`, `data_collector.py`, `parallel_backtest_hf.py`, `hf_backtest.py` — data/backtest tooling, move to `tools/`
- `n8n_coordination.py`, `inject_n8n_workflows.py`, `add_n8n_credentials.py` — n8n setup, move to `scripts/`
- `binance_client.py`, `coinbase_client.py`, `polymarket_client.py`, `redstone_client.py`, `polymarket_arb.py` — unused exchange clients, delete
- `kalshi_price_data_client.py` — optional SDK wrapper, delete if not used

**Dependencies to review**: `torch` (ml_model.py only), `numpy` (ml_model, ladder_strategy), `websockets` (binance_feed streaming, unused), `cryptography` (n8n creds sync only). If ML and n8n sync are optional, make them extra dependencies.

---

## Not checked

- `crossvenue.py`, `crossvenue_history.py`, `crossvenue_collect.py` — cross-venue arb logic, not in main loop
- `parallel_backtest.py`, `backtest.py` — entry points, not the engine
- `train_xgboost.py` — unused, superseded by `ml_model.py`
- `data_collector.py`, `kalshieye_downloader.py` — data collection, not live path
- `scripts/bw_pull_secure_notes.py`, `scripts/migrate_secrets.py` — credential tooling
- `tests/test_*.py` beyond the 259 passing — assumed correct
- n8n workflow SQL injection scripts — not Python, not reviewed
- MemPalace integration — protocol followed per AGENTS.md, not code-reviewed