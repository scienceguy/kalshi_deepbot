# Ponytail Audit — kalshi_superbot

## What this repo does

Single-operator live-trading bot for Kalshi 15-minute binary crypto markets (`trader.py`). It runs a two-sided favourite-maker limit-order strategy (YES/NO in the ~82–93c band), evaluates signals through an LLM with 7-layer fallback (`llm_eval.py`), writes signals/orders/bot-state to Supabase (`supabase_client.py`), and routes settlement P&L through n8n webhooks (`n8n_client.py`). Supporting tooling: market discovery (`market_finder.py`), signed Kalshi REST client (`kalshi_client.py`), fee math (`fees.py`), risk (`risk.py`), cycle supervisor (`resilience.py`), latency arb (`latency_arb.py`), backtesters (`backtest.py`, `backtest_engine.py`, `parallel_backtest.py`, `parallel_backtest_hf.py`), data collectors (`data_collector.py`, `kalshieye_downloader.py`, `kalshi_price_data_client.py`), and n8n workflow/credential injectors.

**Assumed load:** one person, one process at a time; dry-run by default (`DRY_RUN=true`); a single Android/termux host; ~15s loop interval; ~$10 starting dry balance; 9 target crypto series; settlement P&L reconciled from Supabase on startup. Background jobs: none (single foreground loop); n8n and Supabase are external services.

---

## Must fix

### 1. `signals.py:437` — `_evaluate_hedge()` called but never defined
**What this is:** `evaluate()` branches to `_evaluate_hedge(market, max_dollars)` when `market.bracket_role == "HEDGE_NO"`.
**Problem:** `_evaluate_hedge` does not exist anywhere in `signals.py` (or the repo). Any market with `bracket_role == "HEDGE_NO"` raises `NameError` and crashes the cycle. `market_finder.py` tags cheap-yes markets as `HEDGE_NO`, so enabling hedges crashes the bot.
**Fix:** Implement `_evaluate_hedge` (mirroring the two-sided maker logic for the NO side), or remove the `HEDGE_NO` branch and treat those markets as `MAIN`. Add a smoke test that calls `evaluate()` on a `HEDGE_NO` market.
**If we skip it:** The bot crashes on the first hedge market whenever `MAX_HEDGE_PER_SERIES > 0`; dead code path today because the config default is `0`, but a live-tuning env change turns it into an outage.

### 2. `trader.py:100,172,181,271,581,663` — `resting_value` never populated; no intra-cycle cash tracking
**What this is:** `BotState.resting_value` (order_id -> reserved dollars) is initialized to `{}` and only ever `.pop`ped (`_record_filled_order`, line 271); it is never assigned a value.
**Problem:** `pending = sum(self.resting_value.values())` is always `0`, so the CASH BLOCK / TRADABLE BLOCK checks never account for resting orders. Worse, `cash` is never decremented after a placement within a cycle, so the bot can place multiple orders in one cycle each sized against the full balance — effectively double-spending cash in dry run and risking rejected/insufficient-balance orders in live mode.
**Fix:** On order placement record `state.resting_value[oid] = sig.dollar_risk`; decrement a running cash cursor (or pass `current_exposure` plus placed orders into `_pre_trade_check`) so subsequent placements in the same cycle see reduced available cash.
**If we skip it:** Live-mode orders can exceed available cash (Kalshi rejects them, wasting cycles/fees); dry-run state diverges from real balance.

### 3. `trader.py:374-391` + `trader.py:486-505` — Settlement replay can double-count PnL
**What this is:** `_replay_settlement_queue()` replays queued settlements at startup and adds `pnl_dollars` to `state.session_pnl`; `cycle()` also adds `realized_pnl` after a successful n8n webhook.
**Problem:** If the process crashes after the n8n webhook succeeds but before `_save_settlement_queue(remaining)` removes the entry, the settlement stays in the queue and is replayed on the next restart → `session_pnl` credited twice for the same settlement. `order_id` is not used idempotently.
**Fix:** Remove the queue entry (or mark it applied) before crediting PnL, and dedupe replayed settlements by `order_id` against already-booked ones.
**If we skip it:** Reported session PnL drifts upward over restarts; PnL reconciliation with Supabase becomes unreliable.

