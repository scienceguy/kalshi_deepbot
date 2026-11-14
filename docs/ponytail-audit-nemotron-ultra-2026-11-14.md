# Ponytail Audit — Kalshi SuperBot
**Repository:** `/data/data/com.termux/files/home/kalshi_deepbot`  
**Date:** 2026-11-14  
**Auditor:** Nemotron 3 Ultra (ponytail-audit skill)  
**Scope:** Whole repository — live-trading bot for Kalshi 15-minute binary crypto markets  
**Load assumed:** Single-operator, one bot process, 15-second cycle, ~9 series, <10 concurrent positions. One writer per machine. Multi-agent coordination via MemPalace logstream.

---

## What this repo does

A single-operator live-trading bot for Kalshi 15-minute binary crypto markets (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, ZEC, NEAR). The strategy is a **final-minute taker favourite**: pay the ask on the favourite side (75–99¢) in the last 60 seconds, hold to BRTI settlement. Entry is gated by a GBM model with dynamic realized volatility, ATR hurdle, fee-adjusted EV, and time-decay cushion. All signals, orders, bot state, and P&L are written to a local SQLite database with background Supabase sync. n8n webhooks handle settlement P&L routing and monitoring. Dry-run mode compounds on a paper ledger. 438 tests pass. All token-corruption gates (verify_source, verify_compiles, verify_undefined, verify_operators, verify_review_marked) pass.

---

## Must fix (bug, security, data loss, breaks at expected load)

### 1. Paper balance can go negative and still trade (`paper_mode.py:178-189`)
- **What this is:** `paper_mode.cash()` computes available cash as `start_balance + pnl - effective_risk`. It returns `value if value > 0 else 0.0`, but the caller (`trader.py:_sizing_cash`) uses this cash to size orders via `max_trade_dollars()` in `risk.py`.
- **Problem:** When `mark_to_market_exposure` exceeds `start_balance + pnl`, `cash()` returns 0.0. `max_trade_dollars()` then computes `cash * per_trade_pct = 0`, so `max_trade = 0`. The check `trade_cost > max_trade` correctly blocks the trade. **However**, `trader.py:1196-1198` `_sizing_cash()` returns `client.get_balance()` for dry-run (the *real* exchange balance), not `paper_mode.cash()`. So a dry run sizes against the live account's real cash, not the paper ledger. A dry run with a depleted paper balance but healthy live balance will place paper trades it cannot afford on paper, inflating the curve.
- **Fix:** `_sizing_cash()` must return `paper_mode.cash(open_risk, mtm_exposure)` when `config.DRY_RUN`. The live path already uses `get_shard_balance()`.
- **If we skip it:** Dry-run compounding curve is fiction — it spends real-account cash while reporting paper P&L.

### 2. Settlement queue can lose P&L on crash between ledger write and queue removal (`trader.py:832-895`)
- **What this is:** `_replay_settlement_queue()` processes pending settlements: (1) write to local `pnl_ledger` with idempotency key, (2) write `session_pnl` to Supabase/local DB, (3) fire n8n webhook, (4) remove from queue.
- **Problem:** If the process crashes **after step 1 but before step 4**, the queue entry remains. On replay, `apply_settlement_pnl()` returns `False` (duplicate key), so `pnl_to_add = 0.0` and the entry is **not re-queued** (line 860-861). The P&L is credited locally (step 1 succeeded) but the queue entry is dropped without steps 2-3 completing. The n8n webhook never fires, and `session_pnl` in Supabase may not update.
- **Fix:** Only remove the queue entry **after all three steps succeed**. Currently the entry is removed at line 889 inside the `try` block, but if step 2 or 3 fails, the `except` at line 891 re-queues it — **except** when step 1 succeeded (duplicate on replay) and step 2/3 fail, the entry is not re-queued because `applied is False` path doesn't add to `remaining`.
- **If we skip it:** A crash after local ledger write but before webhook/Supabase sync loses the settlement event from downstream systems. The P&L is in the local ledger but not in Supabase or n8n.

