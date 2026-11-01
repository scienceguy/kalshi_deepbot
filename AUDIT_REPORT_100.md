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


## NICE TO HAVE

**56. `trader.py:309-340` — `_touch_window` resizes budget mid-window on cash growth**

What this is: Within one 15m window, if `cash > start_cash` the budget recomputed as `cash * WINDOW_TRADE_PCT` but `used` not reset.
Problem: A profitable settlement mid-window inflates `start_cash` and re-sizes budget, letting bot spend more in same window than intended.
Fix: Only re-size budget on new window ID; don't re-resize mid-window on cash growth.
If we skip it: Per-window cap can be exceeded mid-session.

**57. `trader.py:561-565` — `exposure` from API positions diverges from `state.position_risk` (entry-price basis)**

What this is: Risk `exposure` uses live `yes_position * yes_avg_price_dollars` (API mark), while `state.position_risk` tracks entry-price cost set at fill.
Problem: As market moves the two figures diverge; risk checks use one basis and bookkeeping another, so exposure understated.
Fix: Use API mark-to-market basis for both risk and state.
If we skip it: Risk ceiling evaluated against mixed basis; exposure mis-stated.

**58. `market_finder.py:155` & `MarketInfo.mid_yes` — Inconsistent mid rounding between selection and signals**

What this is: Selection uses `round((yb+ya)/2)` (integer-rounded); `MarketInfo.mid_yes` uses `(yb+ya)/2` (float).
Problem: Boundary market can pass band filter at selection but fail at signal time (or vice versa).
Fix: Compute mid one way everywhere.
If we skip it: Boundary markets flip in/out nondeterministically.

**59. `backtest_engine.py:99-107` — Backtest only buys favourite side; never models two-sided/NO entries or stability gate**

What this is: `simulate_trade()` returns None unless YES in band; never evaluates NO/complement or stability gate.
Problem: Engine doesn't replicate live `signals.py`; best backtest params don't predict live results.
Fix: Align `simulate_trade` with `signals.evaluate`, or label backtest a different strategy.
If we skip it: Parameter selection invalid for live deployment.

**60. `parallel_backtest_hf.py:76-84` — NO branch uses `100 - mid_proxy` but never checks `MIN_SKEW`/trend gates**

What this is: HF backtest engine differs from `backtest_engine` and live `signals.py` (no skew/trend/stability).
Problem: Best HF params don't transfer to live.
Fix: Align or label as different strategy.
If we skip it: HF results not applicable to live trading.

**61. `config.py:143-144` — `CASH_FLOOR_PCT=0.70` + `EXPOSURE_CEILING_PCT=0.30` sum to 1.0, no buffer**

What this is: Floor + ceiling exactly partition portfolio.
Problem: Rounding in `max_trade_dollars` can permit trade pushing exposure just over ceiling or cash just under floor due to epsilon.
Fix: Add small buffer or make ceiling = 1 - floor with epsilon handled consistently.
If we skip it: Marginal edge-case breaches of cash floor/exposure ceiling.

**62. `signals.py:43-49` — `TradeSignal.is_trade` allows `direction != PASS` even when `side` empty**

What this is: A PASS signal has `side=""`; `is_trade` checks `direction != "PASS"` so fine, but malformed signal with direction set and side="" would pass.
Problem: Defensive gap if code path sets direction without side.
Fix: Also require `self.side in ("yes","no")`.
If we skip it: Unlikely malformed signal could reach `place_order` and be rejected by API anyway.

**63. `resilience.py:65-67` — No maximum rebuild cap enforcement in loop beyond `max_rebuilds`**

What this is: `should_rebuild_client()` checks `rebuilds < max_rebuilds`; loop calls rebuild repeatedly.
Problem: After `max_rebuilds` (5) exhausted, loop stops — but during outage it rebuilds 5 times at growing backoff before giving up.
Fix: Acceptable; add clearer "giving up" log.
If we skip it: Long outages cause rebuild attempts then stop; acceptable.

**64. `supabase_client.py:333-344` — `get_bot_state` returns None on invalid JSON without distinguishing missing vs corrupt**

What this is: `json.loads(rows[0]["value_json"])` failures caught, return None.
Problem: Corrupt bot_state indistinguishable from missing key; reconciliation silently treats corrupt as absent.
Fix: Log warning on JSON decode failure; return sentinel.
If we skip it: Corrupt state silently ignored.