### 4. `kalshi_client.py:63` — `_verify_key` marks itself verified before the check succeeds
**What this is:** `_verify_key()` sets `self._verified = True` *before* calling `get_markets()`, "to prevent recursion."
**Problem:** A transient startup network failure (or Kalshi 429) raises inside `get_markets()`, but `_verified` is already `True`. `_ensure_verified()` then never re-checks, so all subsequent requests proceed with an unverified/wrong key and fail with opaque 401s.
**Fix:** Set `self._verified = True` only after `get_markets()` succeeds; on failure leave it `False` so the next request retries verification.
**If we skip it:** A brief network blip at startup permanently poisons the client; the bot appears "connected" but every trade/auth call fails.

### 5. `trader.py:667-680` — Orders recorded as filled before fill status is confirmed
**What this is:** `_place_order()` returns the Kalshi response; `cycle()` immediately calls `state._record_filled_order()` which books `position_risk[ticker] = sig.dollar_risk`, inserts the order into Supabase, and increments `trades_placed`.
**Problem:** A live limit order may be resting (unfilled) or rejected, yet it's booked as a filled position. `fill_count` defaults to `1` even in dry run. Position state diverges from the exchange.
**Fix:** Only call `_record_filled_order()` after checking `resp` indicates a fill (e.g. `filledCount > 0` / status `filled`); otherwise leave the order in a resting-orders set and reconcile via `get_orders()`.
**If we skip it:** Phantom positions and wrong `position_risk`/PnL; the bot thinks it holds contracts it doesn't.

### 6. `trader.py:568-572,636-646` — `state.positions` not seeded from the exchange on (re)start
**What this is:** `state.positions` only records orders placed in the current session. `cycle()` reads open positions from the Kalshi API (`client.get_positions()`) for `open_count`, but `state.positions` stays empty on restart.
**Problem:** `ticker in state.positions` (line 593) never matches positions carried over from a prior session, so the bot can re-enter markets it already has open positions in, and can exceed intended position exposure.
**Fix:** Initialize `state.positions` / `state.position_risk` from `client.get_positions()` at startup (and reconcile each cycle against the API list).
**If we skip it:** Duplicate entries on the same ticker after a restart; position-limit enforcement is ineffective for pre-existing positions.

### 7. `backtest_engine.py` is not tracked in git but imported by `backtest.py` / `parallel_backtest.py`
**What this is:** `git show HEAD:backtest_engine.py` → not present; `git status` shows `?? backtest_engine.py` (untracked). `backtest.py:26` and `parallel_backtest.py:27` do `from backtest_engine import run_sweep`.
**Problem:** A fresh `git clone` / checkout lacks `backtest_engine.py`, so both backtest entry points crash with `ModuleNotFoundError`. The committed code is broken for backtesting.
**Fix:** `git add backtest_engine.py` and commit it; consider CI that runs `python -m py_compile` on all entry points.
**If we skip it:** `backtest.py` and `parallel_backtest.py` are unusable from a clean checkout; parameter tuning workflow is broken.

### 8. `llm_eval.py:355-380` — LLM evaluation can exceed the cycle timeout and blocks all trades when no keys are configured
**What this is:** `evaluate_trade()` walks up to 7 layers, each retrying `MAX_RETRIES_PER_LAYER=2` times with `REQUEST_TIMEOUT=8s`. `should_block_trade()` returns `block=True` when all layers fail (fail-safe).
**Problem:** Worst case ~7 layers × 3 attempts × 8s ≈ 168s per signal, far beyond `CYCLE_TIMEOUT_SECONDS=30` → `SIGALRM` kills the cycle mid-evaluation. With no LLM API keys configured (the default), every layer with an `env_key` returns `None` immediately and local layers (dsh/ollama) are usually down → all trades blocked if `LLM_EVALUATION_ENABLED=true`.
**Fix:** Cap total evaluation time (e.g. a per-call deadline) and add `LLM_EVAL_FAIL_OPEN` to allow trading when LLM is unavailable; align layer timeout with cycle timeout.
**If we skip it:** Cycles frequently time out when LLM is on; enabling LLM evaluation with no keys disables trading entirely.

