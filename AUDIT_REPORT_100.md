# Ponytail Audit — Kalshi SuperBot

**Repo:** /data/data/com.termux/files/home/kalshi_superbot
**Method:** ponytail-audit (ultra). Map → load → flows → trust boundaries → bugs → smallest fix.
**Load assumed:** one person, one process at a time, dry-run by default, single Android/Termux host. Live trading is opt-in (`--live` + `KALSHI_CONFIRM_LIVE=true`). n8n + Supabase are local/remote optional sidecars; the bot itself is a single Python loop on one handset.

## What this repo does

The Kalshi SuperBot is a single-operator live-trading bot for Kalshi 15-minute binary crypto markets. It runs a favour-maker limit-order strategy (two-sided YES/NO at 82–93c), scores signals with an LLM (7-layer fallback), writes signals/orders/bot-state to Supabase, and routes settlement P&L through n8n webhooks. Entry is via `trader.py` (dry-run default, `--live` for real money); supporting tools backtest, collect market data, and inject n8n workflows/credentials.

---

## MUST FIX

**1. trader.py:380-390 — Settlement PnL double-counted on replay after crash**
What this is: `_replay_settlement_queue()` re-sends queued settlements via n8n webhook and adds `pnl_dollars` to `state.session_pnl`. Problem: if the process crashes after the webhook succeeds and `session_pnl += pnl` runs, but before `_save_settlement_queue(remaining)` rewrites the file dropping the entry, the queue still contains the entry on restart. Replay then adds the same PnL a second time. Fix: remove the entry from the queue (rewrite the file) immediately after a successful webhook, before crediting `session_pnl`, or make the credit idempotent keyed by order_id. If we skip it: PnL is overstated after any crash mid-replay, and the Supabase reconciliation at startup (trader.py:840) cannot detect it because the bot's own bot_state is the source being reconciled.

**2. trader.py:465-466 — Position removed from state before settlement PnL is booked**
What this is: settled tickers are popped from `state.positions` and `state.position_risk` (lines 465-467) before the settlement price is fetched and PnL booked (lines 477-559). Problem: a crash or unhandled exception between the pop and the PnL booking loses the position from in-memory state and never books its realized PnL. The position is gone but the cash/risk bookkeeping never reflects its outcome. Fix: book PnL first (or record a "pending settlement" marker), then remove the position only after PnL is recorded or safely queued. If we skip it: positions and PnL silently diverge from the exchange; the bot's cash model drifts from reality.

**3. trader.py:708-726 — float contracts are truncated to int in the API order but dollar_risk keeps the float**
What this is: `sig.contracts` is a float (e.g. 6.5) from `_size()`. `place_order()` sends `count = str(int(contracts))` (kalshi_client.py:378) so the exchange receives 6 contracts, but `sig.dollar_risk = n * cost_per` (trader.py:151) and `resting_value[oid] = sig.dollar_risk` (trader.py:712) use the untruncated 6.5. Problem: cash reserved (resting_value) exceeds the actual order size; the intra-cycle cash model over-reserves, progressively shrinking `cash_room` and causing spurious WINDOW/CASH blocks. Fix: round contracts down to an int before computing dollar_risk/resting_value, then pass that int everywhere. If we skip it: cash accounting slowly drifts; the bot under-trades relative to its stated budget.

**4. trader.py:250-259 + main loop:877-898 — place_order failures are swallowed and never mark the resilience supervisor**
What this is: `_place_order` catches every exception, logs it, and returns `None`; the placement loop just `continue`s. The resilience `mark_failure()` / client-rebuild path (main loop) only triggers on *unhandled* cycle exceptions. Problem: an auth/401/409 error on placing orders is silently swallowed on every cycle; the supervisor never sees failures, never rebuilds the client, and the bot keeps looping placing nothing. Fix: let `place_order` propagate (or call `state.resilience.mark_failure(e)` inside `_place_order`) so repeated order failures trigger backoff/rebuild. If we skip it: after a key rotation or 401, the bot trades zero orders indefinitely while appearing healthy.

