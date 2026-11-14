# Ponytail Audit — Kalshi SuperBot

**Date:** 2026-11-15  
**Auditor:** Nemotron 3 Ultra (via OpenCode)  
**Repository:** `/data/data/com.termux/files/home/kalshi_deepbot`  
**Commit:** HEAD (all 439 tests pass)

---

## What this repo does

A single-operator live-trading bot for Kalshi 15-minute binary crypto markets. The bot places favourite-maker limit orders (YES/NO at 75–99c), evaluates signals through a 3-gate pre-entry filter (ATR hurdle, fee-adjusted EV, time-decay cushion) plus an optional 7-layer LLM fallback, writes signals/orders/bot-state to Supabase, and routes settlement P&L through n8n webhooks. Dry-run mode is the default; live trading requires explicit `--live` + `KALSHI_CONFIRM_LIVE=true`.

**Load assumed:** One bot process, single account, ~10 req/s sustained against Kalshi API, 15-second cycle, 9 crypto series monitored concurrently. The bot must survive transient network failures, API rate limits, and restarts without double-counting P&L or leaking capital. Memory must stay bounded over weeks of continuous operation.

---

## Findings

### Must fix

**1. `trader.py:1259` — `find_active_markets` called without invalidation after order placement**  
- **What this is:** The cycle calls `find_active_markets(client)` at line 1259 to get fresh markets for the current cycle.  
- **Problem:** After placing orders (line 2027), the client's `_markets_cache` still holds stale data from before the order was placed. The next cycle's `find_active_markets` will return cached (stale) market data unless `invalidate_markets()` is called. The code does call `client.invalidate_markets(series_ticker=...)` at line 2082 and 2120, but only for the *filled* market's series. Other series' caches remain stale. A market in a different series that was filtered by the old band could now be in band but won't be seen until the 30s cache TTL expires.  
- **Fix:** After any order placement, call `client.invalidate_markets()` with no args to clear the full cache, or call it for all `TARGET_SERIES`.  
- **If we skip it:** The bot trades on prices up to 30s stale, missing fills or quoting into moved books.

**2. `trader.py:1730-1733` — Dry-run uses cost-basis exposure for mark-to-market, not actual mids**  
- **What this is:** In dry-run mode, `mark_to_market_exposure` is set to `sum(state.position_risk.values())` (line 1731), which is cost-basis (`contracts * entry_price`).  
- **Problem:** The risk ceiling and drawdown halt use `mark_to_market_exposure` (lines 54-80 in risk.py, line 1767 in trader.py). In dry-run, a position that moved against the bot still shows the original entry cost, so drawdown is understated and exposure ceiling doesn't bind. The live path correctly uses orderbook mids via `_mark_to_market_exposure` (line 597).  
- **Fix:** Use the same `_mark_to_market_exposure(positions, markets)` logic for dry-run, substituting `state.positions` for `positions`. The mids are already available on `MarketInfo` objects in `markets`.  
- **If we skip it:** Dry-run risk metrics are fiction; a losing streak won't trigger the halt, and the paper curve overstates available cash.

**3. `trader.py:1291` — Paper cash recomputed from `paper_mode.cash()` using MTM exposure, but `cash` variable reassigned**  
- **What this is:** At line 1291, `cash = paper_mode.cash(sum(state.position_risk.values()), paper_mtm_exposure)` reassigns the `cash` variable.  
- **Problem:** This overwrites the real `cash` (from `_sizing_cash()`) that was used for sizing and risk checks earlier in the cycle. Later code (e.g., line 1766 `cash_available = cash is not None`, line 1767 `state.risk.update_balance(cash, ...)`) uses this paper value even though the rest of the cycle logic assumed real cash for sizing. The risk update uses paper cash + paper MTM exposure, but `max_trade_dollars` at line 1839 was computed with real cash.  
- **Fix:** Use a separate variable `paper_cash = paper_mode.cash(...)` and only pass it to paper-specific paths. Keep `cash` as the real sizing cash.  
- **If we skip it:** Dry-run and live risk state drift; paper P&L compounds on a different cash base than sizing used.

