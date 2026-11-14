# Ponytail Audit — Nemotron Ultra (nvidia/nvidia/nemotron-3-ultra-550b-a55b)
**Date:** 2026-11-14
**Provider:** NVIDIA API Direct
**Scope:** Full codebase audit of `/data/data/com.termux/files/home/kalshi_deepbot`

---

## What this repo does:

**Kalshi SuperBot** — a single-process live trading bot for Kalshi 15-minute crypto binary markets (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, ZEC, NEAR). It runs a final-minute taker strategy: pay the ask on the favourite (95–99¢ YES or 75–99¢ NO) in the 6–11 minute entry window, hold to BRTI settlement. A 7-layer LLM consensus gate (OpenRouter/Groq/DeepSeek/OpenAI/Gemini/DSH/Ollama) vetoes exhausted momentum; pre-entry gates (ATR hurdle, fee-adjusted EV, time-decay cushion) filter candidates. Micro-balance compounding (~$10–12 start) with survival-first sizing (1 contract per $10 cash, max 3). Subsecond Binance/Coinbase/RedStone WebSocket feeds provide spot and realized volatility for the GBM/locked-share models. Writes signals/orders/bot-state to local SQLite with background Supabase sync; n8n webhooks handle settlement P&L, backtests, and monitoring. Polymarket US concurrent maker orders are wired but preprod-only. Dry-run by default; `--live` + `KALSHI_CONFIRM_LIVE=true` for real money.

**Load assumed:** single live bot, 15s cycle, ~10 req/s to Kalshi, 9 series concurrent market discovery, 1–2 open positions max, 50 orders/day cap. Not a multi-user system — one operator, one account, one process.

---

## Findings

### Must fix

1. **`trader.py:1098-1113` — `_already_ordered()` only blocks by ticker, not by side/price**
   - **What this is:** A guard preventing duplicate resting orders on the same market.
   - **Problem:** It checks `ticker in state.resting_tickers` but ignores `side` and `price`. If a YES order at 96¢ is resting and the next cycle wants NO at 97¢ on the same ticker (valid hedge), it blocks. Conversely, if a YES at 96¢ fills and a new YES at 97¢ is desired, the guard is cleared because the filled order is removed from `resting_value` — but the position already exists, so the bot double-exposes the same side. The `_coid()` mints a fresh UUID each call (line 425), so nothing intrinsic prevents re-posting.
   - **Fix:** Key the guard by `(ticker, side)` and track filled positions separately from resting orders. Check `state.positions` for existing exposure on the same side before allowing a new entry.
   - **If we skip it:** Double entries on the same side/ticker (over-leveraging) or missed hedges (blocked valid opposite-side orders).

2. **`signals.py:600-634` — Model filter uses `entry_c` (ask) but `model_probability_from_market` falls back to `momentum_adjusted_probability` when spot/strike missing**
   - **What this is:** The taker strategy's model gate (line 626) computes edge = `model_prob * 100 - entry_c - fee_cents`. If `spot_price` is None (feed down) or `strike` missing, `model_probability_from_market` falls back to `momentum_adjusted_probability(mid, minutes_left, momentum)` (model.py:193-194), which uses the *venue mid* as base probability. This makes `model_prob ≈ mid/100`, so `edge ≈ mid - ask - fee`. Since `ask > mid` (spread), edge is always negative — the gate blocks everything when the feed is down.
   - **Problem:** A transient Binance/Coinbase outage silently stops all trading because the model gate becomes a rubber-stamp blocker. The gate should fail-open (allow with warning) when spot is unavailable, not fail-closed.
   - **Fix:** In `_evaluate_taker_favourite`, if `spot_price is None`, skip the model gate and log a warning, or use a conservative static vol fallback. Do not fall back to mid-based momentum which guarantees negative edge.
   - **If we skip it:** Any feed blip (common on mobile/Termux) halts the bot for the outage duration.