**5. latency_arb.py:99-106 — latency arb buys the overpriced side (direction inverted)**
What this is: `evaluate_market` triggers when `|mid - 50| - fee >= MIN_EDGE`. If `mid > 50` (YES overpriced vs 50c) it sets `side="yes"` and `entry_price=yes_ask` — i.e. buys YES at the ask, the *expensive* side. Problem: the comment claims it buys the cheaper/closer-to-50 side, but the code buys the dearer side; a mean-reversion arb should sell YES / buy NO when YES is above fair. This signal is converted to a real `TradeSignal` and placed by trader.py (lines 636-665). Fix: invert — when `mid > 50` buy NO at `no_ask`; when `mid < 50` buy YES at `yes_ask`. Or remove the latency path until the edge model is validated. If we skip it: latency-arb trades systematically buy high and lose.

**6. trader.py:445-447 & 796-799 — NO order price complemented by assumption, unverified against Kalshi**
What this is: both places assume a resting NO order's stored `price_cents` is the YES complement, so they compute `entry_cents = 100 - price_cents` for NO orders (lines 446-447, 798-799). Problem: if Kalshi stores the NO price directly (not complemented), `entry_cents` is wrong — e.g. a NO order stored as 85 (buy NO at 85c) is valued as 15c. `resting_value` under-reserves by ~70c/contract. Fix: verify the actual Kalshi representation (fetch a NO resting order and compare stored price vs intent), then remove the complement or apply it correctly; add an assert/log. If we skip it: cash tracking for NO resting orders is wrong, risking overspend or false blocks.

**7. supabase_client.py:189-232 — Supabase write failures return None silently; no alert on schema/4xx errors**
What this is: `_request` returns `None` on non-retryable 4xx errors (e.g. missing tables, bad columns) after recording a circuit-breaker failure; callers treat `None` as "skipped" with at most a debug log. Problem: if Supabase tables aren't created or a column is missing, every signal/order/state write is silently dropped — no audit trail, and startup reconciliation `get_bot_state("session_pnl")` returns None so PnL is never reconciled. Fix: surface repeated Supabase failures as a hard error at startup, or at minimum log a prominent warning once per circuit trip; consider failing closed when the audit trail is required. If we skip it: silent data loss; the operator believes trades are recorded when they are not.

**8. data_collector.py:110-116 — closed-market snapshots store negative minutes_left**
What this is: for markets whose `close_time` is in the past, `minutes_left = (ct - now)` is a large negative number, yet it is stored verbatim in the snapshot (line 151) while `minutes_in` is set to 15.0. Problem: downstream time-decay features / backtests receive `minutes_left = -525600` etc. — garbage features that corrupt any model trained on collected data. Fix: clamp `minutes_left` to 0 for closed markets (or store `minutes_in` only). If we skip it: collected snapshots are unusable for time-aware backtesting; lookahead/feature garbage.

**9. trader.py:917-931 — lock file relies on /proc/{pid} which is unreliable on Android/Termux**
What this is: single-instance lock checks `os.path.exists(f"/proc/{pid}")` to decide if a stale lock belongs to a live bot. Problem: on Android PIDs are recycled quickly and /proc may be restricted; a new bot can see a recycled PID and exit with "Bot already running", or fail to detect a real holder. Fix: use `fcntl.flock(LOCK_EX|LOCK_NB)` on the lock file (POSIX advisory, works in Termux) and drop the /proc check. If we skip it: spurious "already running" exits prevent starting the bot after a crash/reboot.

**10. signals.py:130-138 — module-level `_bid_hist` is in-memory, single-process, not persisted on clean shutdown**
What this is: bid-history state lives in a module dict and is saved to `bid_hist_{dry,live}.json` only every 30s or after a trade; there is no atexit/shutdown save. Problem: run the bot for 10 minutes collecting history with no trades, then kill it — the history is lost and the stability check re-warms for 3 minutes on the next start. Fix: call `_force_save_bid_hist()` in trader's shutdown/finally. If we skip it: stability gate re-warms every restart; trading is delayed by `STABILITY_LOOKBACK_MIN` minutes each launch. (shortcut: single-process only; fine unless load crosses to multiple processes.)