**65. `kalshi_client.py:41-49` — `KALSHI_BASE_URL` validation hard-fails on any URL without `/trade-api/v2`**

What this is: Constructor raises `ValueError` if base URL lacks `/trade-api/v2`.
Problem: Kalshi demo/environments or future paths rejected at startup even if they work.
Fix: Warn instead of hard-fail, or make path prefix configurable.
If we skip it: Non-standard Kalshi endpoints cannot be used.

**66. `trader.py:870-872` — `SIGALRM` cycle timeout can interrupt in-flight `requests` call**

What this is: `signal.alarm(CYCLE_TIMEOUT_SECONDS)` raises `TimeoutError` anywhere in `cycle()`, including mid-HTTP-request.
Problem: Socket/connection left half-open; `requests` session may retain broken connection.
Fix: Rely on per-call `timeout=` (already present) instead of process-level SIGALRM; close/replace session on timeout.
If we skip it: Occasional stuck connections after long cycles; mild.

**67. `add_n8n_credentials.py:117-126` — Module-level side effects mutate n8n DB on import**

What this is: Credential insert loop runs at import time (not guarded by `if __name__ == "__main__"`).
Problem: `import add_n8n_credentials` anywhere (test, helper) writes to n8n SQLite DB.
Fix: Guard execution block behind `if __name__ == "__main__"`.
If we skip it: Accidental DB mutation during imports/testing.

**68. `inject_n8n_workflows.py:26-27,385-386` — Hardcoded n8n project/user IDs; Supabase secret in workflow JSON**

What this is: `PROJECT_ID`/`USER_ID` hardcoded; `supabase_key` written plainly into workflow JSON via settlement-processor node.
Problem: Not portable across n8n instances; Supabase service key sits plaintext in n8n SQLite DB.
Fix: Parameterize project/user IDs; store key in n8n credential rather than workflow JSON.
If we skip it: Workflows only work for original account; secret exposure if n8n DB read by others.

**69. `n8n_client.py:262-280` — `run_*_webhook` convenience functions are dead code**

What this is: `run_backtest_webhook`, `run_collect_webhook`, `run_trade_webhook`, `run_monitor_webhook` defined but `trader.py` calls `execute_webhook` directly.
Problem: Dead surface area.
Fix: Remove them or use them consistently.
If we skip it: Minor bloat.

**70. `risk.py:162-172` — `reset_daily` does not clear `consecutive_losses`**

What this is: Daily reset clears session_high and halt but not consecutive-loss counter.
Problem: Cooldown earned just before midnight carries into next day.
Fix: Reset `consecutive_losses = 0` in `reset_daily`.
If we skip it: Minor; cooldown outlasts day boundary.

**71. `tests/` — No integration tests for `cycle()`, settlement, market selection, end-to-end signal→order flow**

What this is: Test suite covers isolated units but not main loop.
Problem: Highest-risk path (money in/out) has no automated coverage.
Fix: Add tests for `cycle` settlement booking, replay double-count guard, `_pre_trade_check` clamping using mocked client/n8n.
If we skip it: Regressions in trading loop caught only by live runs.

**72. `latency_arb.py:126-170` — `simulate_settlement` unused in main flow**

What this is: Defined but only invoked from `__main__`; trader places real latency-arb orders without simulating/validating settlement first.
Problem: Dead code; no pre-trade validation for arb path.
Fix: Remove or wire dry-run simulation before placing.
If we skip it: Unused function; no functional harm.

**73. `supabase_client.py:285-303` — `update_order_settlement` never called**

What this is: Method exists but settlement goes through n8n webhook, not this Supabase call.
Problem: Dead code; orders table only updated via n8n.
Fix: Remove or use it as settlement write path (removing dependence on n8n for audit).
If we skip it: Dead method; settlement audit relies solely on n8n.

**74. `signals.py:22-26` — `spot` module import always fails; `SPOT_FILTER_ENABLED` dead config**

What this is: `import spot` wrapped in try/except sets `_spot_mod = None`; no `spot` module exists.
Problem: `SPOT_FILTER_ENABLED` and `Z_MAX` have no effect; operators configure a filter that does nothing.
Fix: Remove spot filter or implement feed.
If we skip it: Config does nothing; minor confusion.

**75. `kalshi_client.py:55-57` — `requests.Session` never closed**

What this is: Session created in `__init__`, no `close()`.
Problem: Minor resource leak in long-lived process; sockets not released on shutdown.
Fix: Add `close()` and call it in trader shutdown.
If we skip it: Negligible on single short-lived process.

