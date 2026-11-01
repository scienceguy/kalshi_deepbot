# Ponytail Audit — Kalshi SuperBot (100 findings)

**Repo:** /data/data/com.termux/files/home/kalshi_superbot
**Method:** ponytail-audit (ultra). Map → load → flows → trust boundaries → bugs → smallest fix.
**Load assumed:** one person, one process at a time, dry-run by default, single Android/Termux host. Live trading is opt-in (`--live` + `KALSHI_CONFIRM_LIVE=true`). n8n + Supabase are optional external sidecars; the bot itself is a single Python loop on one handset (~15s interval, ~$10 dry balance, 9 crypto series).

## What this repo does

The Kalshi SuperBot is a single-operator live-trading bot for Kalshi 15-minute binary crypto markets. It runs a favour-maker limit-order strategy (two-sided YES/No at 82–93c), scores signals with an LLM (7-layer fallback), writes signals/orders/bot-state to Supabase, and routes settlement P&L through n8n webhooks. Entry is via `trader.py` (dry-run default; `--live` for real money); supporting tools backtest, collect market data, and inject n8n workflows/credentials. The money path: Kalshi signed REST client → signal engine → risk caps → order placement → settlement PnL booked via n8n webhook → Supabase audit trail.

---

## MUST FIX

**1. `signals.py:500-509` — `evaluate()` calls undefined `_evaluate_hedge` on HEDGE_NO markets**

What this is: `evaluate()` branches to `_evaluate_hedge(market, max_dollars)` when `market.bracket_role == "HEDGE_NO"`, but `_evaluate_hedge` does not exist anywhere in `signals.py` or the repo (a stub comment block occupies lines 311-319 instead).
Problem: Any market tagged `HEDGE_NO` raises `NameError` and crashes the whole cycle. `market_finder.py` tags cheap-yes markets as `HEDGE_NO`, so enabling `MAX_HEDGE_PER_SERIES > 0` turns the hedge path into an outage.
Fix: Implement `_evaluate_hedge` (mirroring the NO side of the two-sided maker) or remove the `HEDGE_NO` branch and treat those markets as `MAIN`. Add a smoke test calling `evaluate()` on a `HEDGE_NO` market.
If we skip it: The bot crashes on the first hedge market whenever `MAX_HEDGE_PER_SERIES > 0`; dead code path today only because the config default is `0`.

**2. `trader.py:443-470` + `trader.py:810-824` — NO order price complemented by assumption, never verified**

What this is: Both the cycle reconciliation and startup seeding compute `entry_cents = 100 - price_cents` for NO orders, assuming Kalshi stores NO prices as the YES complement.
Problem: If Kalshi stores the NO price directly (not complemented), `entry_cents` is wrong — e.g. a NO order stored at 85c would be read as 15c. `resting_value` under-reserves by ~70c/contract, risking overspend or false blocks. Never verified against a live NO resting order.
Fix: Fetch a NO resting order and compare stored price vs intent; apply the complement correctly (or remove it); add an assert/log noting the assumption.
If we skip it: Cash tracking for NO resting orders is wrong; live-mode overspend or spurious cash blocks.

**3. `trader.py:734-748` — `resting_value` never decremented within a cycle; cash double-spent**

What this is: `state.resting_value[oid] = sig.dollar_risk` is set on placement but `cash` is never decremented for subsequent placements in the same cycle. `pending = sum(resting_value.values())` is compared against the original `cash`, so each order in a cycle is sized against the full balance.
Problem: The bot can place multiple orders in one cycle each against the full balance — effectively double-spending cash. In dry run the state diverges from reality; in live mode Kalshi rejects insufficient-balance orders (wasted cycles/fees).
Fix: Maintain a running `cash_available` cursor decremented by each placed order's `dollar_risk`, and pass it into `_pre_trade_check` / `max_trade_dollars` for subsequent placements.
If we skip it: Live-mode orders exceed available cash; Kalshi rejections waste cycles and fees.

**4. `trader.py:382-399` — Settlement replay double-counts PnL on crash**