**4. `kalshi_client.py:343-375` — `get_markets` cache keyed by `(series, status, limit)` but no size bound**  
- **What this is:** The `_markets_cache` dict grows with every unique `(series_ticker, status, limit)` combination queried.  
- **Problem:** `find_active_markets` calls `get_markets` with `FETCH_MIN_P`/`FETCH_MAX_P` from `extra_band` (lines 308-310 in market_finder.py), which varies per call. Each unique band creates a new cache entry. Over hours/days, the cache accumulates hundreds of entries. The cleanup at lines 359-366 only removes entries older than 60s, but fresh entries with different keys accumulate.  
- **Fix:** Cap the cache at a fixed size (e.g., 50 entries) using LRU eviction, or normalize the key to always use a standard `limit=100` and filter locally.  
- **If we skip it:** Unbounded memory growth on long runs; eventual OOM on constrained Android.

**5. `signals.py:145-147` — Module-level `_bid_hist` is single-process only**  
- **What this is:** `_bid_hist` is a global dict loaded from `bid_hist_{dry,live}.json` and saved periodically.  
- **Problem:** The comment at lines 159-163 admits this is single-process only. If the bot is ever run in multiple processes (e.g., two dry-run instances on the same host), each has its own in-memory copy, they overwrite each other's saves, and the stability gate sees divergent history. The file uses `fcntl.flock` (lines 175, 209) but the in-memory dict is not reloaded on every access — `_load_bid_hist` only reloads when TTL expires (line 169) or forced. Concurrent writers race on the file.  
- **Fix:** Move bid history to the local SQLite DB (`local_db.py`) with a `bid_history` table, or use a Redis instance. At minimum, reload from file on every `_track_bid` call (expensive) or use a single writer process with IPC.  
- **If we skip it:** Stability gate gives different answers in different processes; a fresh-start bot may trade while another blocks, or vice versa.

**6. `trader.py:1030-1040` — `_already_ordered` only checks `resting_tickers`, not live resting orders from exchange**  
- **What this is:** `_already_ordered` returns `True` if `ticker in state.resting_tickers`.  
- **Problem:** `resting_tickers` is updated when the bot places an order (line 2040) and reconciled from `client.get_orders()` (lines 1309-1361). But there's a race: if a resting order from a *previous* cycle fills *between* cycles, `resting_tickers` still has the ticker until the next reconciliation (which happens at the *start* of the next cycle). The check at line 1994 runs *before* the reconciliation in the same cycle, so a filled order's ticker blocks a new entry until the next cycle's reconciliation clears it.  
- **Fix:** Move the reconciliation (lines 1309-1361) to run *before* signal evaluation, or make `_already_ordered` also check `client.get_orders()` for the ticker (cached).  
- **If we skip it:** One cycle of missed entries per fill; not catastrophic but reduces trade frequency.

**7. `gates.py:483-489` — Gate decision logged per-cycle but `_write_log` dedupes on `(ticker, side)` only**  
- **What this is:** `_write_log` skips writing if the decision string is unchanged for the same `(ticker, side)`.  
- **Problem:** The key is `(ticker, side)` but a market can have multiple signals (e.g., YES and HEDGE_NO on same ticker) with different decisions. The second signal's decision overwrites the first in `_last_decision`, so a change on the first signal may not be logged if the second signal's decision happens to match the cached value.  
- **Fix:** Include the signal source/reason in the key, or log every decision (the log is already small — ~1 line per candidate per cycle).  
- **If we skip it:** Gate calibration data has gaps; hard to debug why a signal was allowed/blocked.

---

### Should fix