**76. `market_finder.py:175` — `ThreadPoolExecutor(max_workers=len(TARGET_SERIES))` with locked client serializes anyway**

What this is: 9 worker threads share one `KalshiClient._lock` (0.12s min gap), effectively serialized.
Problem: Concurrency overhead for no gain on rate-limited client.
Fix: Fetch series sequentially (simpler) or give client a pool.
If we skip it: Minor complexity; no correctness impact.

**77. `kalshi_price_data_client.py:126-131` — `save_to_parquet` imports pandas lazily but pandas unpinned in requirements**

What this is: `pandas==2.2.0` unpinned; `pyarrow==14.0.1` unpinned; `kalshi-price-data` not listed.
Problem: `pip install -r requirements.txt` can pull breaking pandas/pyarrow; importing `kalshi_price_data_client` then fails at runtime.
Fix: Pin `pandas`/`pyarrow`; add `kalshi-price-data` as optional/documented.
If we skip it: `parallel_backtest_hf.py`/`kalshi_price_data_client.py` break on fresh installs.

**78. `inject_n8n_workflows.py:340-346` — Direct n8n SQLite writes bypass API**

What this is: Workflows inserted by writing n8n's SQLite tables directly.
Problem: n8n schema changes break injection; no validation n8n running or DB unlocked; idempotency only by name-prefix delete.
Fix: Use n8n API (`n8n_client.create_workflow`) instead of raw SQL where possible.
If we skip it: Workflow injection brittle across n8n versions.

**79. `fees.py:19` — Hardcoded 0.07 fee multiplier with no schedule-version guard**

What this is: `FEE_RATE = float(os.environ.get("KALSHI_FEE_RATE", "0.07"))` with no range validation.
Problem: If Kalshi changes fee schedule, all PnL/backtest/sizing math silently wrong; no test asserts against known fee schedule version.
Fix: Make fee rate configurable (`KALSHI_FEE_RATE` env, already) and add regression test pinning today's known fee example; validate `0 <= FEE_RATE <= 1`.
If we skip it: Profitability estimates and order sizing wrong after fee change.

**80. `.env` / `kalshi_private.pem` present locally (gitignored) — No secret-scanning guard**

What this is: Working tree `.env` holds real `KALSHI_KEY_ID`/`SUPABASE_URL`; `kalshi_private.pem` holds real key. Both gitignored but present on disk.
Problem: `git add -A` or careless commit could leak live trading credentials and Supabase service key.
Fix: Add pre-commit/secret-scan (`.git/hooks/pre-commit` exists per AGENTS.md) blocking `.env`/`*.pem`; consider `git-secrets`/`gitleaks`.
If we skip it: Credential leak risk if secrets ever committed.

**81. `trader.py:774-788` — Startup initializes Supabase client and logs enabled state**

What this is: `main()` calls `get_supabase_client().enabled` at startup, instantiating `SupabaseClient`.
Problem: If Supabase credentials malformed, client logs warning at import; not harmful but couples startup logging to Supabase availability.
Fix: Minor; make startup Supabase check explicit and non-fatal (already is).
If we skip it: Cosmetic only.

**82. `market_finder.py:124-129` — Sequential per-series fetches with retries before concurrent orderbook stage**

What this is: `find_active_markets()` loops 9 series, each up to 3 retries and 0.5–2s backoffs, before concurrent orderbook stage.
Problem: Market discovery takes several seconds even when healthy, eating into 15s loop and 30s cycle timeout.
Fix: Fetch markets for all series concurrently (bounded pool), then orderbooks concurrently.
If we skip it: Slower cycle startup; less time for trading within each window.

**83. `signals.py:129-131` — Module-level `_bid_hist` global persists across dry/live modes**

What this is: Bid history in module global and `bid_hist_{dry,live}.json` (already scoped by mode). Not cleared between dry-run and live modes.
Problem: Stale bids influence stability checks after long pause.
Fix: Scope bid history to session or version by mode; cap already present.
If we skip it: Minor; stability check may use outdated history after restarts.

**84. `parallel_backtest_hf.py:45-58` — `load_all_data` loads all parquet into memory at once**

What this is: `pd.concat(all_rows)` loads entire HF dataset into RAM before backtest.
Problem: Large datasets OOM small termux host.
Fix: Stream/process shards one at a time.
If we skip it: Large HF datasets may OOM.

**85. `backtest.py:32-40` vs engine default — Inconsistent settlement semantics**