---

## SHOULD FIX

**11. trader.py:423-428 — a transient balance/positions failure skips the entire cycle**
What this is: if `cash == 0.0 and not positions and not DRY_RUN`, the cycle returns early. `get_balance` failure sets `cash=0.0` (line 411). Problem: a single transient balance fetch failure (network blip) with no positions fetched yet causes a whole cycle to be skipped; repeated blips stall trading. Fix: treat a balance fetch failure as "unknown" and skip only cash-dependent steps, not the whole cycle; or require both failures on consecutive cycles. If we skip it: the bot misses cycles on flaky Termux/mobile networks.

**12. market_finder.py:189-192 & 213-220 — only ONE main market is ever returned per cycle**
What this is: `find_active_markets` picks just the market closest to 50c across all series and returns it as the sole MAIN (plus hedges). Problem: other tradeable markets in other series are discarded even if they present better skew/edge; the bot can place at most one main trade per cycle regardless of `MAX_OPEN_POSITIONS`. Fix: return all mains passing the band/liquidity filter, ranked by edge, and let trader rank/signals decide. If we skip it: capital is concentrated in one market; the multi-series config (9 series) is mostly unused.

**13. market_finder.py:159-160 & 219-224 — MAIN selected on raw mid, then dropped after orderbook fetch**
What this is: the MAIN candidate is chosen using `mid` computed from raw API prices (line 155), but `_build` overrides bid/ask with orderbook data (line 78-81), and the real-price check (line 219) can then reject it ("MAIN failed real-price check"). Problem: the selected MAIN can be thrown away after selection, leaving zero mains for the cycle even though other candidates passed the raw filter. Fix: compute the orderbook-adjusted mid before selecting the best MAIN, or fall back to the next-best candidate. If we skip it: cycles with no MAIN market despite tradeable ones existing; missed entries.

**14. risk.py:43-55 — drawdown halt uses cost-basis exposure, not mark-to-market**
What this is: `update_balance` computes `total_value = cash + exposure` where `exposure` is position value at average cost (trader.py:561-565). Problem: an open losing position is still carried at cost, so `total_value` does not reflect the unrealized loss; the drawdown halt triggers later than it should (or not at all) while the position bleeds. Fix: value open positions at current bid/mid for drawdown purposes (requires a mark price). If we skip it: drawdown protection is delayed; larger realized losses before the halt fires.

**15. trader.py:508-523 & 839-845 — session_pnl drift between n8n settlement and Supabase bot_state**
What this is: realized PnL is added to `state.session_pnl` in-memory and written to Supabase bot_state each cycle (line 586-593); on startup it is reconciled *from* Supabase (line 840). But n8n is the system that actually records settlement to the orders table. Problem: if n8n records a settlement but the bot crashes before writing bot_state, Supabase `session_pnl` is stale; on restart reconciliation overwrites in-memory PnL with the stale value (undercount). Conversely replay can re-add (finding 1). Fix: make n8n write settlement PnL to Supabase bot_state (or a dedicated pnl table), and reconcile from that authoritative source; or treat n8n orders table as the PnL source of truth. If we skip it: session_pnl drifts from the exchange/n8n record after crashes.

**16. n8n_client.py:289-293 — `n8n_health_check` writes through the trade/settlement webhook**
What this is: health check calls `execute_webhook("superbot-trade", {"action": "health_check"})`. The `superbot-trade` workflow is a settlement processor (PATCH to Supabase orders). Problem: the health check has side effects (attempts a Supabase PATCH with missing order_id/status) and can return an error even when n8n is healthy — causing `trader.py` to refuse to start (`sys.exit(1)` at line 783). Fix: health check should hit a read-only endpoint (e.g. `GET /api/v1/workflows` or a dedicated health webhook that does not write). If we skip it: bot may fail to start purely because n8n's trade webhook dislikes a health-check payload.

