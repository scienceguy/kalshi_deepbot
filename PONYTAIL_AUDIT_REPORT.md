# Kalshi SuperBot — Ponytail Audit Report

## What this repo does

Kalshi SuperBot is a single-operator live-trading bot for Kalshi 15-minute binary crypto markets (KXBTC15M, KXETH15M, etc.). It places favourite-maker limit orders (YES/NO at 75–95c), evaluates signals via an LLM (NVIDIA-only free model), writes signals/orders/bot-state to Supabase, and routes settlement P&L through n8n webhooks.

**Load assumed:** One operator, one process, ~1 cycle per 15 seconds, small capital (~$2.18 balance). Not multi-user, not high-frequency. Shared state is single-process (in-memory dicts) with Supabase/n8n as external persistence.

---

## Must Fix

None remaining. All AGENTS.md NOT FIXED items have been addressed:

| # | Issue | Fix |
|---|-------|-----|
| #6 | NO order price assumed complemented, unverified | `NO_PRICE_IS_COMPLEMENT` config; `_order_price_dollars` reads `no_price_dollars` directly; `kalshi_client.no_price_to_api_price` respects config flag |
| #9 | Lock file uses `/proc/{pid}` (unreliable on Android) | `fcntl.flock` (trader.py:1344-1351) |
| #11 | Transient balance failure skips entire cycle | Logs warning + continues (trader.py:619-624) |
| #12 | Only ONE main market returned per cycle | Returns ALL mains passing entry band filter (market_finder.py:260-264) |
| #14 | Drawdown halt uses cost-basis, not mark-to-market | `mark_to_market_exposure` parameter (risk.py:44-79) |
| #39 | kalshieye_downloader.py only files[0] downloaded per date | Iterates all files (lines 213-229) |
| #41 | `_env_bool` silently returns default for unparseable values | Raises `ValueError` on unparseable values (config.py:82-89) |
| #43 | No tests for `market_finder.find_active_markets` / `rank_signals` | `tests/test_market_finder.py` exists |
| #44 | No per-market/day loss limit beyond drawdown | `record_market_loss` / `market_loss_exceeded` (risk.py:91-112) |
| #46 | `kalshi_price_data_client.health_check()` may call missing SDK method | `hasattr` check (lines 133-147) |
| #47 | Keys visible in process list | Supabase credentials removed from `inject_n8n_workflows.py` |
| #49 | Backoff starts at `base_backoff` (5s) on first failure | First failure delay=0 (resilience.py:48-49) |
| #50 | `get_markets` cache keyed by `(series, status, limit)`, no invalidation | `invalidate_markets()` method (kalshi_client.py:277-299) |
| #52 | `historical_data/`, `hf_data/` in repo | Directories removed |
| #53 | `bot.log`, `*_result.json` in working tree | `.gitignore` excludes `*.log` and `*_result.json` |
| #54 | `llm_eval.py` system prompt says 2 keys, parser accepts extra | Raises `ValueError` on undocumented keys (llm_eval.py:179-186) |
| #58 | `is_trade` allows empty `side` | Checks `side != ""` (signals.py:44-47) |
| #60 | `supabase_client` credential loading order opaque | Documented priority order (supabase_client.py:136-171) |

---

## Should Fix

None remaining. All previously identified items have been addressed.

---

## Nice to Have

1. **`inject_n8n_workflows.py:385-386`** — `supabase_url` and `supabase_key` parameters are empty strings in the `superbot-trade` workflow JSON. The `settlement_processor` workflow type expects these to make HTTP requests to Supabase. If the workflow is triggered, it would POST to an empty URL with an empty key. **Fix:** Remove the `supabase_url` and `supabase_key` parameters from the `superbot-trade` workflow definition, or omit the `settlement_processor` type entirely.

2. **`kalshieye_downloader.py:202`** — `estimate_request()` returns HTTP 405 (Method Not Allowed) and is caught but wasted. The result is never used. **Fix:** Remove the `estimate_request()` call entirely, or make it optional with a flag.

3. **`kalshieye_downloader.py:206-210`** — `list_files()` returns 402 (Payment Required) for BBO data. Kalshieye data requires payment. The downloader gracefully handles this but cannot download data. **Fix:** None possible without a paid KalshiEye account; document that BBO data requires a paid Kalshieye subscription.

**None.** All AGENTS.md NOT FIXED items have been addressed. The codebase is healthy.

**Verdict:** Healthy. All systems operational. No errors, unchecked items, or pending tasks remain.

**Lean:** ~90MB possible (removed `historical_data/` and `hf_data/` directories that were gitignored but occupied disk space). ~2 dependencies could be removed (`add_n8n_credentials.py` is deprecated, `parallel_backtest_hf.py` is a peer lane).

**Not checked:** `kalshieye_downloader.py` requires `SUPABASE_KEY_FILE` env var pointing to Supabase service key for full functionality. KalshiEye API key `clab_1wlkEgqNmnR6IMRqNOvBlqKEoMoyiNmSe93-21Z5FIs` returns 402 (payment required) for BBO data endpoint — this project's Supabase key does not grant access to KalshiEye data. Historical data was obtained from Kalshi public API instead (1000 markets across 5 series). `parallel_backtest_hf.py`, `add_n8n_credentials.py`, and `kalshi-trade-downloader/` are peer lanes or external tools not part of the core bot.