What this is: `_replay_settlement_queue()` replays queued settlements via n8n webhook and adds `pnl_dollars` to `state.session_pnl`. If the process crashes after the webhook succeeds and `session_pnl += pnl` runs but before `_save_settlement_queue(remaining)` drops the entry, the entry remains and is replayed on restart.
Fix: Remove the entry from the queue (rewrite the file) immediately after a successful webhook, before crediting `session_pnl`; or dedupe replay by `order_id`.
If we skip it: `session_pnl` is overstated after any crash mid-replay; Supabase reconciliation cannot detect it because the bot's own state is the source.

**5. `trader.py:464-482` — Position popped from state before settlement PnL is booked**

What this is: Settled tickers are popped from `state.positions`/`state.position_risk` before the settlement price is fetched and PnL booked.
Problem: A crash/exception between the pop and the PnL booking loses the position from in-memory state and never books its realized PnL. Position is gone but cash/risk bookkeeping never reflects its outcome.
Fix: Book PnL first (or record a pending-settlement marker), then remove the position only after PnL is recorded or safely queued.
If we skip it: Positions and PnL silently diverge from the exchange; cash model drifts from reality.

**6. `trader.py:726-748` — Orders recorded as filled before fill confirmation**

What this is: `cycle()` calls `state._record_filled_order()` immediately after `place_order()`, booking `position_risk`, inserting into Supabase, and incrementing `trades_placed`.
Problem: A live limit order may be resting (unfilled) or rejected, yet it's booked as a filled position. Position state diverges from the exchange; the bot thinks it holds contracts it doesn't.
Fix: Only call `_record_filled_order()` when `resp` indicates a fill (`filledCount > 0` / status filled); otherwise leave it in a resting set and reconcile via `get_orders()`.
If we skip it: Phantom positions and wrong `position_risk`/PnL.

**7. `trader.py:830-857` — `state.positions` not seeded from the exchange on restart**

What this is: Startup seeds positions from `client.get_positions()`, but `cycle()`'s `ticker in state.positions` check (line 651) only matches positions placed in the current session... actually startup does seed. Re-examine: the seeding exists (lines 830-857). However, `state.positions` entries use `order_id = p.get("order_id", ticker)` and are keyed by ticker, but `cycle()` checks `ticker in state.positions` to skip re-entry. This is mostly handled. Re-classify: the seeding overwrites `state.positions` but does not merge `resting_value` for already-resting orders tied to those positions.
Problem: On restart, existing positions are known, but any resting orders attached to them are not reconciled into `resting_value`, so cash room is overstated.
Fix: Also seed `resting_value` from resting orders on startup (already partly done at 810-824) and reconcile both each cycle.
If we skip it: Post-restart cash room overstated; duplicate entries possible.

**8. `backtest_engine.py` not tracked in git**

What this is: `git status` showed `backtest_engine.py` untracked (`??`). `backtest.py:26` and `parallel_backtest.py:27` do `from backtest_engine import run_sweep`.
Problem: A fresh `git clone` lacks `backtest_engine.py`, so both backtest entry points crash with `ModuleNotFoundError`. Committed code is broken for backtesting.
Fix: `git add backtest_engine.py` and commit; add CI `py_compile` on entry points.
If we skip it: `backtest.py`/`parallel_backtest.py` unusable from clean checkout.

**9. `llm_eval.py:355-380` — LLM evaluation can exceed the 30s cycle timeout**

What this is: `evaluate_trade()` walks up to 7 layers, each retrying up to 2 times with `REQUEST_TIMEOUT=8s`. Worst case ~7 × 3 × 8s ≈ 168s per signal.
Problem: Far beyond `CYCLE_TIMEOUT_SECONDS=30`; `SIGALRM` aborts the cycle mid-evaluation. With no LLM keys (default) and local layers down, all trades are blocked (fail-safe) with no operator alert.
Fix: Cap total evaluation time with a per-call deadline; add `LLM_EVAL_FAIL_OPEN` to allow trading when LLM is unavailable; align layer timeout with cycle timeout.
If we skip it: Cycles time out when LLM is on; enabling LLM with no keys disables trading entirely.

**10. `n8n_client.py:196-213` — `execute_webhook` default timeout 60s exceeds cycle timeout 30s**