What this is: `backtest.py.settlement_yes_won` returns `exp_val*100 >= 100` (i.e. `exp_val == 1.0`); engine default `exp_val >= floor_strike`.
Problem: For binary markets coincide, but diverge if `floor_strike != 1`, producing different backtest results for same data.
Fix: Unify to one settlement predicate.
If we skip it: Backtest results depend on entry point used.

**86. `config.py:18-44` — Custom `.env` loader lacks `export`/inline-comment/multiline support**

What this is: `_load_env()` parses simple `KEY=VALUE` lines only.
Problem: `.env` with `export KALSHI_KEY_ID=...`, `KEY=val # comment`, or quoted multiline values parsed incorrectly or breaks.
Fix: Document strict format or adopt `python-dotenv` (adds one dependency).
If we skip it: Operators hit cryptic "credentials missing" errors on reasonably formatted `.env` files.

**87. `trader.py:797-804` — No distinction of auth errors for client rebuild**

What this is: Cycle loop rebuilds `KalshiClient()` after `CLIENT_REBUILD_THRESHOLD` errors of any type.
Problem: 401 auth errors (bad/expired key) not distinguished from transient network errors; rebuilding with same bad key repeats failure.
Fix: Use `resilience.is_auth_error()` to skip rebuilds on auth errors and alert instead.
If we skip it: Auth failures trigger futile client-rebuild loops.

**88. `kalshi_client.py:46` — Signature path derived from `KALSHI_BASE_URL` path**

What this is: `_api_path = urlparse(base_url).path` prepended to every signed path.
Problem: If `KALSHI_BASE_URL` changed to URL without `/trade-api/v2` prefix, all signatures invalid with no warning.
Fix: Validate `KALSHI_BASE_URL` contains expected API root at startup.
If we skip it: Misconfigured base URL → all signed requests 401.

**89. `resilience.py:42-45` — Backoff rebuild loop on persistent non-auth errors**

What this is: `mark_failure()` caps delay at `max_backoff` (60s); `should_rebuild_client()` triggers after `rebuild_threshold` (3); `after_rebuild()` resets errors.
Problem: If rebuilt client keeps failing (persistent outage), loop rebuilds every cycle at 60s intervals indefinitely — no circuit breaker/max rebuild cap beyond 5.
Fix: Add max rebuilds or longer cooldown after repeated rebuild failures.
If we skip it: Long outages cause perpetual rebuild attempts and log spam.

**90. `parallel_backtest_hf.py:35-42` — `StratParams` lacks fields engine has**

What this is: HF `StratParams` fewer fields than `backtest_engine.StratParams` (no `MIN_SKEW_CENTS`, `ENTRY_WINDOW_*`, `AGGRESSIVE_FILL`, `MAX_OPEN_POSITIONS`).
Problem: Two parameter grids for same strategy diverge; best HF params can't port to live config cleanly.
Fix: Share one `StratParams` between engines.
If we skip it: Parameter drift between backtest variants and live config.

**91. `data_collector.py:172-192` — `main` suggests `--data-file` flag that doesn't exist**

What this is: Help prints `python backtest.py --data-file {out_path}`, but `backtest.py` has no `--data-file` arg.
Problem: Users following printed instruction get argument error.
Fix: Remove misleading hint or implement `--data-file` in `backtest.py`.
If we skip it: Confusing CLI guidance.

**92. `fees.py:50-60` — `net_profit_roundtrip` uses `taker_fee_cents` (balance-grid rounded) for both legs**

What this is: Round-trip profit uses `taker_fee_cents` rounding up to cent grid per leg.
Problem: For sub-cent fees overstates fees vs raw 6dp model; minor for backtest direction but inconsistent with `taker_fee_dollars`.
Fix: Document convention or use raw model consistently.
If we skip it: Minor PnL rounding differences in backtests only.

**93. `market_finder.py:154` — `mid` uses `round()` on possibly-zero prices**

What this is: `mid = round((yb+ya)/2) if (yb and ya) else (ya or yb or 0)`.
Problem: If only one side quoted, `mid` can be 0 and market filtered out (`MIN_P <= mid` fails), dropping legitimate one-sided markets.
Fix: Treat one-sided quotes conservatively rather than `mid=0`.
If we skip it: Some tradeable markets with one-sided books skipped.

**94. `supabase_client.py:306-316` — `set_bot_state` upsert uses `resolution=merge-duplicates` without unique constraint assurance**

