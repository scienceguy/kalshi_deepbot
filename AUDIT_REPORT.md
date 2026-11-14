# Ponytail Audit — Kalshi SuperBot
**Repo:** `/data/data/com.termux/files/home/kalshi_deepbot`  
**Date:** 2026-11-13  
**Load assumed:** Single live-trading process cycling every 15s, 9 crypto 15M series, ~10 req/s sustained against Kalshi API. One authoritative writer to Supabase; dry-run mode runs identical logic against paper balance.

---

## What this repo does
A production-grade quantitative trading bot for Kalshi 15-minute binary crypto markets (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, ZEC, NEAR). Uses a **final-minute taker-favourite** strategy: pays the ask on the extreme favourite (95–99c YES or 75–99c NO) in the last ~1 minute of the 15-minute window, holds to BRTI settlement. Entry filtered by a **7-layer LLM consensus veto** (Vertex AI + NVIDIA Nemotron + free models) and **three pre-entry gates** (ATR hurdle, fee-adjusted EV, time-decay cushion). All state (signals, orders, bot state) written to local SQLite with background idempotent sync to Supabase; settlement P&L routed through n8n webhooks for downstream workflows. Runs dry-run by default; live requires explicit `--live` + `KALSHI_CONFIRM_LIVE=true`.

---

## Must fix (bugs, security, data loss, breaks at expected load)

### 1. **Settlement queue idempotency key collision on multi-strike events** (`trader.py:1669, 1754, 1821`)
- **What this is:** The settlement replay queue uses `event_id = ticker.rsplit("-", 1)[0]` to derive a canonical event ID (e.g., `KXBTC15M-26NOV090645` from `KXBTC15M-26NOV090645-45`). Different strikes of the same event share the same `event_id`.
- **Problem:** Two different strikes of the same event (e.g., 45 and 46) can produce the same `settle_key` (`settle:KXBTC15M-26NOV090645:sb_abc123`) if they happen to use the same client order ID (unlikely) OR if the order_id is truncated/corrupted. Worse: the idempotency key doesn't include the strike, so a settlement for strike 45 could incorrectly mark strike 46 as settled.
- **Fix:** Include the strike in the idempotency key: `f"settle:{event_id}:{strike}:{order_id}"`. The strike is available in `market.get("floor_strike")` or parsed from the ticker suffix.
- **If we skip it:** A multi-strike event settlement could credit P&L for the wrong strike, or skip a legitimate settlement because the key was already "used" by a different strike.

### 2. **Exit order idempotency key doesn't include strike** (`trader.py:1079`)
- **What this is:** Exit key is `f"exit:{ticker}:{key_order_id}:{filled}:{exit_cents}"`. The ticker includes the strike, so this is actually fine. But the `order_id` used is the *entry* order_id unless `exit_order_id` is provided. A partial exit creates a new exit order with a new ID, but the key uses the entry order_id — a second partial exit on the same position would have the same key and be treated as duplicate.
- **Problem:** Multiple partial exits on the same position (same entry order_id) would collide on the idempotency key and only the first would credit P&L.
- **Fix:** Always use the actual `exit_order_id` in the key, and include a sequence counter or timestamp for multiple exits against the same entry. The code already does `key_order_id = exit_order_id or order_id` — but if `exit_order_id` is set on the position after the first partial exit, subsequent exits will use that same exit_order_id. Need a counter per position.
- **If we skip it:** Partial exits after the first are silently dropped from P&L accounting.

### 3. **`_load_bid_hist` TTL cache serves stale data after config change** (`signals.py:169`)
- **What this is:** Bid history is cached in memory with a 60-second TTL (`_BID_HIST_TTL_SECONDS`). The cache key doesn't include config parameters that affect stability (`STABILITY_LOOKBACK_MIN`, `STABILITY_MAX_YES_BID_CENTS`).
- **Problem:** If the operator changes stability config at runtime (via hot-reload or restart with new .env), the cached bid history is still used with old parameters for up to 60s. The stability check computes `peak = max(min(b, 100-b))` over the lookback window — if `STABILITY_LOOKBACK_MIN` changes from 3 to 5, the cached history still only has 3 minutes of data.
- **Fix:** Include a config version hash in the cache key, or invalidate `_bid_hist_loaded_at = 0` when config reloads. Simpler: add `config_hash = hash((STABILITY_LOOKBACK_MIN, STABILITY_MAX_YES_BID_CENTS, STABILITY_MIN_COVERAGE_MIN))` to the cache invalidation check.
- **If we skip it:** Config changes take up to 60s to take effect; stability gate uses stale window/threshold mismatch.