**17. kalshi_client.py:191-208 — `_last_req` set before the 429 retry loop; bursts after rate limit**
What this is: `self._last_req = time.time()` is set before the retry loop, so on a 429 the sleep happens but subsequent retry attempts are not re-gated by `_MIN_REQUEST_GAP`. Problem: after backing off for a 429, retries can fire back-to-back, potentially re-triggering 429s. Fix: move the gap enforcement inside the retry loop or re-check the gap before each attempt. If we skip it: marginal; small increase in 429 recurrence on aggressive retries.

**18. config.py:189-199 — DRY_RUN default True but `.env` can silently flip it to false**
What this is: `DRY_RUN = _env_bool("DRY_RUN", True)`; `.env` (gitignored, operator-written) can set `DRY_RUN=false`. Problem: a copied/shared `.env` with `DRY_RUN=false` makes the bot trade real money on startup; the safety relies on the operator reading the warning comment. `validate_dry_run` blocks `--live` without confirmation, but a plain `python trader.py` with `DRY_RUN=false` in `.env` is not blocked (only `--live` path is gated). Actually `--live` without `KALSHI_CONFIRM_LIVE=true` is blocked; but `DRY_RUN=false` + no `--live` proceeds live unguarded. Fix: require `--live` flag whenever `DRY_RUN` is false (treat `DRY_RUN=false` as needing explicit `--live` + confirm), not just the `--live` branch. If we skip it: accidental real-money trading from a misconfigured `.env`.

**19. backtest_engine.py:64-150 — lookahead bias: `last_price` used as entry price**
What this is: `simulate_trade` derives `mid_proxy` from `last_price_dollars` (the price near settlement/close) and treats it as the entry price at the 6–11 min window. Problem: this is the terminal price that already embeds near-settlement information; the backtest buys at a price only observable late in the window, massively inflating win rate/EV. `backtest.py:15` even warns real backtesting needs orderbook snapshots. Fix: label results "proxy/lookahead-biased, not executable"; for real EV use `kalshieye_downloader` snapshots or the orderbook at entry minute. Do not use published EV to size live trades. If we skip it: backtest EV is not achievable live; parameter choices are based on inflated numbers.

**20. backtest_engine.py:170-180 — balance updated per trade in list order, not time order**
What this is: `run_backtest` iterates markets in the order fetched and updates `balance` after each trade. Problem: markets are not sorted by `close_time`, so the simulated bankroll path depends on arbitrary fetch order; PER_TRADE_PCT caps and drawdown are order-dependent. Fix: sort markets by close_time before simulating; compute drawdown on the time-ordered equity curve. If we skip it: backtest results are non-deterministic w.r.t. input order; drawdown/max-trade caps mis-modeled.

**21. backtest.py / parallel_backtest.py — default sweep is huge and slow on Android**
What this is: `parameter_grid(quick=False)` yields ~147k combos; `backtest.py` defaults to non-quick. Problem: on a single Android/Termux host this sweep can run for hours, tying up the device; `run_sweep` also pickles the full markets list into each worker (memory × jobs). Fix: default `--quick`, cap `jobs` to 1-2 on mobile, and add a `--max-combos` guard. If we skip it: default invocation is impractically slow on the target host.

**22. signals.py:202-211 — stability gate blocks all trades for `STABILITY_LOOKBACK_MIN` minutes on a fresh start**
What this is: `_stability_ok` returns False with "no history yet" until `_bid_hist` has `STABILITY_MIN_COVERAGE_MIN` of data. Problem: on first launch the bot trades nothing for the first ~3 minutes; by design but surprising to an operator expecting immediate entries. Fix: document clearly; optionally allow a warm-up bypass flag. If we skip it: not a bug, just delayed first trade. (shortcut: documented design.)