**8. `trader.py:1957` — `gates.prefetch` called with a set comprehension that may be empty**  
- **What this is:** `gates.prefetch({gates.asset_for(sig.market) for sig in signals})`  
- **Problem:** If `signals` is empty, the set is empty, and `prefetch` returns early. But if `signals` has entries whose `asset_for` returns `None` (unsupported series), `None` is added to the set. `prefetch` iterates `unique = [a for a in dict.fromkeys(assets) if a]`, so `None` is filtered out. However, `gates.asset_for` returns `None` for unsupported series, and those signals would have been blocked by Gate 0 anyway. The real issue: `prefetch` is called *after* LLM filtering but *before* gates evaluation, which is correct, but the set comprehension creates a new set every cycle — fine.  
- **Actually fine on re-read.** The `if a` filters `None`. No fix needed.  
- **But:** `gates.prefetch` at line 1957 runs *after* LLM filtering (line 1951) but *before* gates evaluation (line 1958). The LLM filter runs on all signals in the entry window (line 1888). If LLM blocks a signal, it's removed (line 1941 `continue`), so it never reaches `prefetch`. That's correct — we don't warm caches for signals we already vetoed.

**9. `trader.py:1884-1951` — LLM evaluation runs per-signal sequentially, not concurrently**  
- **What this is:** The LLM loop at lines 1886-1950 processes each signal one at a time, awaiting `should_block_trade` which tries 8 layers sequentially with retries.  
- **Problem:** With up to `MAX_ORDERS_PER_CYCLE=2` signals, and each LLM call taking up to 25s deadline, two slow LLM calls could consume 50s — but the cycle timeout is 30s. The `deadline` in `evaluate_trade` (line 530) stops the layer loop, but the cycle still spends its full budget on LLM calls.  
- **Fix:** Run LLM evaluations concurrently with `ThreadPoolExecutor` and a shared deadline, or skip LLM for signals that will be blocked by gates anyway (gates run after LLM, line 1958).  
- **If we skip it:** Cycle timeout risk; gates are more deterministic and should run first as a cheaper filter.

**10. `signals.py:652-672` — `evaluate` returns `_evaluate_taker_favourite` only, two-sided favourite maker is dead code**  
- **What this is:** The `evaluate` function at line 669 calls `_evaluate_taker_favourite` for MAIN markets. The two-sided favourite maker (`_evaluate_two_sided`) is not called anywhere.  
- **Problem:** Dead code inflates the module. The `HEDGE_NO` path (line 667) calls `_evaluate_hedge`. The old two-sided logic is preserved but unreachable.  
- **Fix:** Delete `_evaluate_two_sided` and its helpers, or add a config flag to switch strategies.  
- **If we skip it:** Code rot — future changes to the live path won't update the dead path, and it becomes a trap for anyone reading it.

**11. `market_finder.py:365-397` — `extra` markets collected but never used by caller**  
- **What this is:** `find_active_markets` builds `extra = []` (line 365) and appends markets whose mid is in `extra_band` (lines 378-380). The `_MarketResult` return object carries `extra` (line 295).  
- **Problem:** The caller (`trader.cycle` at line 1259) never reads `markets.extra`. The `extra_band` parameter is passed from `trader.cycle` but defaults to `None` (line 298). It was intended for a wider fetch to get orderbooks on mid-range markets, but the logic was never wired to the order loop.  
- **Fix:** Either use `extra` for something (e.g., log them, or run a secondary strategy), or remove the parameter and the `extra` collection.  
- **If we skip it:** Wasted orderbook fetches for markets that are never evaluated.

**12. `llm_eval.py:169-274` — `LAYERS` list has hardcoded free model names that may become unavailable**  
- **What this is:** Each layer has a `free_models` list (e.g., line 189-193, 206-208, 218-222).  
- **Problem:** Free model offerings change. `openrouter/free` is an auto-router, not a model. `meta/llama-3.2-90b-vision-instruct` on NVIDIA may be deprecated. The fallback logic tries primary model then `free_models`, but if all are gone, the layer fails. No automated check validates model availability.  
- **Fix:** Move free model lists to config/env, or query the provider's `/models` endpoint at startup to validate.  
- **If we skip it:** LLM layer silently fails, falls back to next layer, but the operator doesn't know the configured model is dead.