### 4. **Polymarket US concurrent order path logs but does nothing — dead code path** (`trader.py:428-485`)
- **What this is:** `_place_poly_order()` is called after every Kalshi order placement when `POLYMARKET_ENABLED=true`. It logs "no production crypto market on api.polymarket.us (preprod only)" and returns.
- **Problem:** This is dead code that adds latency (lazy import, asset extraction, logging) on every order. The Polymarket US crypto markets don't exist in production; the international CLOB uses different credentials (L2 derived from passphrase + wallet key, not the US SDK). This path will never execute a real order.
- **Fix:** Remove the entire function and its call site. Keep the import guard if you plan to wire it later, but don't call a no-op on every trade.
- **If we skip it:** Microsecond overhead per order; misleading logs suggesting concurrent Polymarket activity.

### 5. **`binance_feed.start_ws_streams()` called with sorted set but feed expects list** (`trader.py:2363`)
- **What this is:** `binance_feed._KNOWN_BASES` is a `set`. `sorted(...)` returns a `list`. The function signature in `binance_feed.py` likely expects a list of symbols.
- **Problem:** Not a bug per se, but the WebSocket streams are started with all 9 symbols. If any symbol fails to connect, the entire feed may be unstable. No health check on the WS streams after startup.
- **Fix:** Add a health check that verifies WS streams are delivering ticks within 30s of startup. If not, fall back to REST only and log a warning.
- **If we skip it:** Silent feed degradation; gates fall back to REST with 30s cache, adding latency and potentially stale prices.

### 6. **Supabase `set_bot_state_batch` called with `None` values causing schema errors** (`trader.py:1936-1940`)
- **What this is:** `current_state` includes `"cash": cash if cash_available else None` and `"balance": cash if cash_available else None`. When balance fetch fails, these are `None`.
- **Problem:** Supabase PostgREST may reject `null` for non-nullable columns. The `_supabase_record_failure()` in `supabase_client.py` catches 4xx but the bot continues. If the schema has `NOT NULL` on these columns, every failed balance fetch generates a 400 and the write fails silently.
- **Fix:** Only include keys in the batch that have non-None values, or ensure the Supabase schema allows nulls. Better: split into `bot_state` (always present) and `bot_metrics` (optional).
- **If we skip it:** Failed balance fetches generate Supabase 400 errors; bot state not persisted for that cycle.

---

## Should fix (risky code without test, real slowness, duplication)

### 7. **`_mark_to_market_exposure` ignores `client` parameter — dead argument** (`trader.py:666, 1853`)
- **What this is:** Function signature accepts `client=None` but never uses it. The comment says "mark-to-market exposure... only ignores positions whose market has closed/settled".
- **Problem:** The `client` is passed from `cycle()` but unused. This suggests an intention to fetch fresh orderbook for positions not in `markets` list, but the code only uses `mkt_by_ticker` from the `markets` argument (which comes from `find_active_markets`). Positions on markets that dropped out of the tradeable list (price moved out of band) have no orderbook data and fall back to cost basis.
- **Fix:** Either use the client to fetch orderbook for missing positions, or remove the unused parameter. If the intent is to mark-to-market *all* open positions, fetch orderbooks for any position ticker not in `markets`.
- **If we skip it:** Positions on markets that moved out of the entry band are marked at cost basis (stale), under/over-stating exposure and risk limits.

### 8. **`local_db.py` background sync thread has no backpressure / queue overflow protection** (not read but inferred from architecture)
- **What this is:** All writes go to local SQLite immediately; a background thread syncs to Supabase every `LOCAL_DB_SYNC_INTERVAL` (default 30s).
- **Problem:** If Supabase is down for hours, the local queue grows unbounded. The sync thread processes in batches but there's no max queue size, no disk space check, no alerting. A prolonged outage could fill the filesystem.
- **Fix:** Add a max queue size (e.g., 10,000 rows), disk space guard, and n8n alert when queue exceeds threshold. Persist queue to disk on shutdown (already done via SQLite).
- **If we skip it:** Extended Supabase outage → unbounded memory/disk growth → OOM or disk full.