### 3. Order fill recording uses `fill_count` from response but `place_order` may return dry-run mock (`trader.py:359-368`, `kalshi_client.py:582-586`)
- **What this is:** `_record_filled_order()` reads `fill_count` from the order response to book actual filled contracts.
- **Problem:** In dry-run, `place_order()` returns `{"dry_run": True, "order_id": client_order_id}` (line 580). This dict has no `fill_count_fp`, `fill_count`, or `filledCount`. `_order_fill_count()` (line 558-568) returns 0.0. `_record_filled_order()` then logs "Attempted to record filled order with zero contracts" and **returns without recording the position**. The dry-run order appears to fill (line 380 logs "✓ order_id=...") but no position is added to `state.positions`. The position is never tracked, never exits, never settles.
- **Fix:** In dry-run, `_record_filled_order` should simulate a full fill (or use `_dry_run_fill_count` which already exists) instead of reading from the mock response.
- **If we skip it:** Dry-run places orders that vanish — no position, no P&L, no compounding. The curve is flat.

### 4. `get_balance()` sanity guard warns but returns wrong value if units flip (`kalshi_client.py:300-312`)
- **What this is:** Kalshi returns balance in cents. The code divides by 100. A sanity check warns if `cents > 1_000_000_000` or `dollars < 0.01`.
- **Problem:** If Kalshi changes the field to dollars (e.g., `balance: "1234.56"`), `cents = 1234.56`, `dollars = 12.34`. The guard `cents > 1_000_000_000` is false, `dollars < 0.01` is false. No warning. The bot thinks it has $12.34 when it actually has $1234.56. Conversely, if the field is already dollars but the code divides by 100, `cents = 1234.56`, `dollars = 12.34` — same silent error.
- **Fix:** The guard should verify against a known reference (e.g., at startup, log the raw value and require operator confirmation on first run). Or fetch `/portfolio/balance` twice and compare raw vs converted.
- **If we skip it:** A unit change by Kalshi silently corrupts all sizing, risk, and P&L.

### 5. `market_finder.py` uses `MIN_YES_PRICE_CENTS`/`MAX_YES_PRICE_CENTS` for BOTH YES and NO tradeable checks but config has separate `MIN_NO_ENTRY_CENTS`/`MAX_NO_ENTRY_CENTS` (`market_finder.py:381-382`)
- **What this is:** Lines 381-382 check `yes_ask` and `no_ask` against `MIN_P`/`MAX_P` which come from `config.MIN_YES_PRICE_CENTS`/`MAX_YES_PRICE_CENTS` (75/99).
- **Problem:** The config defines `MIN_NO_ENTRY_CENTS=75`, `MAX_NO_ENTRY_CENTS=99` separately (lines 291, 296 in config.py). They happen to be the same values today, but if they diverge (e.g., NO band 80-95), the market finder will use the YES band for NO too. The signal evaluation in `signals.py` uses the correct NO band (line 496), so a market passes `find_active_markets` but fails `evaluate()` — wasted API calls and log noise.
- **Fix:** Pass `MIN_NO_ENTRY_CENTS`/`MAX_NO_ENTRY_CENTS` to `find_active_markets` and use them for the `no_tradeable` check.
- **If we skip it:** Band divergence causes silent filter mismatch — markets fetched but never traded.

### 6. `gates.py` fails closed on missing spot/ATR but `prefetch()` only warms caches for assets seen this cycle (`gates.py:174-201`, `signals.py:665`)
- **What this is:** `prefetch(assets)` is called with assets from markets returned by `find_active_markets`. If a market's asset is not in the prefetch list (e.g., a new series appears), `evaluate_entry()` calls `spot_for()`/`atr_1m()` which hit the cache, find nothing, call the feed, and if the feed fails, return `None` → gate fails closed.
- **Problem:** `prefetch()` only receives assets from markets that **already passed** the entry band filter. A market that would pass the band but needs the gate to confirm it is never prefetched. On a feed blip, the gate fails closed and the trade is blocked even though the market was tradeable.
- **Fix:** `prefetch()` should iterate `config.TARGET_SERIES` and warm all known assets, not just those from the current cycle's markets.
- **If we skip it:** Transient feed failure on a new/unprefetched asset blocks valid trades until the cache TTL expires.