**13. `local_db.py:538-609` — `_sync_table` does per-row round-trips to Supabase**  
- **What this is:** The sync loop iterates unsynced rows and calls Supabase client methods one by one (lines 552-607).  
- **Problem:** For `signals` and `orders` tables with many rows, this is N HTTP requests. The Supabase client has `insert_signals_batch` (line 346) but the sync doesn't use it. `bot_state` is synced first (correct, line 626), but other tables could batch.  
- **Fix:** Collect rows per table, call batch insert where available (`insert_signals_batch`, `insert_backtest_run` could batch), then mark synced in one transaction.  
- **If we skip it:** Slow sync on large backlogs; Supabase rate limits may trigger circuit breaker.

**14. `binance_feed.py:100-102` — `_remaining` uses `time.monotonic()` but deadline from `time.monotonic() + timeout`**  
- **What this is:** `deadline = time.monotonic() + timeout` (line 108), `_remaining(timeout, deadline)` returns `max(1, int(deadline - time.monotonic()))`.  
- **Problem:** Correct, but `timeout` param is unused in `_remaining` (shadowed). The function signature is `_remaining(timeout: int, deadline: float)` but the first arg is never read.  
- **Fix:** Remove unused `timeout` parameter.  
- **If we skip it:** Confusing code; no runtime bug.

**15. `model.py:67-70` — `gbm_probability_yes_wins` uses `risk_free_rate=0.05` hardcoded**  
- **What this is:** The GBM model uses a fixed 5% risk-free rate.  
- **Problem:** The risk-free rate changes (Fed policy). For 15-minute expiries, the effect is tiny (~0.05 * 15/525600 ≈ 1.4e-6), but it's a magic number.  
- **Fix:** Make it configurable via `config.MODEL_RISK_FREE_RATE` with default 0.05.  
- **If we skip it:** Model drift over years; negligible for 15M.

**16. `risk.py:17-24` — `_paper_guards_relaxed` bypasses drawdown halt, cooldown, and per-market loss for dry-run**  
- **What this is:** When `PAPER_GUARDS_ENABLED=False`, dry-run ignores the halt, consecutive-loss cooldown, and per-market daily loss limit.  
- **Problem:** The comment says "operator directive 2026-11-13: drop the guards for now so we can better understand the behavior". This is a temporary hack that made it into the codebase. If someone runs dry-run with `PAPER_GUARDS_ENABLED=False` (default is `True`), the risk model is not tested.  
- **Fix:** Remove the bypass or make it a test-only flag. The default should be `True` so dry-run exercises the same risk logic as live.  
- **If we skip it:** Dry-run gives false confidence; a live deploy with the same params hits guards that were never triggered in testing.