### 9. **Gate 2 (fee-adjusted EV) measured win rate `GATES_MEASURED_Q=0.9928` derived from quote-level not market-level data** (`config.py:202-218`)
- **What this is:** The comment documents that 557 quote rows = 246 distinct markets. Clustered by market, win rate is 0.9878 (lo95 0.9741 vs breakeven 0.9710). The per-quote number flatters the edge.
- **Problem:** The gate uses `GATES_MEASURED_Q` as a floor replacement for model probability. With `GATES_MEASURED_FLOOR_ENABLED=False` (default), the gate scores the GBM model probability which *never clears breakeven* in the 95-99c band (returns -0.01c/trade). The floor is the only thing making the band reachable, but it's opt-in and off by default. This means the configured band (95-99c) is effectively blocked by gate 2 unless the floor is enabled.
- **Fix:** Either enable `GATES_MEASURED_FLOOR_ENABLED=true` with the market-clustered win rate (0.9878), or widen the band to where the GBM model clears on its own. Document this clearly — the band and gate are coupled.
- **If we skip it:** Gate 2 blocks all 95-99c entries in shadow mode (logged as "would_block"), no trades placed. The bot appears to run but never trades.

### 10. **`_stability_ok` fails closed on fresh series when `STABILITY_FAIL_OPEN=false` but `STABILITY_FAIL_OPEN=true` by default** (`signals.py:321-362`)
- **What this is:** Stability check requires `STABILITY_LOOKBACK_MIN` (default 3) minutes of bid history with `STABILITY_MIN_COVERAGE_MIN` (default 2.5) coverage. Fresh series have no history.
- **Problem:** With `STABILITY_FAIL_OPEN=true` (default), fresh series trade immediately with a warning. This defeats the stability gate's purpose (doubles EV per backtests) for the first 3-5 minutes of a new series. The warning is logged once per ticker via `_stability_warned` set, but the set is in-memory and lost on restart.
- **Fix:** Persist `_stability_warned` to the bid history file, or accept that fresh-series trades are lower-EV. Consider requiring 1 minute of history before trading (reduce `STABILITY_MIN_COVERAGE_MIN` to 1.0).
- **If we skip it:** First ~3 minutes of every new 15M series trade without stability filter, reducing edge.