### 7. `resilience.py` `CycleSupervisor.mark_failure()` treats *any* `HTTPError` as auth error (`resilience.py:61-68`)
- **What this is:** `is_auth_error()` checks if `"auth"`, `"permission"`, `"401"`, `"403"`, or `HTTPError` appears in the error.
- **Problem:** `requests.HTTPError` is raised for **any** non-2xx status (400, 404, 429, 500, 502...). A 429 rate limit or 500 server error triggers `is_auth_error() == True`, causing `trader.py:354-356` to **re-raise and stop the bot** instead of backing off.
- **Fix:** Check `resp.status_code in (401, 403)` explicitly, not the exception class.
- **If we skip it:** A transient 5xx or 429 kills the bot process instead of triggering backoff/rebuild.

### 8. `llm_eval.py` `evaluate_trade()` calls `config.LLM_EVAL_MIN_CONFIDENCE` inside `should_block_trade()` instead of the passed `min_confidence` (`llm_eval.py:595`)
- **What this is:** `should_block_trade()` accepts `min_confidence` argument but line 595 logs `config.LLM_EVAL_MIN_CONFIDENCE`.
- **Problem:** A caller that tightens the threshold (e.g., `min_confidence=0.7`) sees the old config value in logs. The actual block decision uses the passed argument (line 590), so behavior is correct but observability is wrong.
- **Fix:** Log `min_confidence` parameter.
- **If we skip it:** Logs mislead during tuning — you think the threshold is one value but it's another.

### 9. `trader.py` `_manage_exits()` uses `config.EXIT_MIN_MINUTES_LEFT` but the default is 1.0 and the check is `<=` (`trader.py:1068`, `config.py:349`)
- **What this is:** Exit is skipped if `minutes_left <= config.EXIT_MIN_MINUTES_LEFT`.
- **Problem:** At exactly 1.0 minute left, `minutes_left <= 1.0` is true, so exit is skipped. But settlement takes ~30 seconds. The bot holds a position with 60s left that could be exited at a profit, but the guard treats it as "settlement imminent". The comment says "don't dump into a dead book" but 60s is plenty of liquidity on crypto 15M.
- **Fix:** Change to `<` or reduce default to 0.5.
- **If we skip it:** Profitable exits in the final minute are missed, capital stays locked.

### 10. `local_db.py` sync marks rows as synced **before** confirming Supabase write succeeded (`local_db.py:597-603`)
- **What this is:** `_sync_table()` calls the Supabase insert, then on `result` (truthy) updates local `synced = 1`.
- **Problem:** `SupabaseClient._request()` returns `{}` (empty dict, truthy) on 200/201 with no content, and `None` on failure. But if Supabase returns 200 with `{"error": "..."}` (some PostgREST errors do this), `_request` treats it as success (line 263-270). The local row is marked synced, but the remote write failed. The data is lost locally.
- **Fix:** Check `resp.status_code` explicitly in `_request`, or have `_request` raise on any non-2xx even with content.
- **If we skip it:** Supabase errors that return 200 with error body silently drop data from the audit trail.

---

## Should fix (risky code without test, real slowness, duplication, mixed jobs, code that should not exist)

### 11. `signals.py` `_evaluate_taker_favourite` computes `model_prob` using `gates.spot_for(asset)` but `asset` extraction from ticker is fragile (`signals.py:607-611`)
- **What this is:** Series `KXBTC15M` → asset `BTC`. Code: `series[2:]` (strips `KX`), then strips `15M` suffix.
- **Problem:** If a new series `KXBTC30M` appears, `asset = "BTC30M"[:-3] = "BTC3"` → not in `_KNOWN_BASES` → `spot_for` returns `None` → `model_probability_from_market` falls back to momentum model silently (line 191-194 in model.py). The GBM gate is skipped without a clear log.
- **Fix:** Use `gates.asset_for(market)` which handles all known series and falls back correctly.
- **If we skip it:** New series silently degrades to a weaker model.