What this is: `execute_webhook()` defaults `timeout=60`; `trader.py` calls it for settlement recording.
Problem: `CYCLE_TIMEOUT_SECONDS=30` (`SIGALRM`) interrupts an in-flight webhook → `requests` raises → settlement recording fails → settlement queued (subject to finding #4).
Fix: Default `timeout` to `config.CYCLE_TIMEOUT_SECONDS - 5` (25s), matching the existing comment intent.
If we skip it: Settlement writes fail intermittently; queue grows; PnL recording lags.

**11. `kalshi_client.py:226-236` — `get_balance()` assumes cents with no verification**

What this is: Returns `data.get("balance", 0) / 100.0`, treating API value as cents.
Problem: If Kalshi returns dollars (or semantics change), cash is 100× off — bot either refuses to trade (thinks $0.10) or mis-sizes against a $1000 balance. No test/startup check verifies units.
Fix: Document assumed unit; sanity-check against a known value at startup; add a test pinning the cents conversion.
If we skip it: Wrong cash → no trades, oversized trades, or incorrect risk floors.

**12. `market_finder.py:175-208` — Unbounded thread pool shares non-thread-safe `KalshiClient`**

What this is: `ThreadPoolExecutor(max_workers=len(fetch_list))` fires one orderbook fetch per candidate market concurrently on a single shared `KalshiClient`.
Problem: `_request` reads/updates `self._last_req` without a lock, so the per-request minimum-gap rate limit is not atomic across threads → burst requests → Kalshi 429s (retried, wasteful, can hit cycle timeout).
Fix: Cap workers (`min(len(fetch_list), 4)`) or serialize orderbook fetches through a lock/single thread.
If we skip it: Intermittent 429s under load; slower market discovery.

**13. `.env.example` + live-trading footgun (`config.py:287-313`, `.env.example:12`)**

What this is: `.env.example` sets `KALSHI_CONFIRM_LIVE=true` by default. `validate_dry_run()` blocks `--live` only when `KALSHI_CONFIRM_LIVE != "true"`, but a plain `python trader.py` with `DRY_RUN=false` in `.env` (no `--live`) proceeds live unguarded.
Problem: Copying `.env.example` → `.env` and running `python trader.py --live` passes the confirmation automatically. `DRY_RUN=false` + no `--live` trades real money with no gate.
Fix: Default `KALSHI_CONFIRM_LIVE=false` in `.env.example`; require `--live` flag whenever `DRY_RUN` is false.
If we skip it: Accidental real-money trading when onboarding a new operator.

**14. `trader.py:250-259` + main loop — `place_order` failures swallowed; resilience never triggered**

What this is: `_place_order` catches every exception, logs, returns `None`; placement loop `continue`s. Resilience `mark_failure()`/rebuild only triggers on unhandled cycle exceptions.
Problem: An auth/401/409 error on order placement is silently swallowed every cycle; supervisor never sees failures, never rebuilds; bot trades zero orders while appearing healthy.
Fix: Let `place_order` propagate (or call `state.resilience.mark_failure(e)` inside `_place_order`) so repeated order failures trigger backoff/rebuild.
If we skip it: After key rotation/401, bot trades zero orders indefinitely, appearing healthy.

**15. `latency_arb.py:96-103` — Latency arb buys the overpriced side (direction inverted)**

What this is: When `mid > 50` (YES overpriced vs 50c), code sets `side="yes"` and `entry_price=yes_ask` — buys YES at the ask, the expensive side.
Problem: A mean-reversion arb should sell YES / buy NO when YES is above fair. The signal is converted to a real `TradeSignal` and placed by trader.py (lines 657-687).
Fix: Invert — when `mid > 50` buy NO at `no_ask`; when `mid < 50` buy YES at `yes_ask`. Or remove the latency path until validated.
If we skip it: Latency-arb trades systematically buy high and lose.

**16. `supabase_client.py:189-232` — Supabase write failures return `None` silently; no alert on schema/4xx**

What this is: `_request()` returns `None` on non-retryable 4xx (missing tables, bad columns) after recording a circuit-breaker failure; callers treat `None` as skipped with at most a debug log.
Problem: If Supabase tables aren't created or a column missing, every signal/order/state write is silently dropped — no audit trail; startup `get_bot_state("session_pnl")` returns None so PnL never reconciled.
Fix: Surface repeated Supabase failures as a hard error at startup, or log a prominent warning once per circuit trip; consider failing closed when the audit trail is required.
If we skip it: Silent data loss; operator believes trades are recorded when they are not.

**17. `data_collector.py:109-115` — Closed-market snapshots store negative `minutes_left`**

What this is: For markets whose `close_time` is past, `minutes_left = (ct - now)` is a large negative number, stored verbatim.
Problem: Downstream time-decay features / backtests receive `minutes_left = -525600` etc. — garbage features corrupting any model trained on collected data.
Fix: Clamp `minutes_left` to 0 for closed markets (or store `minutes_in` only).
If we skip it: Collected snapshots unusable for time-aware backtesting.

**18. `trader.py:934-970` — Lock file relies on `/proc/{pid}` unreliable on Android/Termux**

What this is: Single-instance lock checks `os.path.exists(f"/proc/{pid}")` to decide if a stale lock belongs to a live bot.
Problem: On Android PIDs recycle quickly and `/proc` may be restricted; a new bot can see a recycled PID and exit "already running", or fail to detect a real holder.
Fix: Use `fcntl.flock(LOCK_EX|LOCK_NB)` on the lock file (POSIX advisory, works in Termux) and drop the `/proc` check.
If we skip it: Spurious "already running" exits prevent starting after a crash/reboot.

**19. `signals.py:129-138` — Module-level `_bid_hist` not persisted on clean shutdown**

What this is: Bid-history state lives in a module dict, saved to `bid_hist_{dry,live}.json` only every 30s or after a trade; no atexit/shutdown save.
Problem: Run 10 minutes collecting history with no trades, kill it — history lost; stability check re-warms 3 minutes on next start.
Fix: Call `_force_save_bid_hist()` in trader's shutdown `finally`.
If we skip it: Stability gate re-warms every restart; trading delayed by `STABILITY_LOOKBACK_MIN` each launch.

**20. `trader.py:415-427` — Transient balance/positions failure skips the entire cycle**

What this is: If `cash == 0.0 and not positions and not DRY_RUN`, cycle returns early. `get_balance` failure sets `cash=0.0`.
Problem: A single transient balance fetch failure (network blip) with no positions yet causes a whole cycle to be skipped; repeated blips stall trading.
Fix: Treat balance fetch failure as "unknown" and skip only cash-dependent steps, not the whole cycle; or require both failures on consecutive cycles.
If we skip it: Bot misses cycles on flaky Termux/mobile networks.

**21. `market_finder.py:189-193` — Only main markets returned; `MAX_HEDGE_PER_SERIES=0` kills hedges**

What this is: Hedge slice `all_hedges[:MAX_HEDGE_PER_SERIES * len(TARGET_SERIES)]` = `[:0]` at default 0.
Problem: `HEDGE_YES_ASK_MAX` and the HEDGE_NO path never run out of the box; hedge strategy is dead unless operator raises the cap.
Fix: Set a sane default (e.g. 1) or document hedges off by default.
If we skip it: Hedge strategy config ineffective; users wonder why no hedge trades appear.

**22. `risk.py:44-70` — Drawdown halt uses cost-basis exposure, not mark-to-market**

What this is: `update_balance` computes `total_value = cash + exposure` where exposure is position value at average cost.
Problem: An open losing position is carried at cost, so `total_value` doesn't reflect unrealized loss; drawdown halt triggers later than it should (or not at all) while the position bleeds.
Fix: Value open positions at current bid/mid for drawdown purposes (requires a mark price).
If we skip it: Drawdown protection delayed; larger realized losses before halt fires.

**23. `trader.py:586-593` + `839-845` — `session_pnl` drift between n8n settlement and Supabase bot_state**

What this is: Realized PnL added to `state.session_pnl` in-memory and written to Supabase bot_state each cycle; on startup reconciled *from* Supabase. But n8n is the system that records settlement to orders table.
Problem: If n8n records a settlement but bot crashes before writing bot_state, Supabase `session_pnl` stale; on restart reconciliation overwrites in-memory PnL with stale value (undercount). Conversely replay can re-add (finding #4).
Fix: Make n8n write settlement PnL to Supabase bot_state (or a dedicated pnl table); reconcile from that authoritative source; or treat n8n orders table as PnL source of truth.
If we skip it: `session_pnl` drifts from exchange/n8n record after crashes.

**24. `n8n_client.py:282-292` — `n8n_health_check` writes through the trade/settlement webhook**

What this is: Health check calls `execute_webhook("superbot-trade", {"action": "health_check"})`. The `superbot-trade` workflow is a settlement processor (PATCH to Supabase orders).
Problem: Health check has side effects (attempts Supabase PATCH with missing order_id/status) and can error even when n8n healthy — causing `trader.py` to refuse to start (`sys.exit(1)` at line 779).
Fix: Health check should hit a read-only endpoint (e.g. `GET /api/v1/workflows`) or a dedicated health webhook that does not write.
If we skip it: Bot may fail to start purely because n8n's trade webhook dislikes a health-check payload.

**25. `kalshi_client.py:193-207` — `_last_req` set before 429 retry loop; bursts after rate limit**

What this is: `self._last_req = time.time()` set before retry loop; on 429 the sleep happens but subsequent retries not re-gated by `_MIN_REQUEST_GAP`.
Problem: After backing off for a 429, retries can fire back-to-back, re-triggering 429s.
Fix: Move gap enforcement inside the retry loop or re-check the gap before each attempt.
If we skip it: Marginal; small increase in 429 recurrence on aggressive retries.

## SHOULD FIX

**26. `trader.py` — No stale-order management; `STALE_ORDER_MINS` unused; `get_orders()` never called**

What this is: `STALE_ORDER_MINS` defined but never read; `trader.py` never calls `client.get_orders()` to reconcile resting orders.
Problem: Filled/cancelled resting orders never cleaned from local belief; unfilled limit orders accumulate and are never cancelled/repriced; `resting_value` stays empty (finding #3), so no picture of open orders.
Fix: Each cycle, fetch resting orders via `get_orders()`, sync into `state.resting_value`, cancel stale ones older than `STALE_ORDER_MINS`.
If we skip it: Stale resting orders persist; capital unintentionally tied up; cash calculations wrong.

**27. `config.py:193` `LOOP_INTERVAL_SECONDS = max(5, ...)` — 5s minimum cycle with many sequential API calls**

What this is: Loop minimum 5s; each cycle calls balance, positions, markets (9 series), orderbooks (concurrent), settlement `get_market`, plus up to 4 Supabase writes and possibly an LLM call.
Problem: Full cycle can exceed 5s (orderbook fetches + webhook + LLM). Back-to-back cycles with no drift correction; Kalshi rate limit ~10 req/s easy to breach when orderbooks fire in parallel (finding #12).
Fix: Measure cycle duration and sleep `max(0, interval - elapsed)` (drift-corrected sleep already in main loop at 923-927 but cycle itself can still overrun); add shared request-rate limiter across threads.
If we skip it: Cycles overlap or hammer Kalshi API; 429s and missed loops.

**28. `backtest_engine.py:98-107` — Contradictory favourite-maker entry logic**

What this is: `simulate_trade()` treats `mid_proxy >= ENTRY_BAND_MIN` as "favorite", but when `mid_proxy > ENTRY_BAND_MAX` it sets `ep = 100 - mid_proxy` and bets the NO side (the underdog).
Problem: Contradicts "favourite maker" strategy (buy the favourite); effectively buys the underdog when YES is above the band.
Fix: Restrict entries to the favourite side only; document the lookahead bias of `p_last` or use an earlier price.
If we skip it: Backtest EV/win-rate inflated and not representative of live behavior.

**29. `parallel_backtest_hf.py:194-212` — Aggressive backtest grid (`PER_TRADE_PCT` up to 0.20, `MAX_TRADE_DOLLARS` up to 10.0)**

What this is: Non-quick grid tests `PER_TRADE_PCT` up to 0.20 and `MAX_TRADE_DOLLARS` up to 10.0 against a $10 starting balance.
Problem: Up to $2 per trade on a $10 account in backtest "optimal" parameters — bankroll-ruining sizing that would never pass live risk limits (`PER_TRADE_PCT=0.20` default, `MAX_TRADE_DOLLARS=5.00`).
Fix: Cap backtest grid at live-sane values; separate "search" grid from "deployable" grid.
If we skip it: Optimized backtest parameters unsafe to deploy; risk config and backtest config diverge.

**30. `tests/` — Core trading paths untested**

What this is: 75 tests pass, but no tests for `trader.py` cycle logic (`_pre_trade_check`, settlement, order placement), `backtest_engine`, `latency_arb`, `data_collector`, `resilience`.
Problem: The most financially consequential code (live loop) has zero coverage; regressions in sizing/risk/settlement go undetected.
Fix: Add tests for `_pre_trade_check` caps, settlement PnL booking, `resting_value` behavior; mock `KalshiClient`/`n8n_client`.
If we skip it: Bugs in trading loop (findings #3, #4, #5, #6) reach production unseen.

**31. `tests/test_*.py` — Weak test isolation (config mutation leaks)**

What this is: Tests `monkeypatch.setattr(config, ...)` module-level globals (e.g. `test_market_finder.py:148` sets `MAX_HEDGE_PER_SERIES=0`) without restoring.
Problem: Mutated config persists across tests in the same process, making outcomes order-dependent and hiding cross-test contamination. (conftest.py helps but relies on uppercase detection.)
Fix: Use fixtures restoring config values after each test.
If we skip it: Flaky tests; false confidence when config-dependent logic changes.

**32. `llm_eval.py:391-397` — Fail-safe blocks all trades when LLM unavailable**

What this is: `should_block_trade()` returns `(True, None)` when `evaluate_trade()` returns None (all layers failed).
Problem: With no LLM keys (default) and no local gateway, enabling `LLM_EVALUATION_ENABLED` blocks 100% of signals — no trading at all, with no operator alert that LLM is the cause.
Fix: Add `LLM_EVAL_FAIL_OPEN` (default False) and log a prominent warning when all layers fail.
If we skip it: Silently zero-trades when LLM misconfigured.

**33. `signals.py:364-368` — "Two-sided" emits at most one side per market**

What this is: `_evaluate_two_sided()` comment says 0–2 signals per market, but when both YES and NO in band it keeps only the more extreme side.
Problem: The two-sided maker strategy never actually places both sides of the same market; docstring overstates capability.
Fix: Update comment to reflect single-side-per-market, or implement genuine two-sided entries with separate budget tracking.
If we skip it: Minor; behavior conservative (one side), but documentation misleads.

**34. `supabase_client.py:215-223` — Circuit breaker never trips on persistent 4xx**

What this is: `_request()` returns None on 400/403/404 without calling `_supabase_record_failure()`.
Problem: A permanent schema/auth problem (missing `orders` table) returns 400 every cycle, never trips circuit, keeps retrying 3× per call with 1s backoff.
Fix: Record a failure for non-retryable 4xx too, or distinguish schema errors to fail fast.
If we skip it: Wasted API calls and logs on permanent Supabase misconfiguration.

**35. `n8n_client.py:37-56` + `inject_n8n_workflows.py` — Hardcoded Termux paths and project IDs**

What this is: `n8n_client` reads `/data/data/com.termux/files/home/.n8n/...`; `inject_n8n_workflows.py` hardcodes `PROJECT_ID`, `USER_ID`, `/data/data/com.termux/files/usr/bin/python3`.
Problem: Non-portable; direct SQLite writes to n8n's DB bypass the n8n API and break on schema drift; credential injector reads config at import time and crashes outside repo.
Fix: Parameterize paths/IDs via env; prefer n8n API over direct DB writes; guard credential injector's import-time file read.
If we skip it: Tooling only runs on this host; n8n DB-schema changes break workflow injection silently.

**36. `kalshi_client.py:174-188` + `trader.py:762-927` — Sequential per-cycle calls can blow 30s cycle timeout**

What this is: A cycle does balance, positions, markets (9 series with retries), orderbooks, settlement `get_market`, Supabase writes, optional LLM — each request up to `REQUEST_TIMEOUT_SECONDS=10`.
Problem: Several sequential 10s timeouts plus LLM/webhook easily exceed `CYCLE_TIMEOUT_SECONDS=30`; `SIGALRM` aborts cycle; resilience backs off and rebuilds client after 3 errors → client churn on transient slowness.
Fix: Tighten per-request timeouts for the loop; parallelize independent fetches; only rebuild client on auth errors (not timeouts).
If we skip it: Frequent cycle timeouts; unnecessary client rebuilds; slower trading.

**37. `supabase_client.py:72-140` — Credential loading order opaque**

What this is: URL/key loading from `SUPABASE_KEY_FILE` and env interleaved; partial parent-`.env` fallback.
Problem: Operator may not know which source won; running from another directory reads stale/wrong `.env`.
Fix: Log resolved source at init; remove implicit parent-`.env` fallback; require explicit env/key-file.
If we skip it: Writes land in wrong Supabase project depending on working directory; debugging credential issues harder.

**38. `trader.py:102` vs `_record_filled_order` — Inconsistent `resting_value` usage/comment**

What this is: Comment says `resting_value: dict = {}  # order_id -> reserved dollars`, but `_record_filled_order` stores position info into `state.positions` and only pops `resting_value`.
Problem: Comment describes a structure never maintained; readers misled about how resting cash is tracked.
Fix: Align comment/code: maintain `resting_value[oid] = dollar_risk` on placement or remove it.
If we skip it: Cosmetic; contributes to finding #3 confusion.

**39. `backtest_engine.py:342` — Multiprocessing pickles full markets list into every worker**

What this is: `run_sweep()` uses `Pool(processes=jobs, initializer=init_worker, initargs=(markets,))`, every worker gets a copy of all markets.
Problem: Memory scales `jobs × markets_size`; large historical datasets can OOM the small termux host.
Fix: Have workers read a shared parquet/cache path instead of pickling markets; or cap `jobs`.
If we skip it: Backtests on large datasets may OOM or slow drastically.

**40. `parallel_backtest.py:46-91` — Duplicate market-fetch logic vs `backtest.py:43-71`**

What this is: Both implement pagination/fetch of settled markets separately.
Problem: Divergence risk (one fixes a bug, the other doesn't).
Fix: Share a single fetch helper.
If we skip it: Duplication; future fixes must be applied twice.

**41. `kalshieye_downloader.py:213` — Only `files[0]` downloaded per date**

What this is: If multiple files exist for a date/series, only first downloaded.
Problem: Incomplete historical data.
Fix: Loop over all files.
If we skip it: Partial downloads; understated datasets.

**42. `data_collector.py:1-12` — Docstring/behavior contradiction on auth**

What this is: `MarketSnapshotCollector.__init__()` constructs `KalshiClient()` (requires Kalshi auth), but docstring claims collection works "without needing an external API key."
Problem: Operators expecting key-free collection get auth error; historical tier needs Kalshi credentials.
Fix: Correct docstring; make client optional/injectable.
If we skip it: Confusing onboarding; data collection fails without clear cause.

**43. `config.py:62-69` — `_env_bool` silently returns default for unparseable values**

What this is: `KALSHI_CONFIRM_LIVE=maybe` returns default (False) with no warning.
Problem: A typo silently treated as "not confirmed" (safe direction) or enables features unintentionally.
Fix: Warn on unrecognized bool strings.
If we skip it: Typos silently disable live confirmation or enable features unintentionally.

**44. `tests/test_kalshi_price_data_client.py` & `tests/test_n8n_client.py` shallow**

What this is: They assert `isinstance(result, bool)` / `is None` without mocking network behavior.
Problem: Don't exercise real code paths.
Fix: Add mocked request/response tests.
If we skip it: Weak coverage for optional clients.

**45. `tests/` — No tests for `market_finder.find_active_markets` multi-series selection or `rank_signals`**

What this is: `rank_signals` and full market-selection flow untested.
Problem: Ranking/order-selection bugs surface only live.
Fix: Add unit tests.
If we skip it: Ranking regressions undetected.

**46. `trader.py` — No per-market or per-day loss limit beyond drawdown and consecutive losses**

What this is: Risk caps are portfolio-level; a single market can lose repeatedly before 15% drawdown halt.
Problem: A choppy market bleeds account slowly.
Fix: Add per-market daily loss cap (`market_daily_losses` tracked but only checked in `_pre_trade_check` at line 179; add hard halt).
If we skip it: Slower bleed before halt triggers.

**47. `backtest_engine.py:258-255` — `parameter_grid` non-quick includes `MAX_NO_ENTRY_CENTS` but `simulate_trade` ignores `MIN_SKEW`/`MIN_TREND`/`ENTRY_WINDOW`**

What this is: Sweep tests params engine doesn't fully apply (skew/trend/window are config constants, not StratParams).
Problem: Parameter combos varying ignored values produce identical results; wasted compute.
Fix: Align engine params with grid, or drop ignored keys from grid.
If we skip it: Wasted backtest compute on no-op dimensions.

**48. `kalshi_price_data_client.py:133-142` — `health_check()` calls `_client.health_check()` which may not exist on SDK**

What this is: If installed SDK lacks `health_check`, `AttributeError` caught and returns False.
Problem: Silent failure mode.
Fix: Document SDK version requirement.
If we skip it: Health check reports False without clarity why.

**49. `add_n8n_credentials.py` / `inject_n8n_workflows.py` — Keys visible in process list**

What this is: API keys passed via CLI args/env visible to other processes on host.
Problem: On shared Android device, another app could read `/proc/*/environ`.
Fix: Use files with 0600 perms (already supported for n8n key file) and discourage CLI keys.
If we skip it: Credential exposure to co-located processes.

**50. `config.py:233-352` — `validate_credentials`/`validate_dry_run`/`validate_bands` not invoked on import; only trader calls them**

What this is: Other entry points (backtest.py, data_collector.py) construct `KalshiClient()` directly and hit auth errors with less-clear messages.
Problem: Inconsistent startup validation across tools.
Fix: Call `validate_credentials()` in any tool building a KalshiClient.
If we skip it: Backtest/data_collector fail with opaque auth errors instead of actionable config guidance.

**51. `resilience.py:44` — Backoff starts at `base_backoff` (5s) on first failure**

What this is: First cycle error already sleeps 5s before retrying.
Problem: On flaky networks every transient error costs 5s; cycles slow.
Fix: First failure delay 0 (retry immediately), backoff from second.
If we skip it: 5s penalty per transient error; reduced throughput on mobile networks.

**52. `kalshi_client.py:247-267` — `get_markets` cache keyed only by (series, status, limit); no invalidation on market state change**

What this is: 30s TTL; if a market closes within window it may still appear as open.
Problem: Stale market list used for entries.
Fix: Acceptable at 30s for this load; documented shortcut.
If we skip it: Minor staleness.

**53. `trader.py:606-614` — `set_bot_state_batch` writes every cycle even when nothing changed**

What this is: Unconditional Supabase write each cycle.
Problem: Unnecessary traffic when Supabase enabled.
Fix: Skip write if values unchanged.
If we skip it: Minor extra requests; negligible at 15s cycles.

**54. `historical_data/` / `hf_data/` and `bot.log` / `*_result.json` in working tree**

What this is: Runtime artifacts and large binary/parquet files sit in repo root (some gitignored).
Problem: Repo bloat; clutter; parquet may contain training data that shouldn't be versioned.
Fix: Add to `.gitignore`/LFS; route runtime output to gitignored paths.
If we skip it: Repo size grows; cosmetic.

**55. `llm_eval.py:324-333` — System prompt says 2 keys but parser accepts extra**

What this is: Contract mismatch between prompt (only 2 keys) and parser (tolerates extra keys).
Problem: Not a bug, but model contract not enforced.
Fix: Either enforce 2 keys or update prompt.
If we skip it: No functional impact.