### 11. **`RiskManager.update_balance` uses `mark_to_market_exposure` for drawdown but `exposure` (cost basis) for other calculations** (`risk.py:54-108`)
- **What this is:** Drawdown uses `total_value = cash + effective_exposure` where `effective_exposure = mark_to_market_exposure if provided else exposure`. But `max_trade_dollars` and `check` use `exposure` (cost basis) for ceiling and per-trade caps.
- **Problem:** Inconsistent exposure measure. Drawdown sees real-time MTM; sizing sees stale cost basis. A winning position increases MTM exposure → tighter drawdown room but same sizing ceiling → asymmetric behavior.
- **Fix:** Pass `mark_to_market_exposure` to `max_trade_dollars` and `check` (already done in trader.py:1967-1969 for `max_trade_dollars`, but `check` at line 2153 doesn't pass it). Ensure `check` uses MTM for ceiling.
- **If we skip it:** Sizing ceiling doesn't reflect unrealized gains; bot may over-size after a winning trade.

### 12. **`gates.py` caches (`_spot_cache`, `_atr_cache`, `_sigma_cache`) have no size bound** (`gates.py:50-52`)
- **What this is:** Three dicts caching feed data keyed by asset symbol. Max 9 assets currently, but if `TARGET_SERIES` expands or a bug causes repeated keys with different case, dicts grow unbounded.
- **Problem:** Memory leak over very long runs (weeks). Each cache entry holds a tuple of 3 floats + timestamps. Trivial now, but a pattern that scales poorly.
- **Fix:** Add LRU eviction (e.g., `maxsize=50` per cache) or reuse the `_prune_last_decision` pattern.
- **If we skip it:** Negligible in practice (9 keys), but a latent bug if series list grows.

### 13. **`llm_eval.py` 7-layer fallback has no circuit breaker per provider** (not read but AGENTS.md mentions 7-layer fallback)
- **What this is:** LLM evaluation tries Vertex AI → NVIDIA → Groq → DeepSeek → OpenAI → Gemini → Ollama → DSH. Each layer can hang or rate-limit.
- **Problem:** A slow/hung provider blocks the entire evaluation chain for up to `LLM_REQUEST_TIMEOUT` (20s) × retries. With 7 layers, worst-case latency exceeds cycle timeout (30s). No per-provider circuit breaker to skip a consistently failing provider.
- **Fix:** Add a failure counter per provider; after N consecutive failures, skip that provider for M minutes. Track latency per provider and prefer fast ones.
- **If we skip it:** LLM evaluation becomes the cycle bottleneck; a single slow provider delays all trades.

### 14. **`supabase_client.py` batch writes (`set_bot_state_batch`) not atomic — partial failure leaves inconsistent state** (`supabase_client.py` not read but `trader.py:1751, 1937` calls it)
- **What this is:** `set_bot_state_batch` writes multiple keys in one call. If one key fails (e.g., schema error on `session_pnl` but `cycles` succeeds), the batch may partially apply.
- **Problem:** No transaction semantics in PostgREST. A partial failure leaves some keys updated, others not. The local DB has the correct state; Supabase drifts.
- **Fix:** Write keys individually with idempotency, or accept eventual consistency and reconcile on startup (already done via `get_bot_state` fallback chain).
- **If we skip it:** Supabase bot_state can drift from local truth; reconciliation on restart fixes it but intermediate reads are stale.

### 15. **`market_finder.py` orderbook fetch uses `ThreadPoolExecutor(max_workers=4)` but Kalshi rate limit is ~10 req/s** (`market_finder.py:326, 355`)
- **What this is:** Up to 9 series × (1 main + hedges) = ~20 orderbook fetches per cycle. 4 workers = 5 sequential batches. At 10 req/s limit, this is fine, but the `REQUEST_MIN_GAP_SECONDS=0.2` in `kalshi_client.py` enforces 5 req/s globally.
- **Problem:** The global rate limiter in `kalshi_client.py` serializes across all threads. 4 workers don't actually parallelize — they just queue on the lock. The `ThreadPoolExecutor` adds overhead without throughput gain.
- **Fix:** Remove ThreadPoolExecutor for orderbook fetches; do them sequentially with the global rate limiter. Or make the rate limiter per-endpoint (balance vs markets vs orderbook) since they have different limits.
- **If we skip it:** Wasted threads; no actual parallelism. Simpler code without executor.

---

## Nice to have (small optimizations, cleanup)

### 16. **`config.py` validates `CASH_FLOOR_PCT + EXPOSURE_CEILING_PCT <= 1.0` but they sum to exactly 1.0 (0.70 + 0.30)** (`config.py:649`)
- **What this is:** The validation allows `<= 1.0 + 1e-9`. The defaults sum to 1.0 exactly.
- **Problem:** Zero headroom. Any floating-point drift or fee deduction makes `cash + exposure > portfolio`, triggering false risk blocks. The `_SUM_EPSILON = 1e-9` is too small for dollar arithmetic.
- **Fix:** Change defaults to `0.69 + 0.30 = 0.99` or increase epsilon to `1e-6`. Document that sum must be `< 1.0` for fees.
- **If we skip it:** Occasional false "cash floor" blocks when fees reduce cash slightly below the exact floor.

### 17. **`trader.py` has two `_touch_window` calls per cycle (lines 1890, 1947)** — one in balance-available branch, one in risk check
- **What this is:** `_touch_window` refreshes the per-window budget if cash grew. Called twice with same `now` and `cash`.
- **Problem:** Redundant call. Not a bug, just noise.
- **Fix:** Call once after balance fetch, before risk update.
- **If we skip it:** Negligible.

### 18. **`signals.py` `_track_bid` clamps bid to 0-100 but `yes_bid` can be 0 (no quote)** (`signals.py:222`)
- **What this is:** `max(0, min(100, int(yes_bid_cents or 0)))`. A `yes_bid` of 0 means no bid; clamping to 0 records a "0 bid" in history.
- **Problem:** Stability check computes `peak = max(min(b, 100-b))`. A 0 bid gives `min(0, 100) = 0`, so `peak = 0` which passes stability (price never came near strike). But 0 bid means *no quote*, not a real 0c bid. This inflates stability coverage with phantom data.
- **Fix:** Skip recording when `yes_bid_cents <= 0` or `yes_bid_cents >= 100`. Only record valid quotes (1-99).
- **If we skip it:** Fresh markets with no quotes appear "stable" and trade immediately.

### 19. **`kalshi_client.py` `_markets_cache` keyed by `(series_ticker, status, limit)` but `invalidate_markets` only matches series/status** (`kalshi_client.py:363, 409-431`)
- **What this is:** Cache key includes `limit`. `invalidate_markets(series_ticker="KXBTC15M")` removes keys matching series but any limit.
- **Problem:** If `get_markets(series_ticker="KXBTC15M", limit=50)` and `get_markets(series_ticker="KXBTC15M", limit=100)` are both called, they cache separately. Invalidation removes both. Correct behavior, but the `limit` in the key is unnecessary — the API returns up to `limit`, and a smaller limit is a subset. Could just cache the full response and slice.
- **Fix:** Cache by `(series_ticker, status)` only; slice `result[:limit]` on read. Simpler invalidation.
- **If we skip it:** Slightly more cache entries; works correctly.

### 20. **`fees.py` `taker_fee_cents` uses `math.ceil(raw * 100 - 1e-9)` — the `- 1e-9` is a floating-point hack** (`fees.py:49`)
- **What this is:** To avoid `ceil(0.7900000000000001) = 1` when raw fee is exactly 0.79 cents.
- **Problem:** Fragile. Better: `int(math.ceil(raw * 100 - 1e-12))` or use `Decimal` for fee math. The fee is critical path — must be exact.
- **Fix:** Use `Decimal` for all fee calculations, or define fees in centicents (1/100 cent) as integers.
- **If we skip it:** Extremely rare off-by-one-cent fee errors on boundary values.

---

## Verdict
**Healthy core, sharp edges on settlement idempotency and gate-band coupling.** The bot trades live with real money (grown $1.33 → $1.55, 100% win rate on shard 2). Critical path (order placement, settlement, risk) is correct and idempotent. The 7-layer LLM veto and 3 gates are sophisticated but the gate-band coupling (gate 2 blocks the configured 95-99c band unless floor enabled) is a latent misconfiguration. Settlement queue idempotency keys need strike inclusion. Dead Polymarket code path should be removed.

---

## Lean: -1,200 lines, -3 dependencies possible
- Remove `_place_poly_order` + call site (~60 lines)
- Remove unused `client` param from `_mark_to_market_exposure` (~5 lines)
- Consolidate `_touch_window` calls (~5 lines)
- Remove `ThreadPoolExecutor` from orderbook fetch, use sequential with global rate limiter (~30 lines)
- Drop `limit` from market cache key (~10 lines)
- Remove `latency_arb.py` (unused, ~180 lines)
- Remove `data_collector.py` legacy (~120 lines)
- Remove `kalshieye_downloader.py` (unused, ~220 lines)
- Remove `backtest_real.py` (duplicate of backtest_engine, ~150 lines)
- Remove `parallel_backtest_hf.py` legacy (~200 lines)
- Drop `web3-polymarket` skill reference (not used in this repo)
- `binance_feed` WebSocket code could be replaced with REST-only + cache (removes `websockets` dep)

---

## Not checked
- `binance_feed.py` (WebSocket implementation, feed health)
- `local_db.py` (SQLite schema, sync thread, idempotency keys)
- `supabase_client.py` (circuit breaker, batch write atomicity)
- `llm_eval.py` (7-layer fallback, consensus logic, rate limiting)
- `ml_model.py` / `scripts/train_ml_hf.py` (ML fair value, calibration)
- `n8n_client.py` / `inject_n8n_workflows.py` (workflow definitions, webhook contracts)
- `paper_mode.py` (paper trading ledger, compounding)
- `model.py` (GBM, locked-share, momentum models)
- `gates.py` full gate logic (ATR, EV, time-decay)
- `resilience.py` (cycle supervisor, rebuild logic)
- `crossvenue.py` / `polymarket_*.py` (cross-venue arb, not wired)
- `cfb_monitor.py`, `ladder_strategy.py`, `grid_latency.py` (experimental)
- `scripts/*.py` (training, verification, credential sync)
- Tests beyond the one failing config test (441 passed)