### 9. `n8n_client.py:180` — `execute_webhook` timeout (60s) exceeds the cycle timeout (30s)
**What this is:** `execute_webhook()` defaults `timeout=60`; `trader.py` calls it for settlement recording.
**Problem:** `CYCLE_TIMEOUT_SECONDS=30` raises `SIGALRM` after 30s, interrupting an in-flight webhook request → `requests` raises → settlement recording fails → settlement is queued (and subject to finding #3).
**Fix:** Set the webhook timeout to `min(timeout, config.CYCLE_TIMEOUT_SECONDS - small_margin)` or run settlement recording outside the alarm-guarded cycle.
**If we skip it:** Settlement writes fail intermittently; the settlement queue grows and PnL recording lags.

### 10. `kalshi_client.py:207-211` — `get_balance()` assumes balance is in cents with no verification
**What this is:** `get_balance()` returns `data.get("balance", 0) / 100.0`, treating the API value as cents.
**Problem:** If Kalshi ever returns dollars (or the field semantics change), cash is 100× off — the bot either refuses to trade (thinks $0.10) or mis-sizes against a $1000 balance. No test or startup check verifies units.
**Fix:** Document the assumed unit and, ideally, sanity-check against a known value at startup; add a test pinning the cents conversion.
**If we skip it:** Wrong cash → no trades, oversized trades, or incorrect risk floors.

### 11. `market_finder.py:185-193` — Unbounded thread pool shares a non-thread-safe KalshiClient
**What this is:** `ThreadPoolExecutor(max_workers=len(fetch_list))` fires one orderbook fetch per candidate market concurrently on a single shared `KalshiClient`.
**Problem:** `KalshiClient._request` reads/updates `self._last_req` without a lock, so the per-request minimum-gap rate limit is not atomic across threads → burst requests → Kalshi 429s (retried, but wasteful and can hit the cycle timeout).
**Fix:** Cap workers (e.g. `min(len(fetch_list), 4)`) or serialize orderbook fetches through a lock / single thread.
**If we skip it:** Intermittent 429s under load; slower market discovery.

### 12. `.env.example` + live-trading footgun (`config.py:306`, `.env.example:10`)
**What this is:** `.env.example` sets `KALSHI_CONFIRM_LIVE=true` by default, and `.env` in the working tree contains real `KALSHI_KEY_ID` and `SUPABASE_URL` (both gitignored).
**Problem:** Copying `.env.example` → `.env` and running `python trader.py --live` passes the live confirmation automatically, bypassing the intent gate. `validate_dry_run()` only blocks `--live` when `KALSHI_CONFIRM_LIVE != "true"`.
**Fix:** Default `KALSHI_CONFIRM_LIVE=false` in `.env.example`; add a reminder not to commit `.env`/`*.pem`.
**If we skip it:** Accidental live trading with real funds when onboarding a new operator.

---

## Should fix

### 13. `config.py:173-180,227-232,119-123` — Dead / unused config
**What this is:** `STOP_LOSS_ENABLED/PCT`, `PROFIT_EXIT_*/TARGET`, `LLM_HOLD_*/LLM_EVAL_MIN_HOLD_CONFIDENCE`, `HEDGE_BALANCE_PCT`, `MAX_HEDGE_PER_SERIES` (default 0) are defined but never read by `trader.py`/`signals.py`.
**Problem:** The README promises stop-loss/profit-exit scanning and hedge brackets, but the cycle never executes them — config is misleading and creates an expectation of protection that doesn't exist.
**Fix:** Implement stop-loss / profit-exit scanning in `cycle()` (or remove the config), and wire `MAX_HEDGE_PER_SERIES` > 0 into `market_finder` (currently `0` fetches no hedges).
**If we skip it:** Positions are held to expiry only; operators believe stop-loss protection is active when it is not.

### 14. `trader.py` — No stale-order management (`STALE_ORDER_MINS` unused; `get_orders` never called)
**What this is:** `STALE_ORDER_MINS` is defined but never used; `trader.py` never calls `client.get_orders()` to reconcile resting orders.
**Problem:** Filled or cancelled resting orders are never cleaned from local belief; unfilled limit orders accumulate and are never cancelled or repriced. `resting_value` stays empty (finding #2), so the bot has no picture of open orders.
**Fix:** Each cycle, fetch resting orders via `get_orders()`, sync them into `state.resting_value`, cancel stale ones older than `STALE_ORDER_MINS`.
**If we skip it:** Stale resting orders persist; capital is unintentionally tied up; cash calculations are wrong.

### 15. `config.py:193` `LOOP_INTERVAL_SECONDS = max(5, ...)` — 5s minimum cycle with many sequential API calls
**What this is:** Loop minimum interval is 5s; each cycle calls balance, positions, markets (9 series), orderbooks (concurrent), settlement `get_market`, plus up to 4 Supabase writes and possibly an LLM call.
**Problem:** A full cycle can exceed 5s (orderbook fetches + webhook + LLM). Back-to-back cycles with no drift correction; Kalshi rate limit ~10 req/s is easy to breach when orderbooks fire in parallel (finding #11).
**Fix:** Measure cycle duration and sleep `max(0, interval - elapsed)`; add a request-rate limiter shared across threads.
**If we skip it:** Cycles overlap or hammer the Kalshi API; 429s and missed loops.

### 16. `backtest_engine.py:98-107` / `parallel_backtest_hf.py:77-88` — Contradictory favorite-maker entry logic
**What this is:** `simulate_trade()` treats `mid_proxy >= ENTRY_BAND_MIN` as "favorite," but when `mid_proxy > ENTRY_BAND_MAX` it sets `ep = 100 - mid_proxy` and bets the NO side (the underdog).
**Problem:** This contradicts the "favourite maker" strategy (buy the favorite); it effectively buys the underdog when YES is above the band. `parallel_backtest_hf.py` also uses `p_last` (last price before settlement) as the entry proxy — strong lookahead bias.
**Fix:** Restrict entries to the favorite side only; document the lookahead bias of `p_last` or use an earlier price.
**If we skip it:** Backtest EV/win-rate is inflated and not representative of live behavior.

### 17. `parallel_backtest_hf.py:201,210` — Aggressive backtest grid (PER_TRADE_PCT up to 0.80)
**What this is:** The non-quick grid tests `PER_TRADE_PCT` up to `0.80` and `BALANCE_PCT_PER_TRADE` up to `0.80` against a $10 starting balance.
**Problem:** That's up to $8 per trade on a $10 account in the backtest "optimal" parameters — bankroll-ruining sizing that would never pass live risk limits (`PER_TRADE_PCT=0.20` default).
**Fix:** Cap the backtest grid at live-sane values (≤ `config.PER_TRADE_PCT`); separate "search" grid from "deployable" grid.
**If we skip it:** Optimized backtest parameters are unsafe to deploy; risk config and backtest config diverge.

### 18. `tests/` — Core trading paths untested
**What this is:** 61 tests pass, but there are no tests for `trader.py` cycle logic (`_pre_trade_check`, settlement, order placement), `backtest_engine`, `latency_arb`, `data_collector`, or `resilience`.
**Problem:** The most financially consequential code (the live loop) has zero coverage; regressions in sizing/risk/settlement go undetected.
**Fix:** Add tests for `_pre_trade_check` caps, settlement PnL booking, and `resting_value` behavior; mock `KalshiClient`/`n8n_client`.
**If we skip it:** Bugs in the trading loop (findings #2, #3, #5) can reach production unseen.

### 19. `tests/test_*.py` — Weak test isolation (config mutation leaks)
**What this is:** Tests `monkeypatch.setattr(config, ...)` module-level globals (e.g. `test_market_finder.py:148` sets `MAX_HEDGE_PER_SERIES=0`) without restoring them.
**Problem:** Mutated config persists across tests in the same process, making test outcomes order-dependent and hiding cross-test contamination.
**Fix:** Use fixtures that restore config values after each test, or copy config into a namespace object under test.
**If we skip it:** Flaky tests; false confidence when config-dependent logic changes.

### 20. `llm_eval.py:391` — Fail-safe blocks all trades when LLM unavailable
**What this is:** `should_block_trade()` returns `(True, None)` when `evaluate_trade()` returns `None` (all layers failed).
**Problem:** With no LLM keys (default) and no local gateway, enabling `LLM_EVALUATION_ENABLED` blocks 100% of signals — no trading at all, with no operator alert that LLM is the cause.
**Fix:** Add `LLM_EVAL_FAIL_OPEN` (default False) and log a prominent warning when all layers fail.
**If we skip it:** Silently zero-trades when LLM is misconfigured.

### 21. `signals.py:364-368` — "Two-sided" emits at most one side per market
**What this is:** `_evaluate_two_sided()` comment says it returns 0–2 signals per market, but when both YES and NO are in band it keeps only the more extreme side (lines 364-368).
**Problem:** The two-sided maker strategy never actually places both sides of the same market; the docstring/comment overstates capability.
**Fix:** Update the comment to reflect single-side-per-market, or implement genuine two-sided entries with separate budget tracking.
**If we skip it:** Minor; behavior is conservative (one side), but documentation misleads.

### 22. `supabase_client.py:228-230` — Circuit breaker never trips on persistent 4xx
**What this is:** `_request()` returns `None` on 400/403/404 without calling `_supabase_record_failure()`.
**Problem:** A permanent schema/auth problem (e.g. missing `orders` table) returns 400 every cycle, never trips the circuit, and keeps retrying 3× per call with 1s backoff.
**Fix:** Record a failure for non-retryable 4xx too, or distinguish schema errors to fail fast.
**If we skip it:** Wasted API calls and logs on permanent Supabase misconfiguration.

### 23. `n8n_client.py:37,56` + `inject_n8n_workflows.py` — Hardcoded termux paths and project IDs
**What this is:** `n8n_client` reads `/data/data/com.termux/files/home/.n8n/...`; `inject_n8n_workflows.py` hardcodes `PROJECT_ID`, `USER_ID`, and `/data/data/com.termux/files/usr/bin/python3`.
**Problem:** Non-portable; direct SQLite writes to n8n's DB bypass the n8n API and break on schema drift; credential injector (`add_n8n_credentials.py:16-21`) reads config at import time and crashes outside the repo.
**Fix:** Parameterize paths/IDs via env; prefer the n8n API over direct DB writes; guard the credential injector's import-time file read.
**If we skip it:** Tooling only runs on this host; n8n DB-schema changes break workflow injection silently.

### 24. `data_collector.py:32` — Docstring/behavior contradiction on auth
**What this is:** `MarketSnapshotCollector.__init__()` constructs `KalshiClient()` (requires Kalshi auth, verifies on first use), but the module docstring claims collection works "without needing an external API key."
**Problem:** Operators expecting key-free data collection get an auth error; the historical tier actually needs Kalshi credentials.
**Fix:** Correct the docstring; make the client optional/injectable so collection can run on cached public data.
**If we skip it:** Confusing onboarding; data collection fails without clear cause.

### 25. `kalshi_client.py:174-188` + `trader.py:764-788` — Sequential per-cycle calls can blow the 30s cycle timeout
**What this is:** A cycle does balance, positions, markets (9 series with retries), orderbooks, settlement `get_market`, Supabase writes, and optional LLM — each request up to `REQUEST_TIMEOUT_SECONDS=10`.
**Problem:** Several sequential 10s timeouts plus LLM/webhook easily exceed `CYCLE_TIMEOUT_SECONDS=30`; `SIGALRM` aborts the cycle, `resilience` backs off and rebuilds the client after 3 errors → client churn on transient slowness.
**Fix:** Tighten per-request timeouts for the loop, parallelize independent fetches, and only rebuild the client on auth errors (not timeouts).
**If we skip it:** Frequent cycle timeouts; unnecessary client rebuilds; slower trading.

### 26. `supabase_client.py:143-148` — Reads parent-dir `.env` as fallback depending on CWD
**What this is:** If credentials missing, `supabase_client` parses `Path(__file__).parent.parent / ".env"` (the repo root `.env`).
**Problem:** Running from another directory or a copied repo reads a stale/wrong `.env`, silently using the wrong Supabase project.
**Fix:** Remove the implicit parent-`.env` fallback; require explicit env/key-file.
**If we skip it:** Writes land in the wrong Supabase project depending on working directory.

---

## Nice to have

### 27. `trader.py:102` vs `_record_filled_order` (line 264) — Inconsistent `resting_value` usage/comment
**What this is:** Comment says `resting_value: dict = {}  # order_id -> reserved dollars`, but `_record_filled_order` stores position info (side/entry/contracts/order_id) into `state.positions` and only pops `resting_value`.
**Problem:** The comment describes a structure that is never maintained; readers are misled about how resting cash is tracked.
**Fix:** Align the comment/code: either maintain `resting_value[oid] = dollar_risk` on placement or remove it.
**If we skip it:** Cosmetic; contributes to finding #2 confusion.

### 28. `backtest_engine.py:342` — Multiprocessing pickles the full markets list into every worker
**What this is:** `run_sweep()` uses `Pool(processes=jobs, initializer=init_worker, initargs=(markets,))`, so every worker process gets a copy of all markets.
**Problem:** Memory use scales as `jobs × markets_size`; with large historical datasets this can OOM the small termux host.
**Fix:** Have workers read a shared parquet/cache path instead of pickling markets; or cap `jobs`.
**If we skip it:** Backtests on large datasets may OOM or slow drastically.

### 29. `parallel_backtest.py:55` — Public fetch uses `limit=1000` with no cursor pagination
**What this is:** `fetch_settled_markets_public()` requests `limit=1000` from `/markets` once per series.
**Problem:** Kalshi paginates; markets beyond the first page are silently excluded from the backtest universe.
**Fix:** Follow the cursor until exhausted (like `fetch_all_historical_markets`).
**If we skip it:** Backtests run on an incomplete market sample; EV results biased.

### 30. `requirements.txt` — Missing/unpinned transitive data deps
**What this is:** `pandas` and `pyarrow` are unpinned; `kalshi-price-data` (used by `kalshi_price_data_client.py`) is not listed at all.
**Problem:** `pip install -r requirements.txt` can pull breaking pandas/pyarrow versions; importing `kalshi_price_data_client` then fails with `ImportError` at runtime.
**Fix:** Pin `pandas`/`pyarrow`; add `kalshi-price-data` as optional or document `pip install kalshi-price-data`.
**If we skip it:** `parallel_backtest_hf.py` and `kalshi_price_data_client.py` break on fresh installs.

### 31. `add_n8n_credentials.py:16-21` — Import-time config read + placeholder secrets in source
**What this is:** The script reads `/data/data/com.termux/files/home/.n8n/config` at module import and contains placeholder credential values.
**Problem:** Importing it outside the repo crashes; placeholder secrets are committed to source.
**Fix:** Move file reads into `main()`; remove placeholder values or mark them clearly as do-not-commit.
**If we skip it:** Script is fragile and leaks placeholder secret structure.

### 32. `inject_n8n_workflows.py` — Direct n8n SQLite writes bypass the API
**What this is:** Workflows are inserted by writing n8n's SQLite tables directly.
**Problem:** n8n schema changes break the injection; no validation that n8n is running or the DB is unlocked; no idempotency beyond name-prefix delete.
**Fix:** Use the n8n API (`n8n_client.create_workflow`) instead of raw SQL where possible.
**If we skip it:** Workflow injection is brittle across n8n versions.

### 33. `fees.py` — Hardcoded 0.07 fee multiplier with no schedule-version guard
**What this is:** `taker_fee_dollars()` hardcodes Kalshi's 0.07 fee rate and the cent grid.
**Problem:** If Kalshi changes its fee schedule, all PnL/backtest/sizing math is silently wrong; no test asserts against a known fee schedule version.
**Fix:** Make the fee rate configurable (`FEE_RATE`) and add a regression test pinning today's known fee example.
**If we skip it:** Profitability estimates and order sizing become wrong after a fee change.

### 34. `.env` / `kalshi_private.pem` present locally (gitignored) — No secret-scanning guard
**What this is:** The working tree `.env` holds a real `KALSHI_KEY_ID` and `SUPABASE_URL`; `kalshi_private.pem` holds a real key. Both are gitignored but present on disk.
**Problem:** `git add -A` or a careless commit could leak live trading credentials and a Supabase service key.
**Fix:** Add a pre-commit/secret-scan reminder (or a CI check) blocking `.env`/`*.pem`; consider `git-secrets`/`gitleaks`.
**If we skip it:** Credential leak risk if secrets are ever committed.

### 35. `trader.py:711` — Startup initializes Supabase client and logs its enabled state
**What this is:** `main()` calls `get_supabase_client().enabled` at startup (line 711), instantiating `SupabaseClient`.
**Problem:** If Supabase credentials are malformed, the client logs a warning at import/module load; not harmful but couples startup logging to Supabase availability.
**Fix:** Minor; make startup Supabase check explicit and non-fatal (already is).
**If we skip it:** Cosmetic only.

### 36. `market_finder.py:124-129` — Sequential per-series fetches with retries
**What this is:** `find_active_markets()` loops 9 series, each with up to 3 retries and 0.5–2s backoffs, before the concurrent orderbook stage.
**Problem:** Market discovery can take several seconds even when healthy, eating into the 15s loop and the 30s cycle timeout.
**Fix:** Fetch markets for all series concurrently (bounded pool), then fetch orderbooks concurrently.
**If we skip it:** Slower cycle startup; less time for trading within each window.

### 37. `signals.py:130` — Module-level `_bid_hist` global with file persistence
**What this is:** Bid history lives in a module global and `bid_hist.json`, capped at 1000 entries/ticker and 200 tickers.
**Problem:** History persists across restarts and isn't cleared between dry-run and live modes; stale bids can influence stability checks after a long pause.
**Fix:** Scope bid history to the session or version it by mode; cap is already present.
**If we skip it:** Minor; stability check may use outdated history after restarts.

### 38. `trader.py:543-545` — Four sequential Supabase `set_bot_state` writes per cycle
**What this is:** Each cycle writes `cycle`, `cash`, `exposure`, `session_pnl` as separate Supabase calls.
**Problem:** 4 network writes per cycle (every ~15s) wastes rate quota and adds latency; none are critical in-path.
**Fix:** Batch bot-state into a single write every N cycles or on change.
**If we skip it:** Unnecessary Supabase traffic and slight cycle latency.

### 39. `latency_arb.py:176` — `run(client, markets)` accepts unused `client`
**What this is:** `run()` takes a `client` argument but never uses it; `__main__` builds its own client and market list.
**Problem:** Dead parameter; confusing API; the standalone path duplicates `TARGET_SERIES` fetching logic.
**Fix:** Remove the unused parameter or have `run()` use the passed client.
**If we skip it:** Cosmetic; unused argument.

### 40. `backtest.py:34` vs engine default — Inconsistent settlement semantics
**What this is:** `backtest.py.settlement_yes_won` returns `exp_val*100 >= 100` (i.e. `exp_val == 1.0`); the engine default is `exp_val >= floor_strike`.
**Problem:** For binary markets they coincide, but the two implementations diverge semantically if `floor_strike != 1`, producing different backtest results for the same data.
**Fix:** Unify to one settlement predicate.
**If we skip it:** Backtest results depend on which entry point is used.

### 41. `config.py` — Custom `.env` loader lacks `export`/inline-comment/multiline support
**What this is:** `_load_env()` parses simple `KEY=VALUE` lines only.
**Problem:** An `.env` with `export KALSHI_KEY_ID=...`, `KEY=val # comment`, or quoted multiline values is parsed incorrectly or breaks.
**Fix:** Document the strict format or adopt `python-dotenv` (adds one dependency).
**If we skip it:** Operators hit cryptic "credentials missing" errors on reasonably formatted `.env` files.

### 42. `trader.py` — No distinction of auth errors for client rebuild
**What this is:** The cycle loop rebuilds `KalshiClient()` after `CLIENT_REBUILD_THRESHOLD` errors of any type (line 777).
**Problem:** 401 auth errors (bad key, expired key) are not distinguished from transient network errors; rebuilding the client with the same bad key just repeats the failure.
**Fix:** Use `resilience.is_auth_error()` to skip rebuilds on auth errors and alert instead.
**If we skip it:** Auth failures trigger futile client-rebuild loops.

### 43. `kalshi_client.py:46` — Signature path derived from `KALSHI_BASE_URL` path
**What this is:** `_api_path = urlparse(base_url).path` is prepended to every signed path.
**Problem:** If `KALSHI_BASE_URL` is changed to a URL without the `/trade-api/v2` prefix, all signatures become invalid with no warning.
**Fix:** Validate `KALSHI_BASE_URL` contains the expected API root at startup.
**If we skip it:** Misconfigured base URL → all signed requests 401.

### 44. `resilience.py:42-45` — Backoff rebuild loop on persistent non-auth errors
**What this is:** `mark_failure()` caps delay at `max_backoff` (60s); `should_rebuild_client()` triggers after `rebuild_threshold` (3) errors; `after_rebuild()` resets errors.
**Problem:** If the rebuilt client keeps failing (e.g. persistent outage), the loop rebuilds every cycle at 60s intervals indefinitely — no circuit breaker or max-rebuild cap.
**Fix:** Add a maximum number of rebuilds or a longer cooldown after repeated rebuild failures.
**If we skip it:** Long outages cause perpetual rebuild attempts and log spam.

### 45. `parallel_backtest_hf.py:37-43` — `StratParams` lacks some fields the engine has
**What this is:** The HF backtest `StratParams` has fewer fields than `backtest_engine.StratParams` (no `MIN_SKEW_CENTS`, `ENTRY_WINDOW_*`, `AGGRESSIVE_FILL`, `MAX_OPEN_POSITIONS`).
**Problem:** Two parameter grids for the same strategy diverge; best HF parameters can't be ported to the live config cleanly.
**Fix:** Share one `StratParams` between engines.
**If we skip it:** Parameter drift between backtest variants and live config.

### 46. `data_collector.py:188` — Suggests a `--data-file` flag that doesn't exist
**What this is:** The help prints `python backtest.py --data-file {out_path}`, but `backtest.py` has no `--data-file` argument.
**Problem:** Users following the printed instruction get an argument error.
**Fix:** Remove the misleading hint or implement `--data-file` in `backtest.py`.
**If we skip it:** Confusing CLI guidance.

### 47. `fees.py:45-56` — `net_profit_roundtrip` uses `taker_fee_cents` (balance-grid rounded) for both legs
**What this is:** Round-trip profit uses `taker_fee_cents` which rounds up to the cent grid per leg.
**Problem:** For sub-cent fees this overstates fees vs the raw 6dp model; minor for backtest direction but inconsistent with `taker_fee_dollars`.
**Fix:** Document the convention or use the raw model consistently.
**If we skip it:** Minor PnL rounding differences in backtests only.

### 48. `market_finder.py:159` — `mid` uses `round()` on possibly-zero prices
**What this is:** `mid = round((yb + ya) / 2) if (yb and ya) else (ya or yb or 0)`.
**Problem:** If only one side is quoted, `mid` can be 0 and the market is filtered out (`MIN_P <= mid` fails), dropping legitimate one-sided markets.
**Fix:** Treat one-sided quotes conservatively rather than as `mid=0`.
**If we skip it:** Some tradeable markets with one-sided books are skipped.

---

## Verdict

The repo is a real, working single-operator trading bot with a solid core (signed Kalshi client, exact fee math, circuit breakers, dry-run default, lock file, 61 passing tests). It is NOT yet safe to run in live mode without addressing the Must-fix items above — most critically the undefined `_evaluate_hedge`, the non-tracked `resting_value`/intra-cycle cash (overspend risk), settlement replay double-counting, the pre-verified `_verify_key`, orders booked as filled before confirmation, positions not seeded from the exchange, and `backtest_engine.py` not being tracked in git (fresh checkouts break backtesting).

The load assumption is one person, one process, dry-run by default on a single termux host — within that scope the architecture is appropriate. The biggest risks are financial (cash overspend, phantom positions, PnL double-count) and operational (crashes on config changes, broken fresh-checkout).

**Lean: -0 lines, -0 dependencies possible.** The audit does not recommend removing code; the fixes are additive (implement `_evaluate_hedge`, populate `resting_value`, seed positions, dedupe settlement replay, track `_verified` correctly) plus committing the untracked `backtest_engine.py`. No new dependencies are required; `kalshi-price-data` and `pandas/pyarrow` are optional and already import-guarded.

**Not checked:** live Kalshi API behavior (no network credentials used during the audit), actual n8n/Supabase service availability, the `kalshieye_downloader.py` external API, the `kalshi-price-data` SDK (not installed), and performance under concurrent multi-process operation (single-process assumed). The local `.env`/`kalshi_private.pem` contain real credentials but are gitignored and were not committed or inspected beyond confirming their presence.