**23. fees.py:19 — `FEE_RATE` read from env with no range validation**
What this is: `FEE_RATE = float(os.environ.get("KALSHI_FEE_RATE", "0.07"))`. Problem: a misconfigured env (e.g. `0.7` for 70% or a negative) silently corrupts all fee/PnL math in `fees.py`. Fix: validate `0 <= FEE_RATE <= 1` at import with a clear error. If we skip it: bad env → silently wrong fees and settlement PnL.

**24. market_finder.py:196 — `MAX_HEDGE_PER_SERIES` default 0 disables hedges by default**
What this is: hedge slice is `all_hedges[:MAX_HEDGE_PER_SERIES * len(TARGET_SERIES)]` = `[:0]` when default 0. Problem: `HEDGE_YES_ASK_MAX` and the HEDGE_NO path never run out of the box; the hedge strategy is dead unless the operator raises the cap. Fix: either set a sane default (e.g. 1) or document that hedges are off by default. If we skip it: hedge strategy config is ineffective; users wonder why no hedge trades appear.

**25. trader.py:402 — calls private `signals._maybe_save_bid_hist` (tight coupling)**
What this is: trader reaches into signals' private module function. Problem: breaks encapsulation; refactoring signals risks breaking trader. Fix: expose a public `signals.tick(now)` or move bid-history saving into signals' `evaluate`. If we skip it: minor coupling smell, no functional impact.

**26. supabase_client.py:333-344 — `get_bot_state` returns None on invalid JSON without distinguishing "missing" vs "corrupt"**
What this is: `json.loads(rows[0]["value_json"])` failures are caught and return None. Problem: corrupt bot_state is indistinguishable from a missing key; reconciliation silently treats corrupt data as absent. Fix: log a warning on JSON decode failure and return a sentinel. If we skip it: corrupt state is silently ignored.

**27. kalshi_client.py:41-49 — `KALSHI_BASE_URL` validation hard-fails on any URL without `/trade-api/v2`**
What this is: constructor raises `ValueError` if base URL lacks `/trade-api/v2`. Problem: Kalshi demo/environments or future paths are rejected at startup even if they work. Fix: warn instead of hard-fail, or make the path prefix configurable. If we skip it: non-standard Kalshi endpoints cannot be used.

**28. trader.py:870-872 — `SIGALRM` cycle timeout can interrupt an in-flight `requests` call**
What this is: `signal.alarm(CYCLE_TIMEOUT_SECONDS)` raises `TimeoutError` anywhere in `cycle()`, including mid-HTTP-request. Problem: a socket/connection can be left half-open; the `requests` session may retain a broken connection. Fix: wrap per-call timeouts (already `timeout=` on requests) and rely on those instead of process-level SIGALRM, or close/replace the session on timeout. If we skip it: occasional stuck connections after long cycles; mild.

**29. add_n8n_credentials.py:117-126 — module-level side effects mutate the n8n DB on import**
What this is: the credential insert loop runs at import time (not guarded by `if __name__ == "__main__"`). Problem: `import add_n8n_credentials` anywhere (a test, a helper) writes to `/data/.../.n8n/database.sqlite`. Fix: guard the execution block behind `if __name__ == "__main__"`. If we skip it: accidental DB mutation during imports/testing.

**30. inject_n8n_workflows.py:26-27 & 385-386 — hardcoded n8n project/user IDs and Supabase secret stored in workflow JSON**
What this is: `PROJECT_ID` and `USER_ID` are hardcoded; `supabase_key` is written plainly into `workflow_entity` JSON via the settlement-processor webhook node. Problem: not portable across n8n instances; the Supabase service key sits in plaintext in the n8n SQLite DB. Fix: parameterize project/user IDs; store the key in n8n credential rather than workflow JSON. If we skip it: workflows only work for the original account; secret exposure if n8n DB is read by others.

**31. n8n_client.py — `run_*_webhook` convenience functions are dead code**
What this is: `run_backtest_webhook`, `run_collect_webhook`, `run_trade_webhook`, `run_monitor_webhook` (lines 263-280) are defined but `trader.py` calls `execute_webhook` directly. Problem: dead surface area. Fix: remove them or use them consistently. If we skip it: minor bloat.

