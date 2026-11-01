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