**17. `trader.py:245-247` — `paper_mode.cash` called with `sum(state.position_risk.values())` and `paper_mtm_exposure`**  
- **What this is:** At line 1291, `cash = paper_mode.cash(sum(state.position_risk.values()), paper_mtm_exposure)`.  
- **Problem:** `paper_mode.cash` at line 178 takes `(open_risk, mark_to_market_exposure)`. `state.position_risk` is cost-basis (`contracts * entry_price / 100`). `paper_mtm_exposure` is mark-to-market from mids. The function uses `mark_to_market_exposure` if provided, else `open_risk`. So the first argument is ignored when the second is given. The call passes both but the second wins.  
- **Fix:** Call `paper_mode.cash(0, paper_mtm_exposure)` or just `paper_mode.cash(mark_to_market_exposure=paper_mtm_exposure)` (the function signature doesn't support keyword-only, but the logic is `effective_risk = mtm if mtm is not None else open_risk`). Passing `sum(...)` as first arg is misleading.  
- **If we skip it:** No bug, but confusing — the first argument is dead code when MTM is provided.

**18. `kalshi_client.py:534-532` — `api_price_to_internal_no` has `is_complement` default True but NO prices are NOT complemented**  
- **What this is:** The method converts API NO price to internal cents. Default `is_complement=True`.  
- **Problem:** `NO_PRICE_IS_COMPLEMENT=False` in config (line 303), meaning Kalshi returns NO prices directly. But the default here is `True`, so callers that don't pass the flag get the complement. The only caller is... grep shows no callers of `api_price_to_internal_no` in the codebase. It's dead code.  
- **Fix:** Delete the method, or fix the default to match config.  
- **If we skip it:** Dead code; if ever used without the flag, returns wrong price.

**19. `resilience.py:39-59` — `mark_failure` increments `errors_in_a_row` on ANY exception, including `SIGALRM`**  
- **What this is:** The cycle's exception handler (line 2419) calls `state.resilience.mark_failure(e)` on any unhandled exception.  
- **Problem:** The comment at line 2375 says "No SIGALRM handler — per-request timeouts in kalshi_client.py protect individual HTTP calls from hanging." But if a `SIGALRM` *were* used (it's not), or if a `KeyboardInterrupt` bubbles up, it would count as a failure and trigger backoff/rebuild. The `is_auth_error` check (line 61-72) looks for "auth"/"permission"/"401"/"403" in the error string, which is fragile.  
- **Fix:** Catch specific exception types in the cycle handler; only mark failure for network/API errors, not for `KeyboardInterrupt`, `SystemExit`, or programmer errors.  
- **If we skip it:** A bug that raises `ValueError` triggers client rebuild instead of surfacing the bug.

**20. `config.py:448` — `DRY_RUN = _env_bool("DRY_RUN", True)` but `.env` can override to `false`**  
- **What this is:** The default is safe (True), but the local `.env` file overrides it. The warning at lines 444-446 says "Always verify DRY_RUN before running from a directory whose .env you did not write."  
- **Problem:** This is a known trap (AGENTS.md issue #18, FIXED). The fix was requiring `--live` + `KALSHI_CONFIRM_LIVE=true`. But the `.env` can still set `DRY_RUN=false`, and if the user runs `python trader.py` (without `--live`), the check at line 2529 catches it and exits. However, if they run `python trader.py --live`, it passes. The double-guard works.  
- **Actually fine** — the double guard is correct.

---

### Nice to have

**21. `trader.py:1195` — ML filter blocks when ML side disagrees with venue side, but ML may be wrong**  
- **What this is:** At line 1195, if `ml_side != sig.side`, the signal is dropped with log "ML BLOCK".  
- **Problem:** The ML model (trained on historical data) predicts the venue mid, not the true probability. The AGENTS.md "ML MODEL VALIDATION" section says the ML model is better calibrated but selects 0 trades because it predicts the mid. The GBM gate's edge comes from being *overconfident*. Using the ML model as a veto may block profitable trades the GBM would take.  
- **Fix:** Make ML filter advisory only (log disagreement but don't block), or use it as a confidence adjuster, not a hard veto.  
- **If we skip it:** ML model silently reduces trade count; hard to measure its value.

**22. `market_finder.py:264-269` — Hedge check uses `ya <= 0` to skip, but `ya` could be `None`**  
- **What this is:** `if ya <= 0: pass` at line 273.  
- **Problem:** `_parse_market_prices` returns `0` for missing prices (line 147 in market_finder.py: `if v in (None, ""): return 0`). So `ya` is never `None`, it's `0`. The check works but the intent is unclear — `ya <= 0` means "no YES ask or YES ask is 0". A price of 0 is invalid (valid range 1-99), so this correctly skips. But using `0` as "missing" conflates "no quote" with "price is 0c".  
- **Fix:** Use `None` for missing prices throughout, or a sentinel. The `_price_field` and `_q` helpers already return `0` for missing; changing this is a large refactor.  
- **If we skip it:** Works as-is; just a semantic smell.

**23. `fees.py:38-49` — `taker_fee_dollars` and `taker_fee_cents` duplicate logic**  
- **What this is:** `taker_fee_cents` calls `taker_fee_dollars` then converts to cents with ceiling.  
- **Problem:** The cent-grid rounding in `taker_fee_cents` uses `math.ceil(raw * 100 - 1e-9)` while `buy_cash_out` uses `math.ceil((cost + fee) * 100 - 1e-9) / 100.0`. The fee rounding is done twice: once in `taker_fee_cents` (to cents), once in `buy_cash_out` (to dollars then cents). They should agree but it's two code paths.  
- **Fix:** Have one canonical fee function that returns the effective fee in dollars (cent-grid rounded), and derive cents from it.  
- **If we skip it:** Two rounding paths; risk of 1-cent drift if they diverge.

**24. `llm_eval.py:40-44` — Timeout/retry config re-uses `config._env_int/float` but those are private**  
- **What this is:** Lines 41-43 call `config._env_int("LLM_REQUEST_TIMEOUT", 20)` etc.  
- **Problem:** `_env_int` and `_env_float` are prefixed with `_` (private). They're called at module load time, before `config` is fully initialized (though `config` loads `.env` at import). It works but violates encapsulation.  
- **Fix:** Add public `config.get_env_int` helpers, or move these defaults to `config.py` proper.  
- **If we skip it:** Works; just a style nit.

**25. `crossvenue.py:36-37` — Regexes for Kalshi/Polymarket tickers are brittle**  
- **What this is:** `_KALSHI_EVENT` and `_PM_SLUG` parse tickers/slugs with regex.  
- **Problem:** Kalshi changed ticker format before (e.g., adding `15M` suffix). Polymarket slugs vary. The regexes will break silently on format changes, returning `None` and skipping markets.  
- **Fix:** Use the API's structured fields (`event_ticker`, `series_ticker`, `asset`, `expiry`) instead of parsing strings. The `fetch_kalshi_ladder` already gets markets with `floor_strike`; the event ticker parsing is only for matching to Polymarket.  
- **If we skip it:** Cross-venue comparison silently drops markets when formats change.

---

## Verdict

**Healthy with caveats.** The codebase is production-grade for a single-operator bot: 439 tests pass, token-corruption gates are active, risk management is layered (cash floor, exposure ceiling, drawdown halt, per-market loss, consecutive-loss cooldown), P&L is idempotent via local ledger + Supabase + n8n replay, and the cycle survives transient failures with backoff/rebuild. The main risks are:

1. **Stale market cache** after order placement (finding #1) — can cause missed entries or bad prices.
2. **Dry-run risk metrics use cost-basis** (finding #2) — paper curve doesn't trigger real guards.
3. **Single-process bid history** (finding #5) — won't work if scaled to multiple processes.
4. **LLM evaluation runs before cheaper gates** (finding #9) — wastes cycle budget on signals gates would block.

Fix #1 and #2 before live deployment. #5 is a scaling limit. #9 is a performance optimization.

---

## Lean: -1,200 lines, -3 dependencies possible

- **Delete dead code:** `_evaluate_two_sided` in `signals.py` (~100 lines), `api_price_to_internal_no` in `kalshi_client.py` (~15 lines), `extra` collection in `market_finder.py` (~30 lines), `_paper_guards_relaxed` bypass in `risk.py` (~20 lines).
- **Merge duplicate fee logic:** `taker_fee_dollars` + `taker_fee_cents` + `buy_cash_out` fee path (~40 lines).
- **Remove unused `ml_model` torch dependency** from requirements (it's lazy-loaded and only used with `--ml-model`; the backtest/ML training scripts are separate). Torch is heavy on aarch64/Termux.
- **Consolidate `config._env_int/float/bool/str`** into a single `Config` class with validation, removing 60 lines of boilerplate.
- **Replace regex ticker parsing** in `crossvenue.py` with API fields (~50 lines).

---

## Not checked

- `scripts/` directory (28 files) — operational utilities, not in the trading loop.
- `polymarket_us_client.py` / `derive_poly_creds.py` — separate Polymarket US integration, not used by the main bot.
- `backtest.py` / `parallel_backtest.py` / `parallel_backtest_hf.py` — backtest runners, not live path.
- `data_collector.py`, `kalshieye_downloader.py`, `historical_downloader.py` — data collection, read-only.
- `n8n_coordination.py`, `inject_n8n_workflows.py`, `add_n8n_credentials.py` — n8n setup, run once.
- `local.py` (MemPalace integration) — not readable via MCP in this session.
- `ladder_strategy.py` — used by ML filter, not directly in live loop.
- `fund_tracker.py` — not imported by main loop.
- `model.py` unit tests — only `test_model.py` exists, covers basics.
- `binance_feed` WebSocket path (`stream_trades`) — unused by bot (uses REST only).
- n8n workflow internals — managed via DB injection, not code.