**32. risk.py:148-158 — `reset_daily` does not clear `consecutive_losses`**
What this is: daily reset clears session_high and halt but not the consecutive-loss counter. Problem: a cooldown earned just before midnight carries into the next day. Fix: reset `consecutive_losses = 0` in `reset_daily`. If we skip it: minor; cooldown outlasts the day boundary.

---

## NICE TO HAVE

**33. tests/ — no integration tests for `cycle()`, settlement, market selection, or end-to-end signal→order flow**
What this is: test suite covers isolated units (fees, risk, signals helpers, client init) but not the main loop. Problem: the highest-risk path (money in/out) has no automated coverage. Fix: add tests for `cycle` settlement booking, replay double-count guard, and `_pre_trade_check` clamping using mocked client/n8n. If we skip it: regressions in the trading loop are caught only by live runs.

**34. latency_arb.py:129-173 — `simulate_settlement` is unused in the main flow**
What this is: defined but only invoked from `__main__`; trader places real latency-arb orders without simulating/validating settlement first. Problem: dead code and no pre-trade validation for the arb path. Fix: remove or wire a dry-run simulation before placing. If we skip it: unused function; no functional harm.

**35. supabase_client.py:285-303 — `update_order_settlement` is never called**
What this is: method exists but settlement goes through the n8n webhook, not this Supabase call. Problem: dead code; the orders table is only updated via n8n. Fix: remove or use it as the settlement write path (removing dependence on n8n for audit). If we skip it: dead method; settlement audit relies solely on n8n.

**36. signals.py:23-26 — `spot` module import always fails; `SPOT_FILTER_ENABLED` is dead config**
What this is: `import spot` wrapped in try/except sets `_spot_mod = None`; no `spot` module exists in the repo. Problem: `SPOT_FILTER_ENABLED` and `Z_MAX` have no effect; operators configure a filter that does nothing. Fix: remove the spot filter or implement the feed. If we skip it: config does nothing; minor confusion.

**37. kalshi_client.py:55-57 — `requests.Session` never closed**
What this is: session created in `__init__`, no `close()`. Problem: minor resource leak in a long-lived process; sockets not released on shutdown. Fix: add `close()` and call it in trader shutdown. If we skip it: negligible on a single short-lived process.

**38. market_finder.py:176 — `ThreadPoolExecutor(max_workers=len(TARGET_SERIES))` with a locked client serializes anyway**
What this is: 9 worker threads all share one `KalshiClient._lock` (0.12s min gap), so they are effectively serialized; threads add complexity without speed. Problem: concurrency overhead for no gain on the rate-limited client. Fix: fetch series sequentially (simpler) or give the client a pool. If we skip it: minor complexity; no correctness impact.