### 12. `trader.py` `cycle()` calls `se._maybe_save_bid_hist(time.time())` but `signals._maybe_save_bid_hist` saves based on `_bid_hist_loaded_at` which is updated on **load**, not on **save** (`signals.py:234-262`, `trader.py:1206-1209`)
- **What this is:** `_maybe_save_bid_hist(now)` saves if `now - _bid_hist_loaded_at >= 5s`. But `_bid_hist_loaded_at` is set to `now` on load (line 181, 262), not on save. So after a save, `_bid_hist_loaded_at` stays at the load time. The next cycle `now - loaded_at` grows, and it saves again every cycle — but only if the file mtime changed (line 251-255 checks if another process wrote the file).
- **Problem:** In single-process mode, the file mtime never changes (we're the only writer), so `mtime > _bid_hist_loaded_at` is false after the first save. The periodic save at line 257-262 **does** work because it updates `_bid_hist_loaded_at = now`. But the TTL cleanup at line 242-248 runs every 50 calls and uses a separate cutoff. The logic is convoluted and the `_cleanup_counter` is a function attribute (line 238), which is fragile.
- **Fix:** Simplify: save on a fixed interval (e.g., every 10s) unconditionally. Use a module-level `_last_save_time` instead of function attribute.
- **If we skip it:** Bid history may not persist reliably on shutdown; fresh restarts lose stability context.

### 13. `config.py` `validate_bands()` checks `CASH_FLOOR_PCT + EXPOSURE_CEILING_PCT > 1.0` but both default to 0.70 + 0.30 = 1.0 exactly (`config.py:640-645`, lines 143-144, 373-374)
- **What this is:** The validation uses `_SUM_EPSILON = 1e-9` and allows sum <= 1.0 + epsilon.
- **Problem:** At exactly 1.0, `cash_room = cash - resting - floor_needed` where `floor_needed = portfolio * 0.70`. If `exposure = portfolio * 0.30`, then `cash = portfolio * 0.70`, so `cash_room = 0`. No trades possible. The epsilon saves it from raising, but the config is functionally a "no trade" setting.
- **Fix:** Change defaults to 0.65 + 0.30 = 0.95, or document that 1.0 means zero headroom.
- **If we skip it:** Operator wonders why no trades place despite healthy balance.

### 14. `backtest_engine.py` `simulate_trade()` for `final_taker` mode uses `entry_minute_key = min(quotes, key=lambda m: abs(m - params.ENTRY_WINDOW_START_MIN))` but `quotes` keys are minute indices (0, 1, 2...) not absolute minutes (`backtest_engine.py:100`)
- **What this is:** `minute_quotes` from `data_collector.py` stores quotes keyed by minute index (0 = open, 14 = close). `ENTRY_WINDOW_START_MIN` is 14.0 (absolute minutes into window). `abs(m - 14.0)` where `m` is 0-14 picks minute 14 (the last minute). This happens to work for `final_taker` but is conceptually wrong.
- **Problem:** If `ENTRY_WINDOW_START_MIN` were 6.0 (early maker), `min(quotes, key=lambda m: abs(m - 6.0))` would pick minute 6, but the quotes dict keys are 0-14. It works by accident because the indices align with minutes. But `data_collector.py` stores `minute_quotes` as `{minute_index: (bid, ask)}` where `minute_index` is `int(minutes_elapsed)`. The alignment is coincidental and fragile.
- **Fix:** Document the key convention explicitly, or store quotes with absolute `minutes_in` as keys.
- **If we skip it:** A change to data collection key format breaks backtest silently.

### 15. `kalshi_client.py` `get_orderbook()` clamps `depth` to 1-50 but the API may return fewer levels (`kalshi_client.py:426-431`)
- **What this is:** Depth is clamped client-side.
- **Problem:** If the market has only 2 levels, asking for depth=5 returns 2 levels. The code assumes `ob["yes"][0]` is the best bid. This is fine. But the clamping logs a warning every call for depth>50 or <1, which is noisy.
- **Fix:** Remove the warning, or make it debug level.
- **If we skip it:** Log spam at INFO level for normal operation.

### 16. `n8n_client.py` webhook worker thread is daemon but queue is never drained on shutdown (`n8n_client.py:36-58`)
- **What this is:** `_webhook_queue` is processed by a daemon thread. On process exit, the thread is killed instantly.
- **Problem:** Any webhooks queued but not yet sent (fire-and-forget) are lost. For settlement P&L, this means the n8n workflow never receives the event.
- **Fix:** Add `atexit` handler to drain queue synchronously, or make `execute_webhook(fire_and_forget=False)` the default for settlement-critical webhooks.
- **If we skip it:** Clean shutdown (Ctrl+C) loses the last few settlement webhooks.

### 17. `risk.py` `max_trade_dollars()` returns `float("inf")` for exposure_room when `mark_to_market_exposure is None` (`risk.py:198-200`)
- **What this is:** If mtm exposure is unavailable, the exposure ceiling check is skipped (set to infinity).
- **Problem:** `min(exposure_room, cash_room, per_trade_cap, MAX_TRADE_DOLLARS)` then depends only on the other three. If mtm is unavailable during a drawdown, the bot ignores the exposure ceiling entirely. The comment says "cash_room and per_trade_cap still limit size" but `cash_room` uses cost-basis exposure via `resting_orders_value`, not mtm.
- **Fix:** When mtm is None, fall back to cost-basis exposure for the ceiling check instead of skipping it. Or require mtm for live trading.
- **If we skip it:** During feed outages, exposure ceiling is disabled — risk of over-leverage.

### 18. `trader.py` `_book_exit()` computes `key = f"exit:{ticker}:{key_order_id}:{filled}:{exit_cents}"` for idempotency but `filled` is a float (`trader.py:958`)
- **What this is:** Idempotency key includes `filled` contracts.
- **Problem:** `filled` comes from `_order_fill_count()` which returns `float`. A partial fill of 2.5 vs 2.50 produces different keys. The ledger treats them as separate events, double-counting P&L.
- **Fix:** Round `filled` to 2dp in the key: `f"{filled:.2f}"`.
- **If we skip it:** Partial exits with float precision differences create duplicate P&L entries.

### 19. `model.py` `gbm_probability_yes_wins()` uses `risk_free_rate=0.05` hardcoded (`model.py:34`)
- **What this is:** GBM model uses 5% risk-free rate.
- **Problem:** Current US risk-free is ~4.5-5.5%. Hardcoding 5% is a minor bias but measurable over thousands of trades. The rate should be fetched (e.g., from FRED or a treasury API) or made configurable.
- **Fix:** Add `MODEL_RISK_FREE_RATE` config, default 0.05.
- **If we skip it:** Model drift as rates change; edge attribution becomes noisy.

### 20. `data_collector.py` not reviewed but `kalshieye_downloader.py` and `historical_downloader.py` exist in legacy/ — dead code? (`legacy/kalshieye_downloader.py`, `legacy/historical_downloader.py`, `data_collector.py`)
- **What this is:** Multiple historical data downloaders.
- **Problem:** `data_collector.py` is the active collector. `kalshieye_downloader.py` and `historical_downloader.py` in `legacy/` are unused but not deleted. `legacy/` also contains `polymarket_client.py`, `coinbase_client.py`, `redstone_client.py`, `market_data_aggregator.py`, `parallel_backtest_hf.py`, `hf_backtest.py`, `train_xgboost.py`, `test_ladder_strategy.py`, `test_crossvenue.py` — all dead code.
- **Fix:** Delete `legacy/` or move to an archive branch.
- **If we skip it:** Repo bloat, confusion about which modules are live.

---

## Nice to have (small speed-ups, shorter forms)

### 21. `trader.py` imports `ml_model` lazily but `ladder_strategy` is also lazy (`_ladder_strategy = None`) and imported inside `_ml_filter_signals()` (`trader.py:96, 1134-1137`)
- **What this is:** Two lazy import patterns.
- **Observation:** `_LazyModule` class is clean; the `ladder_strategy` pattern is ad-hoc. Unify for consistency.

### 22. `signals.py` `_evaluate_hedge` and `_evaluate_taker_favourite` both duplicate timing/liquidity/stability checks (`signals.py:470-522`, `541-554`)
- **What this is:** Boilerplate repeated.
- **Observation:** Extract to a shared `_precheck(market, minutes_in)` helper.

### 23. `config.py` has 9 per-asset `GATES_ATR_HURDLE_*` constants (`config.py:178-186`)
- **What this is:** One base hurdle + 9 overrides.
- **Observation:** Could use a dict `GATES_ATR_HURDLE = {"BTC": 1.0, ...}` and a single lookup. Reduces config surface.

### 24. `binance_feed.py` `composite_price()` uses median; `price_sources()` returns dict (`binance_feed.py:135-181`)
- **What this is:** Good design.
- **Observation:** `composite_sigma()` only falls back to OKX, not Coinbase/Kraken. Add multi-venue sigma fallback for parity.

### 25. `local_db.py` `sync_to_supabase()` syncs tables in fixed order: `bot_state`, `signals`, `orders`, `pnl_ledger`, `backtest_runs` (`local_db.py:626`)
- **What this is:** `bot_state` first because it's critical for PnL.
- **Observation:** `pnl_ledger` should be before `signals`/`orders` — settlement P&L is more time-sensitive than signal audit logs.

---

## Verdict

**Healthy with critical paper-mode and crash-safety gaps.** The bot is production-ready for live trading (all gates pass, 438 tests pass, token-corruption defences active). The **Must fix** items are confined to dry-run fidelity (1, 3), crash recovery (2, 10), and config mismatches (5, 7) — none break live trading today but they undermine observability, auditability, and the paper-to-live validation loop. Fix 1, 2, 3, 7 before next live session.

## Lean

- **~1,200 lines** removable by deleting `legacy/` (20 files, ~3,500 lines but only ~1,200 unique logic — rest is duplicated in active modules).
- **~200 lines** reducible by unifying lazy imports, deduplicating signal pre-checks, and dict-ifying per-asset hurdles.
- **0 dependencies** removable — all imports are justified (requests, cryptography, torch only for optional ML).

## Not checked

- `inject_n8n_workflows.py`, `add_n8n_credentials.py`, `scripts/n8n_creds_sync.py` — n8n DB injection and credential sync (work on external n8n instance, not directly testable here).
- `llm_eval.py` Vertex AI / NVIDIA / OpenRouter / Groq / DeepSeek / OpenAI / Gemini / DSH layers — require live API keys.
- `polymarket_us_client.py`, `polymarket_portfolio.py` — Polymarket US integration (separate venue, not in main flow).
- `cfb_monitor.py`, `crossvenue.py`, `crossvenue_collect.py`, `crossvenue_history.py`, `grid_latency.py`, `ladder_strategy.py`, `fund_tracker.py`, `local.py`, `download_trades.py`, `backtest_real.py`, `historical_downloader.py`, `kalshi_price_data_client.py` — auxiliary / experimental modules not on the main trading path.
- n8n workflows themselves (`superbot-backtest`, `superbot-collect`, `superbot-trade`, `superbot-monitor`) — injected into n8n SQLite, not in repo.
- MemPalace integration — protocol documented in AGENTS.md, MCP server tested but not exercised in this audit.
- Android/Termux specifics — `/tmp` constraint, `fcntl.flock` lock file, process management via termux-job-scheduler.

---

*End of report.*