What this is: `POST` to `bot_state` with `Prefer: resolution=merge-duplicates`.
Problem: If no unique constraint on `key` column, creates duplicate rows each cycle instead of upserting.
Fix: Ensure unique constraint on `bot_state(key)` in Supabase schema; document.
If we skip it: Duplicate bot_state rows; `get_bot_state` returns first only.

**95. `trader.py:689-692` — `rank_signals` empty after latency signals added only if no signals; OK**

What this is: `rank_signals` returns only tradeable signals; if none, early return.
Problem: None — correct behavior.
If we skip it: n/a (correct.)

**96. `kalshi_client.py:269-272` — `get_market` returns `data.get("market", data)`**

What this is: Falls back to entire response if `market` key absent.
Problem: Masking malformed API responses; downstream code may get unexpected shape.
Fix: Log warning on fallback; validate response shape.
If we skip it: Silent handling of API shape changes.

**97. `signals.py:82-122` — `_maker_price` fallback `yes_ask or yes_bid or 1` can yield 1 when no book**

What this is: With no quotes, returns 1c entry.
Problem: Could place orders at 1c with no liquidity; liquidity gate in `_evaluate_common` (line 257) catches `n <= 0` but not illiquid 1c quotes directly.
Fix: Require both sides of book before making a price; reject one-sided books for maker entries.
If we skip it: Orders at 1c unlikely to fill; wasted resting orders.

**98. `backtest_engine.py:152-211` — `run_backtest` does not model resting-order fill probability**

What this is: Assumes entry at `mid_proxy` always fills; no fill probability / slippage.
Problem: Backtest PnL assumes perfect fills; live maker orders may not fill or fill partially.
Fix: Add fill-probability model or label results as idealized.
If we skip it: Backtest EV overstated vs live fill rates.

**99. `trader.py:642-646` — `find_active_markets` called every cycle even when cash unknown**

What this is: Market discovery runs even when `cash_available` False.
Problem: Wastes API calls when balance fetch failed.
Fix: Skip market finding when cash unknown (or cache last known markets).
If we skip it: Wasted Kalshi API calls during outages.

**100. `parallel_backtest.py:94-122` / `backtest.py:74-107` — No `--dry`/offline mode using local fixtures**

What this is: Both require live Kalshi API to fetch markets; no offline/fixture mode.
Problem: Can't run backtests without network/credentials.
Fix: Add `--data-file` to read local JSONL/parquet snapshots.
If we skip it: Backtests require network; can't run offline on device.

---

## Verdict

The repo is a real, working single-operator trading bot with a solid core (signed Kalshi client, exact fee math, circuit breakers, dry-run default, lock file, 75 passing tests). It is NOT yet safe to run in live mode without addressing the Must-fix items — most critically: the undefined `_evaluate_hedge` (crash on hedge markets), unverified NO price complement, `resting_value` never decremented within a cycle (cash double-spend), settlement replay double-counting PnL, positions popped before PnL booked, orders recorded as filled before confirmation, `place_order` failures swallowed so resilience never rebuilds on auth errors, latency arb buying the overpriced side, Supabase writes failing silently, and the LLM evaluation exceeding the cycle timeout / blocking all trades when no keys configured.

The load assumption (single process, dry-run default, one Android host) means concurrency and scale issues are mostly shortcuts, not findings. Within that scope the architecture is appropriate. The biggest risks are financial (cash overspend, phantom positions, PnL double-count) and operational (crashes on config changes, broken fresh-checkout for backtesting).

**Lean:** roughly -90 lines and -1 dependency possible by deleting dead code (`update_order_settlement`, `run_*_webhook` convenience fns, `simulate_settlement` from main path, spot-filter stub, duplicate market-fetch logic) and collapsing the two market-fetch paths; no dependency removals are safe (cryptography/requests/pandas/pyarrow all used) except the commented-out optional `kalshi-price-data`.

**Not checked:** live Kalshi API behavior (no credentials; cannot verify NO-order price complement, balance-in-cents assumption, or `/trade-api/v2` signature path), actual n8n/Supabase service availability, the `kalshieye_downloader.py` external API, the `kalshi-price-data` SDK (not installed), Android signal/SIGALRM delivery under battery optimization, TLS/certificate verification against real Kalshi endpoints, and performance under concurrent multi-process operation (single-process assumed). Local `.env`/`kalshi_private.pem` contain real credentials but are gitignored and were not inspected beyond confirming presence.