**39. parallel_backtest.py:48-93 & backtest.py:45-73 — duplicate market-fetch logic across two tools**
What this is: both implement pagination/fetch of settled markets separately. Problem: divergence risk (one fixes a bug, the other doesn't). Fix: share a single fetch helper. If we skip it: duplication; future fixes must be applied twice.

**40. kalshieye_downloader.py:213 — only `files[0]` is downloaded per date**
What this is: if multiple files exist for a date/series, only the first is downloaded. Problem: incomplete historical data. Fix: loop over all files. If we skip it: partial downloads; understated datasets.

**41. data_collector.py:13-12 — docstring says "dry-run trading" but collects closed historical markets**
What this is: tool actually fetches settled markets, not live dry-run snapshots. Problem: misleading documentation sets wrong expectations. Fix: update docstring. If we skip it: documentation mismatch only.

**42. config.py:63 — `_env_bool` silently returns the default for unparseable values**
What this is: `KALSHI_CONFIRM_LIVE=yes` works, but `KALSHI_CONFIRM_LIVE=maybe` returns the default (`False`) with no warning. Problem: a typo is silently treated as "not confirmed". Fix: warn on unrecognized bool strings. If we skip it: typos silently disable live confirmation (safe direction) or enable features unintentionally.

**43. tests/test_kalshi_price_data_client.py & tests/test_n8n_client.py are shallow**
What this is: they assert `isinstance(result, bool)` / `is None` without mocking network behavior. Problem: they don't exercise real code paths. Fix: add mocked request/response tests. If we skip it: weak coverage for optional clients.

**44. tests/ — no tests for `market_finder.find_active_markets` multi-series selection or `rank_signals`**
What this is: `rank_signals` and the full-market-selection flow are untested. Problem: ranking/order selection bugs surface only live. Fix: add unit tests. If we skip it: ranking regressions undetected.

**45. trader.py — no per-market or per-day loss limit beyond drawdown and consecutive losses**
What this is: risk caps are portfolio-level; a single market can lose repeatedly before the 15% drawdown halt. Problem: a choppy market can bleed the account slowly. Fix: add per-market daily loss cap. If we skip it: slower bleed before halt triggers.

**46. backtest_engine.py:258-255 — `parameter_grid` non-quick includes `MAX_NO_ENTRY_CENTS` but `Simulate_trade` ignores `MIN_SKEW`/`MIN_TREND`/`ENTRY_WINDOW`**
What this is: the sweep tests params the engine doesn't fully apply (skew/trend/window are config constants, not StratParams). Problem: parameter combos that vary ignored values produce identical results; wasted compute. Fix: align engine params with grid, or drop ignored keys from grid. If we skip it: wasted backtest compute on no-op dimensions.

**47. kalshi_price_data_client.py:133-142 — `health_check` calls `_client.health_check()` which may not exist on the SDK**
What this is: if the installed SDK lacks `health_check`, `AttributeError` is caught and returns False. Problem: silent failure mode. Fix: document SDK version requirement. If we skip it: health check reports False without clarity why.

**48. add_n8n_credentials.py / inject_n8n_workflows.py — credentials/keys passed on command line or in env visible in process list**
What this is: API keys passed via CLI args/env are visible to other processes on the host. Problem: on a shared Android device, another app could read `/proc/*/environ`. Fix: use files with 0600 perms (already supported for n8n key file) and discourage CLI keys. If we skip it: credential exposure to co-located processes.

**49. config.py:233-339 — `validate_credentials`/`validate_dry_run`/`validate_bands` are not invoked on import; only trader calls them**
What this is: other entry points (backtest.py, data_collector.py) construct `KalshiClient()` directly and hit auth errors with less-clear messages. Problem: inconsistent startup validation across tools. Fix: call `validate_credentials()` in any tool that builds a KalshiClient. If we skipit: backtest/data_collector fail with opaque auth errors instead of actionable config guidance.

**50. resilience.py:44 — backoff starts at `base_backoff` (5s) on the first failure**
What this is: first cycle error already sleeps 5s before retrying. Problem: on flaky networks every transient error costs 5s; cycles slow. Fix: first failure delay 0 (retry immediately), backoff from the second. If we skip it: 5s penalty per transient error; reduced throughput on mobile networks.

**51. kalshi_client.py:265 — `get_markets` cache keyed only by (series, status, limit); no TTL invalidation on market state change**
What this is: 30s TTL; if a market closes within the window it may still appear as open. Problem: stale market list used for entries. Fix: acceptable at 30s for this load; documented. If we skip it: minor staleness; (shortcut.)

**52. trader.py:586-593 — `set_bot_state_batch` writes every cycle even when nothing changed**
What this is: unconditional Supabase write each cycle. Problem: unnecessary traffic when Supabase is enabled. Fix: skip write if values unchanged. If we skip it: minor extra requests; negligible at 15s cycles.

**53. `historical_data/` and `hf_data/` contain large binary/parquet artifacts tracked in repo**
What this is: `historical_data/*.jsonl.gz`, `kalshi_btc_features.parquet`, `hf_data/*.parquet` are in the working tree (some gitignored, some not). Problem: repo bloat; parquet may contain training data that shouldn't be versioned. Fix: add to .gitignore / LFS. If we skip it: repo size grows; (not a code bug.)

**54. `bot.log` and `*_result.json` files present in working tree**
What this is: runtime artifacts sit in the repo root. Problem: clutter; some are gitignored but exist on disk. Fix: clean up; ensure all runtime output goes to gitignored paths. If we skip it: cosmetic.

**55. `llm_eval.py:324-332 — system prompt tells model to return ONLY two keys but response parsing accepts extra keys`
What this is: contract mismatch between prompt (2 keys) and parser (tolerates extra). Problem: not a bug, but parser leniency means the model contract is not enforced. Fix: either enforce 2 keys or update prompt. If we skip it: no functional impact.

**56. `parallel_backtest_hf.py:77-85 — NO branch uses `100 - mid_proxy` but never checks `MIN_SKEW`/trend gates`
What this is: HF backtest engine differs from `backtest_engine` and from live `signals.py` (no skew/trend/stability). Problem: best HF params don't transfer to live. Fix: align or label as a different strategy. If we skip it: HF results not applicable to live trading.

**57. `config.py:143-144 — `CASH_FLOOR_PCT=0.70` and `EXPOSURE_CEILING_PCT=0.30` sum to 1.0, leaving no buffer**
What this is: floor + ceiling exactly partition the portfolio. Problem: rounding in `max_trade_dollars` can permit a trade that pushes exposure just over ceiling or cash just under floor due to epsilon. Fix: add a small buffer or make ceiling = 1 - floor with epsilon handled consistently. If we skip it: marginal edge-case breaches of cash floor/exposure ceiling.

**58. `signals.py:46-50 — `TradeSignal.is_trade` allows `direction != PASS` even when `side` is empty string**
What this is: a PASS signal has `side=""`; `is_trade` checks `direction != "PASS"` so it's fine, but a malformed signal with direction set and side="" would pass. Problem: defensive gap if a code path sets direction without side. Fix: also require `self.side in ("yes","no")`. If we skip it: unlikely malformed signal could reach `place_order` and be rejected by the API anyway.

**59. `trader.py:669-670 — `ranked` may be empty after latency signals are added only if no signals; OK`
What this is: `rank_signals` returns only tradeable signals; if none, early return. Problem: none. If we skip it: n/a (correct.)

**60. `supabase_client.py:72-140 — credential loading order is opaque (file > env > .env fallback partially)**
What this is: URL and key loading from `SUPABASE_KEY_FILE` and env is interleaved and hard to follow. Problem: operator may not know which source won. Fix: log the resolved source at init. If we skip it: debugging credential issues is harder.

---

## Verdict

The code is well-structured and the critical-path money code (fee math, settlement PnL, risk caps) is deliberately careful — but there are real integrity risks: settlement PnL can be double-counted on replay crash, positions can be dropped before PnL is booked, float contracts are truncated in the API order while reserving cash for the float, order failures are swallowed so the resilience supervisor never rebuilds on auth errors, and the latency-arb path buys the overpriced side. Supabase writes fail silently, so the audit trail can be empty without the operator knowing. These are must-fix before any live run. The load assumption (single process, dry-run default, one Android host) means concurrency and scale issues are mostly shortcuts, not findings.

**Lean:** roughly -60 lines and -1 dependency possible by deleting dead code (`update_order_settlement`, `run_*_webhook` convenience fns, `simulate_settlement` from main path, spot-filter stub, duplicate fetch logic) and collapsing the two market-fetch paths; no dependency removals are safe (cryptography/requests/pandas/pyarrow are all used) except the commented-out optional `kalshi-price-data`.

**Not checked:** live Kalshi API behavior (no credentials; cannot verify NO-order price complement, balance-in-cents assumption, or `/trade-api/v2` signature path), n8n workflow execution (n8n not running), Supabase schema (no tables to query against), Android signal/SIGALRM delivery under battery optimization, TLS/certificate verification against real Kalshi endpoints, and the `kalshi-price-data` / `kalshieye` external services (not installed/available).