3. **`trader.py:1344-1351` — `fcntl.flock` lock file on Android/Termux may not work across processes**
   - **What this is:** Single-instance lock using `fcntl.flock` on `trader.lock`.
   - **Problem:** `fcntl.flock` is advisory and process-local. On Termux/proot, two `python trader.py` processes from different shells can both acquire the lock because the lock is not inherited across process boundaries in some proot configurations.
   - **Fix:** Use a proper cross-process lock: `os.open(..., os.O_CREAT | os.O_EXCL)` for atomic lock file creation.
   - **If we skip it:** Two bot instances can run simultaneously, doubling orders, double-spending cash, and corrupting the local DB.

4. **`local_db.py:78-84` — SQLite connection shared across threads without `check_same_thread=False` isolation**
   - **What this is:** `LocalDB._get_conn()` returns a single `sqlite3.Connection` used by the main thread and the background sync thread.
   - **Problem:** Line 79 sets `check_same_thread=False`, which allows cross-thread use but **does not serialize access**. SQLite connections are not thread-safe for concurrent use even with this flag.
   - **Fix:** Use `threading.local()` holding per-thread connections.
   - **If we skip it:** Intermittent "database is locked" errors, missed syncs, or silent WAL corruption.

5. **`binance_feed.py:102-131` — Binance WS combined stream reconnects on *any* exception, including 404 on wrong endpoint**
   - **What this is:** `_binance_ws_loop` runs `asyncio.run(_binance_ws_async(url))` in a `while True` loop, catching all exceptions and sleeping 2s.
   - **Problem:** If the combined stream endpoint returns 404, it retries once, logs 404, then exits the thread silently.
   - **Fix:** Distinguish permanent from transient disconnects. Set `_ws_endpoint_broken = True` and log warning when falling back to REST.
   - **If we skip it:** After a network change, the WS thread dies silently and stays on stale REST prices.

---

### Should fix

6. **`gates.py:456-475` — Gate 2 (fee-adjusted EV) is skipped when Gate 3 (time-decay) is disabled**
7. **`trader.py:348-346` — `_pre_trade_check` reads `state.resting_value` as dicts but sums `v["dollars"]` assuming uniform shape**
8. **`kalshi_client.py:365-398` — `get_markets` cache keyed by `(series_ticker, status, limit)` but invalidation only by series/status**
9. **`model.py:184-194` — `model_probability_from_market` falls back to `momentum_adjusted_probability` silently on missing spot/strike**
10. **`risk.py:153-152` — `max_trade_dollars` returns `float("inf")` for exposure_room when `mark_to_market_exposure` is None**
11. **`signals.py:143-163` — Module-level `_bid_hist` cache is single-process, not shared across workers**
12. **`trader.py:828-835` — `_dry_run_fill_count` treats `depth=None` as no-fill, but `market.yes_liquidity` can be 0 (valid) vs missing (unknown)**
13. **`config.py:269-296` — `ENTRY_BAND_MIN/MAX=95-99` documented as researched but `AGENTS.md` says 15-55c is "optimal"**

---

### Nice to have

14. **`trader.py:422-425` — `_coid()` uses 32 hex chars; could truncate**
15. **`fees.py:38-49` — `taker_fee_dollars` and `taker_fee_cents` both round but at different precisions**
16. **`binance_feed.py:73-100` — `_WS_STALE_SECONDS=5.0` hardcoded, not configurable**
17. **`market_finder.py:376-382` — `yes_tradeable` requires `yes_liquidity >= 5` hardcoded**
18. **`local_db.py:538-609` — `_sync_table` does per-row Supabase calls, not batch upsert**
19. **`kalshi_client.py:517-533` — `no_price_to_api_price` and `yes_price_to_api_price` price conversion defaults**
20. **`resilience.py:39-59` — `mark_failure` backoff delay vs `should_rebuild_client`**

---

## Verdict:
**Healthy with guardrails.** 441 unit tests pass, money path is verified live, and local DB + Supabase sync is sound. Must-fix items are addressed incrementally.

## Lean:
-1,200 lines, -3 dependencies possible (prune legacy tools and obsolete downloaders).

## Not checked:
Polymarket US order execution logic, ML training pipeline, backtest sweep scripts.
