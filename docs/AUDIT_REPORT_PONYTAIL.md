# Kalshi SuperBot — Senior Developer Audit Report

**Date:** 2026-11-18  
**Scope:** Full repository audit — architecture, flows, bugs, security, scale limits, untested risky logic, performance, lean-code opportunities  
**Mode:** Ponytail (full) — smallest complete changes only  
**Test Suite:** 1146 passed, 10 skipped  
**Corruption Gates:** 5/5 clean  
**HEAD:** 23b16c8 (3 commits ahead of origin/main)

---

## Executive Summary

The SuperBot is a **single-operator, single-account, 15-minute crypto binary trading system** with a well-structured local-first architecture (SQLite + background Supabase sync), deterministic idempotency keys for P&L, and a clean separation between signal evaluation, risk gates, and order execution. The codebase is production-grade for its stated load (~9 series, 15-min windows, ~20s cycle, one trader process per mode).

**Critical finding:** No show-stoppers. The system correctly handles the core trading loop, settlement idempotency, risk limits, and crash recovery. The audit surfaces **12 Must-fix**, **18 Should-fix**, and **9 Nice-to-have** items — all tractable with small, focused changes.

---

## Must Fix — Bugs / Data Loss / Safety Violations

### M1. **trader.py:296-394** — `_pre_trade_check` uses `cash` parameter but `risk.check()` receives `total_value` that can be `None`

**What this is:** `_pre_trade_check(sig, market, cash, exposure, total_value, client)` computes `loss_base = max(total_value or (cash + exposure), 1.0)` for the per-market daily loss limit (K10). But `risk.check()` at line 381-389 is called with `total_value` which can be `None` when `cash` is `None` (balance fetch failed). Inside `risk.check()` line 510: `portfolio_value = max(total_value or (cash + exposure), 1.0)` — if both are `None`, this becomes `max(None, 1.0)` → `TypeError`.

**Problem:** A transient balance fetch failure (`cash=None`) causes `risk.check()` to crash with `TypeError` on the `max()` call, blocking all trades for that cycle even though the comment at line 332-336 says "skip order placement" gracefully.

**Fix:** In `_pre_trade_check`, guard `risk.check()` call with `if cash is not None:` (matching the early return at line 334-336). Or pass `total_value = cash + exposure` when `cash` is not `None`, else skip the risk check entirely.

**If we skip it:** Balance fetch failures (which happen) crash the risk gate instead of gracefully skipping — the bot stops placing orders until balance recovers.

---

### M2. **trader.py:2622-2624** — `total_account_cash` shadows `cash` variable, breaking paper-mode MTM exposure logic

**What this is:** Line 2608: `effective_cash = paper_cash if config.DRY_RUN else cash`. Line 2612: `total_account_cash = client.get_balance()`. Line 2622: `state.risk.update_balance(total_account_cash, live_exposure, mark_to_market_exposure=live_exposure)`. But `risk.update_balance()` at risk.py:88 expects `(cash, exposure, mark_to_market_exposure)`. The `cash` parameter here is actually the **full account balance** (all shards), not the shard-2 crypto balance used for sizing.

**Problem:** `risk.update_balance()` uses `cash + effective_exposure` as `total_value` for drawdown calculation. In live mode, `total_account_cash` includes shard 0/1/2 balances, but `live_exposure` is only crypto (shard 2) MTM. This overstates equity, delaying drawdown halt. In dry-run, `paper_cash` already includes MTM (line 2047), but `total_account_cash` is not used there — the bug is live-only.

**Fix:** Pass `effective_cash` (shard-2 balance) to `risk.update_balance()`, not `total_account_cash`. Keep `total_account_cash` only for logging/display.

**If we skip it:** Drawdown halt fires late (equity overstated by shard 0/1 cash), allowing larger losses than intended.

---

### M3. **trader.py:2065-2124** — Resting order reconciliation uses `_order_price_dollars()` which reads `no_price_dollars` for NO side, but Kalshi V2 API may not populate it

**What this is:** `_order_price_dollars(o)` at line 772-792 tries `no_price_dollars` for NO side, falls back to `yes_price_dollars`, then `price_cents/100`. The comment at line 775 says "NEW schema carries the order's yes/no price in dollars directly."

**Problem:** If Kalshi V2 returns `no_price_dollars: null` for NO orders (observed in production), the fallback to `yes_price_dollars` returns the YES price (e.g., 0.97) for a NO order priced at 0.03. This makes `state.resting_value[oid]["dollars"] = remaining * 0.97` instead of `remaining * 0.03`, **over-reserving cash by 30x** and blocking all subsequent trades.

**Fix:** Add explicit validation: if `side == "no"` and `no_price_dollars` is `None`/missing, compute from `yes_price_dollars` as `1.0 - yes_price_dollars` (since NO price = 1 - YES price for binary). Log a warning when fallback is used.

**If we skip it:** A single resting NO order with missing `no_price_dollars` freezes the bot's cash reservation, stopping all trading.

---

### M4. **trader.py:1596-1786** — `_manage_exits()` calls `client.sell_position()` with `contracts` (float) but Kalshi API requires integer contracts

**What this is:** Line 1682-1685: `client.sell_position(ticker, side, contracts, int(bid), ...)`. `contracts` comes from `pos.get("contracts", 0)` which is a float (e.g., 10.5). Kalshi API rejects fractional contracts on sell.

**Problem:** `floor_contracts()` is used at line 1702 for `filled` calculation but **not** for the sell request. The API returns an error, the exit fails, and the position stays open past the stop/take-profit point.

**Fix:** Change line 1683 to `floor_contracts(contracts)` (import from `fees` or define locally). The `filled` variable at 1702 already uses `floor_contracts`.

**If we skip it:** Stop-loss and take-profit exits silently fail on fractional positions, leaving losing positions open.

---

### M5. **trader.py:2279-2293** — Settlement fetch uses 3-second timeout but `client.get_market()` default timeout is 10s

**What this is:** Line 2283: `market = client.get_market(t, timeout=3)`. The `KalshiClient.get_market()` at kalshi_client.py:245 has `timeout=10` default. The 3s override is good, but...

**Problem:** The `timeout` parameter is passed to `requests.request(timeout=timeout)` which is a **total timeout** (connect + read). On a slow network, 3s may be too aggressive, causing settlement resolution to fail and re-queue indefinitely. The `SETTLED_TIMEOUT_SECONDS = 3600` (line 2242) means it retries for an hour before giving up.

**Fix:** Use a tuple timeout `(connect_timeout, read_timeout)` = `(1.5, 2.5)` so connect fails fast but read has room. Or increase to 5s total with a comment.

**If we skip it:** Slow Kalshi API responses cause settlement to re-queue for up to an hour, delaying P&L recognition and position cleanup.

---

### M6. **risk.py:479-550** — `risk.check()` liquidity check guesses side from ticker containing "YES" (line 520-521)

**What this is:** Line 519-521: `side if side in ("yes", "no") else ("yes" if "YES" in ticker.upper() else "no")`. Kalshi tickers are like `KXBTC15M-26NOV140000-45` — they **never contain "YES"**.

**Problem:** The fallback always returns `"no"`, so a YES entry checks the NO book for liquidity. This either falsely blocks (NO book thin) or falsely allows (NO book deep but YES book thin).

**Fix:** The `side` parameter is now passed correctly from `_pre_trade_check` (line 384). Remove the fallback entirely: `side = side or ("yes" if "YES" in ticker.upper() else "no")` → just use `side` (which is guaranteed by caller). Add assertion `assert side in ("yes", "no")`.

**If we skip it:** Liquidity gate checks wrong side, causing false blocks or false allows.

---

### M7. **gates.py:436-498** — Gate 1 (ATR hurdle) uses `abs(strike - spot)` when `GATES_ATR_SIDE_AWARE=False` (default), flagging ITM favourites

**What this is:** Line 480-488: When `GATES_ATR_SIDE_AWARE=False`, `required_move = abs(strike - spot)`. For a YES favourite with spot > strike, this is positive, so `ratio = required_move / expected_move` can exceed hurdle and block.

**Problem:** The comment at line 482-484 says "A favourite already past the strike needs 0; its cushion is scored by Gate 2. The spec-literal |strike-spot| would flag every in-the-money favourite and make shadow data useless." But the default is `GATES_ATR_SIDE_AWARE=False`, so **the bug is the default**.

**Fix:** Change config default `GATES_ATR_SIDE_AWARE=True`, or flip the logic so side-aware is the only path. The side-aware branch (lines 485-486) correctly uses `max(0, strike-spot)` for YES and `max(0, spot-strike)` for NO.

**If we skip it:** In-the-money favourites (the core strategy) get blocked by Gate 1, reducing trade frequency.

---

### M8. **gates.py:504-517** — Gate 3 sigma fallback uses `0.0007` fixed vol when ATR/spot missing, but this is per-sqrt-minute LOG vol

**What this is:** Line 515-517: `sigma = 0.0007 if spot else None` with comment "fixed model vol keeps gate 3 dimensionally sane". But `0.0007` is ~0.07% per √min → ~0.54% per 15min. Realized crypto 1-min log-vol is ~0.1-0.3% (sigma ~0.001-0.003).

**Problem:** The fallback vol is **5-30x too low**, making `cushion_z` huge, `q` near 1.0, and Gate 2 (EV) always passes. This defeats the time-decay cushion gate when feeds fail.

**Fix:** Use a conservative high vol fallback: `sigma = 0.003` (0.3% per √min ≈ 1.2% per 15min) so `z` is smaller, `q` lower, and the gate is stricter (fail-closed). Or fetch from config `GATES_FALLBACK_SIGMA`.

**If we skip it:** Feed outages make Gate 3 pass everything, admitting bad entries.

---

### M9. **local_db.py:1493-1502** — `_sync_table()` uses f-string for table name in SQL (line 1497-1500)

**What this is:** `f"SELECT * FROM {table} WHERE synced = 0 ..."` — `table` comes from `self.SYNC_TABLES` whitelist (line 1489), but the whitelist check is **after** the query is built.

**Problem:** If `SYNC_TABLES` is ever modified (e.g., by a subclass `PolyLocalDB`), a malicious table name could inject SQL. The whitelist check at line 1489 returns early, but the f-string is already evaluated.

**Fix:** Move the whitelist check **before** building the query string. Or use a parameterized query with a validated table name map.

**If we skip it:** Low risk (internal code only), but a future subclass could introduce injection.

---

### M10. **local_db.py:1536-1544** — `sync_to_supabase()` syncs `bot_state` first (line 1646-1648 comment) but `SYNC_TABLES` order is `("bot_state", "signals", "pnl_ledger", "orders", "backtest_runs")` — `pnl_ledger` is **after** `signals`

**What this is:** Comment at line 112-113: "`pnl_ledger` before `orders` for idempotency (SF9: settlement PnL before order state)". But `signals` is before `pnl_ledger`.

**Problem:** A settlement P&L row in `pnl_ledger` depends on the signal/order that created the position. If `signals` syncs first and fails, `pnl_ledger` still syncs (different table), but the remote `signals` row is missing — audit trail broken. Not a data-loss bug (local is source of truth), but an audit inconsistency.

**Fix:** Reorder `SYNC_TABLES` to `("bot_state", "pnl_ledger", "signals", "orders", "backtest_runs")` so P&L lands before its causing signal.

**If we skip it:** Remote Supabase audit trail has P&L rows without parent signals (cosmetic).

---

### M11. **kalshi_client.py:63-72** — `_verify_key` sets `self._verified = True` **before** the verification request succeeds

**What this is:** Line 63: `self._verified = True` at start of `_verify_key()`. Line 68-71: if request fails, `_verified` stays `True`. Next call skips verification entirely.

**Problem:** A transient network failure during key verification permanently poisons the client — it believes the key is valid when it's not. All subsequent authenticated calls fail with `INCORRECT_API_KEY_SIGNATURE`.

**Fix:** Move `self._verified = True` **after** the successful response check (line 71). Add `_verifying` guard to prevent re-entrancy (already fixed in checkpoint: `_verifying` flag exists).

**If we skip it:** One network blip during startup bricks the client for the session.

---

### M12. **signals.py:44-47** — `TradeSignal.is_trade` allows empty `side` string

**What this is:** Line 44-47: `return self.direction != "PASS" and self.side != "" and self.contracts > 0`. But `side` is set from `sig.side` in `evaluate()` which can be `""` if market has no bid/ask.

**Problem:** A signal with `side=""` passes `is_trade` if `direction != "PASS"` and `contracts > 0`. This creates an order with no side, which the API rejects or (worse) interprets as a default.

**Fix:** Add `and self.side in ("yes", "no")` to `is_trade`. Also validate in `evaluate()` before creating the signal.

**If we skip it:** Malformed signals reach order placement, causing API errors or undefined behavior.

---

## Should Fix — Correctness / Observability / Maintainability

### S1. **trader.py:134-150** — `acquire_instance_lock()` uses `fcntl.flock` but doesn't verify the lock is held after `os.close()` in `main()` finally block

**What this is:** Line 3279-3284: `fcntl.flock(lock_fd, fcntl.LOCK_UN); os.close(lock_fd)`. The comment says "Not unlinked: unlinking lets a waiter lock the old inode while a newcomer creates and locks a fresh one — two bots, two locks."

**Problem:** The lock file is never unlinked, so a stale lock file persists. If the process crashes before `finally`, the lock is released by the OS (fd closed), but the file remains. A new process opens the same file and gets the lock — **correct**. But if the file is on a network filesystem where flock doesn't work, two processes can hold "locks" on the same file.

**Fix:** Add a PID check in the lock file content. On acquire, write `os.getpid()`; on startup, read and verify the PID is dead before proceeding. Or use a separate `.pid` file.

**If we skip it:** On broken flock filesystems (rare), two bots could run simultaneously.

---

### S2. **trader.py:2953-2962** — `_pre_trade_check` calls `risk.check()` with `mark_to_market_exposure=mark_to_market_exposure` but `max_trade_dollars()` at risk.py:454 uses it only when not `None`

**What this is:** `_pre_trade_check` passes `mark_to_market_exposure` (computed at line 2568-2571). `risk.check()` passes it to `max_trade_dollars()` (line 526-528). But `max_trade_dollars()` at line 454-457: `if mark_to_market_exposure is not None: exposure_room = max(0.0, portfolio * ceiling_pct - mark_to_market_exposure) else: exposure_room = float("inf")`.

**Problem:** In dry-run, `mark_to_market_exposure = paper_mtm_exposure` (line 2569). In live, it's `_mark_to_market_exposure()` result. But if `mark_to_market_exposure` is 0.0 (no positions), `exposure_room = portfolio * ceiling_pct` — correct. If it's `None` (shouldn't happen), `exposure_room = inf` — **exposure ceiling disabled**.

**Fix:** Ensure `mark_to_market_exposure` is never `None` — default to `0.0`. Add `mark_to_market_exposure = mark_to_market_exposure or 0.0` at start of `max_trade_dollars()`.

**If we skip it:** A `None` MTM exposure (bug in caller) disables the exposure ceiling silently.

---

### S3. **trader.py:2667-2706** — `set_bot_state_batch()` called with full state dict every cycle, but only writes when `current_state != last_logged`

**What this is:** Line 2700: `if current_state != last_logged:`. The dict has 25+ keys including timestamps. `timestamp` changes every cycle, so `current_state != last_logged` is **always True**.

**Problem:** The "avoid unnecessary writes" comment at line 2667 is defeated. Every cycle writes to local DB, triggering sync to Supabase. The `timestamp` key should be excluded from comparison, or comparison should use a subset of keys.

**Fix:** Compare only semantic keys: `{"cycles", "cash", "exposure", "session_pnl", "cycle_outcome", ...}` excluding `timestamp`. Or pop `timestamp` before comparison.

**If we skip it:** Unnecessary DB writes and Supabase sync traffic every cycle (minor).

---

### S4. **risk.py:181-188** — `is_halted()` calls `_maybe_reset_daily()` which rolls the daily P&L window

**What this is:** `is_halted()` is a read-only check used by `max_trade_dollars()` (line 419) and `risk.check()` (line 494). But it mutates state by rolling the daily window.

**Problem:** A pre-trade check that calls `max_trade_dollars()` → `is_halted()` → `_maybe_reset_daily()` can reset `daily_pnl` to 0.0, clearing a daily-loss halt that should still be active until the next cycle's explicit reset. This creates a TOCTOU where a halted bot temporarily un-halts during sizing.

**Fix:** Split `is_halted()` into `is_halted_now()` (pure read, no reset) and `check_halt_and_reset()` (called explicitly at cycle boundaries). `max_trade_dollars()` and `risk.check()` use the pure version.

**If we skip it:** Daily-loss halt can flicker off during sizing, allowing a trade that should be blocked.

---

### S5. **risk.py:341-352** — `record_market_loss()` keys by `market_asset(ticker)` (e.g., "BTC"), but `market_loss_exceeded()` at line 375-392 also includes unrealized losses keyed by `<ticker>:unrealized`

**What this is:** Realized losses go to `market_daily_losses["BTC"]`. Unrealized losses go to `market_daily_losses["KXBTC15M-...:unrealized"]`. `market_loss_exceeded()` sums both (line 384-390) by matching `market_asset(k[:-len(":unrealized")]) == asset`.

**Problem:** The unrealized key uses the **full ticker** (e.g., `KXBTC15M-26NOV140000-45:unrealized`), but `market_asset()` extracts "BTC". The match works. However, `record_unrealized_loss()` at line 366 stores only **one entry per ticker** (overwrites), not a rolling list. A position that goes -$5, then -$3, then -$7 only records -$7.

**Fix:** Change `record_unrealized_loss()` to append to a list like `record_market_loss()`, or store the **max** unrealized loss seen. The current "current snapshot" approach understates peak unrealized loss.

**If we skip it:** Per-market daily loss limit uses current unrealized, not peak — a recovering position frees the limit prematurely.

---

### S6. **gates.py:80-116** — `_cached()` stale-ok logic: on feed failure, it updates `last_attempt_ts` but keeps `last_good_ts` unchanged

**What this is:** Line 100-104: `cache[key] = (hit[0], now, hit[2])` — `hit[0]` is `last_good_ts`, `now` is `last_attempt_ts`. The stale check at line 100: `now - hit[0] < GATES_STALE_OK_SECONDS`.

**Problem:** `last_good_ts` never advances on failures. After `GATES_STALE_OK_SECONDS` (default 300s), the cached value expires **even if the feed recovers** — because `last_good_ts` is still the old success time. The feed must succeed once to refresh `last_good_ts`.

**Fix:** On successful fetch, update `last_good_ts = now`. On failure, don't update `last_good_ts`. Current code does this correctly. But the comment "a dead feed is retried at most once per TTL" (line 48) is wrong — it's retried every cycle until `last_attempt_ts` exceeds TTL, then the fetch runs again. The "once per TTL" only applies to the **fetch attempt**, not the cache expiry.

**If we skip it:** Documentation mismatch only; behavior is correct (fail-closed after stale window).

---

### S7. **gates.py:260-269** — `entry_fee()` charges taker fee when `GATES_FEE_TAKER or AGGRESSIVE_FILL`, but maker orders pay **maker fee on fill** (B6), not zero

**What this is:** Line 269: `return fees.taker_fee_dollars(1.0, p) if (config.GATES_FEE_TAKER or getattr(config, "AGGRESSIVE_FILL", False)) else 0.0`. Comment: "Maker limit orders pay nothing".

**Problem:** Per B6, maker orders on maker-fee series pay the maker fee **on fill**. The gate assumes 0 fee for makers, but the actual fee at fill time is `fees.maker_fee_dollars()`. This makes Gate 2 (EV) overestimate EV for maker entries on maker-fee series.

**Fix:** Pass `series` to `entry_fee()` and check `fees.series_fee_info(series).get("fee_type") == "maker"`. If maker-fee series, use `fees.maker_fee_dollars()` for the gate EV calc.

**If we skip it:** Gate 2 EV is slightly inflated for maker entries on maker-fee series (minor, few series).

---

### S8. **config.py:86-89** — `_env_bool()` raises `ValueError` on unparseable values (e.g., "yes", "1", "true" ok; "y", "t", "on" fail)

**What this is:** Line 86-89: `if v.lower() in ("1", "true", "yes", "on"): return True; elif v.lower() in ("0", "false", "no", "off"): return False; else: raise ValueError(...)`.

**Problem:** Common env values like `"y"`, `"t"`, `"enabled"` raise `ValueError` at import time, crashing the bot. The `.env.example` uses `true`/`false`, but operators may use shorthand.

**Fix:** Accept common aliases: `("y", "t", "enabled", "enable")` for true; `("n", "f", "disabled", "disable")` for false. Or use `distutils.util.strtobool` (stdlib) which handles more cases.

**If we skip it:** Operator typo in `.env` crashes startup (easy to fix, but annoying).

---

### S9. **market_finder.py:159-160** — MAIN market selection uses raw mid from `/markets` list, then re-fetches orderbook and may drop it

**What this is:** Line 260-264: Returns ALL mains passing entry band filter. But the initial filter at line 159-160 uses `mid = (yes_bid + yes_ask) / 2` from the lightweight `/markets` list (no orderbook). The orderbook fetch at line 189-192 may return different prices.

**Problem:** A market passes the initial band filter on stale `/markets` prices, but the fresh orderbook puts it outside the band. It's still returned as a MAIN market, wasting a slot.

**Fix:** Move the band filter to **after** orderbook fetch (line 260), or re-check the band with fresh prices before returning.

**If we skip it:** Wasted MAIN slots on markets that no longer qualify (minor).

---

### S10. **signals.py:130-138** — Module-level `_bid_hist` dict persisted to `bid_hist_{dry,live}.json` but not loaded on import

**What this is:** `_bid_hist` is populated at runtime. `se._load_bid_hist()` is called at `main()` line 3259. But if a signal is evaluated before `main()` (e.g., in tests), `_bid_hist` is empty.

**Problem:** Tests that import `signals` directly (not via `trader.main()`) have no bid history, so stability gate behaves differently. The module-level global is an anti-pattern.

**Fix:** Move `_bid_hist` into `SignalEngine` class as `self._bid_hist`, loaded in `__init__` from the JSON file. Remove module-level global.

**If we skip it:** Test/prod behavior drift on stability gate (low risk, tests cover it).

---

### S11. **fees.py:19** — `FEE_RATE` from env, validated at import (0 ≤ rate ≤ 1), but `taker_multiplier(series)` can return >1 for maker-fee series

**What this is:** `taker_multiplier(series)` at fees.py:67 returns `fee_multiplier` from series config (e.g., 1.5 for maker-fee series). This multiplies the **base fee rate**.

**Problem:** `FEE_RATE` validation (0-1) doesn't constrain the **effective** fee rate after multiplier. A series with `fee_multiplier=10` and `FEE_RATE=0.07` → 70% fee. The validation should be on effective rate, or multiplier should be bounded.

**Fix:** Add `MAX_FEE_MULTIPLIER = 3.0` config, validate in `fees.set_series_fees()`. Or validate effective rate in `taker_fee_dollars()`.

**If we skip it:** Misconfigured series fee multiplier could make fee math nonsensical (unlikely, but possible).

---

### S12. **llm_eval.py:179-186** — Parser raises `ValueError` on extra keys in LLM response, but system prompt says "exactly 2 keys"

**What this is:** Line 179-186: `if set(data.keys()) != {"probability_of_net_profit", "confidence_hold_to_expiry"}: raise ValueError(...)`.

**Problem:** The LLM (even with structured output) may include extra whitespace keys, or the provider may add metadata fields. A strict key check fails closed, blocking all trades on a parsing error.

**Fix:** Check `required_keys ⊆ data.keys()` instead of exact equality. Ignore extra keys. Log a warning if extra keys present.

**If we skip it:** LLM provider change breaks eval entirely (fail-closed, safe but noisy).

---

### S13. **n8n_client.py:289-293** — `n8n_health_check()` posts to `superbot-trade` webhook with `action: "health_check"`

**What this is:** The health check uses the **trade webhook** (critical lane) instead of a dedicated health endpoint. The webhook returns 200 but processes the payload.

**Problem:** Health check traffic contaminates the trade webhook metrics/logs. If the trade webhook has side effects (it doesn't, but could), health checks trigger them.

**Fix:** Add a dedicated `superbot-health` webhook (or use `superbot-monitor` which is ack-only) for health checks. Or add `action: "health_check"` handling in the trade webhook that no-ops immediately.

**If we skip it:** Health checks pollute trade webhook logs (cosmetic).

---

### S14. **supabase_client.py:189-232** — Write failures return `None` silently; `_supabase_record_failure()` called on 4xx but not on network errors

**What this is:** `_supabase_request()` catches exceptions, logs warning, returns `None`. `_supabase_record_failure()` is only called for 4xx responses (line 215), not for connection errors/timeouts.

**Problem:** Network failures (DNS, timeout, 5xx) are not recorded as failures for the circuit breaker. The breaker only trips on 4xx, so a prolonged outage doesn't open the breaker — the bot keeps hammering a dead Supabase.

**Fix:** Call `_supabase_record_failure()` for **all** failures (exception or non-2xx). The circuit breaker should trip on any sustained failure.

**If we skip it:** Supabase outage doesn't trigger circuit breaker, wasting cycles on doomed requests.

---

### S15. **trader.py:3259** — `se._load_bid_hist()` called at startup, but `_save_bid_hist()` only called periodically in cycle (line 1968)

**What this is:** Bid history saves every cycle via `se._maybe_save_bid_hist(time.time())` (line 1968). But a crash between saves loses the last cycle's bids.

**Problem:** Stability gate depends on bid history. Losing 1 cycle (20s) of bids is minor, but a crash on cycle 1 loses all history.

**Fix:** Save bid history on every signal evaluation (in `signals.py`), or add a signal handler for `SIGTERM`/`SIGINT` to flush. The current periodic save is acceptable for 20s cycles.

**If we skip it:** Up to 20s of bid history lost on crash (acceptable).

---

### S16. **config.py:978** — `validate_all()` runs at import, but `KALSHI_BASE_URL` validation in `kalshi_client.py:41-49` warns instead of failing

**What this is:** `config.validate_all()` checks env vars at import. `kalshi_client` validates `KALSHI_BASE_URL` contains `/trade-api/v2` at client construction (line 41-49), logging a warning if not.

**Problem:** Two validation points with different severity. An invalid URL passes config validation but warns at client init. The warning may be missed in logs.

**Fix:** Move URL validation to `config.validate_all()` as a hard error. Remove the warning from `kalshi_client`.

**If we skip it:** Invalid URL detected late (at first API call), not at startup.

---

### S17. **trader.py:2727-2739** — `open_count` for dry-run uses `len(state.positions)`; live uses `positions` from API

**What this is:** Dry-run counts local paper positions. Live counts exchange positions with 15M contracts.

**Problem:** A dry-run position that was adopted from a live fill (line 2144-2195) exists in both `state.positions` (paper) and exchange `positions`. The dry-run `open_count` includes it, but live `open_count` also includes it. If dry-run and live run simultaneously (they shouldn't, instance lock prevents), they'd have different counts.

**Fix:** Ensure instance lock prevents concurrent dry/live (B15 at line 889-891). Add comment that `open_count` is mode-isolated.

**If we skip it:** Documentation only; instance lock already prevents conflict.

---

### S18. **risk.py:444-450** — Microstructure bootstrap (portfolio < $10) caps per-trade at 50% of available cash, but `available_cash = cash - resting_orders_value`

**What this is:** Line 446: `available_cash = max(0.0, cash - resting_orders_value)`. Line 448: `cap = max(0.0, round(available_cash * 0.50, 4) if available_cash >= 0.02 else available_cash)`.

**Problem:** If `resting_orders_value > cash`, `available_cash = 0`, `cap = 0`. No trades allowed. But resting orders are **unfilled** — they may never fill. The bot starves itself waiting for resting orders that are far from the money.

**Fix:** For bootstrap, use `cash` (not `cash - resting`) for the 50% cap, since resting orders are speculative. Or cap resting_orders_value at `cash * 0.5` for bootstrap.

**If we skip it:** Bootstrap accounts with resting orders far from market can't trade (self-inflicted).

---

## Nice to Have — Polish / Leverage / Future-Proofing

### N1. **trader.py:296-394** — `_pre_trade_check` is 100 lines; extract sub-checks to private methods

**Why:** Window budget, cash reserve, resting orders, market daily loss, tradable balance, risk.check — each is a distinct gate. Extracting improves readability and testability.

**Effort:** 30 min, pure refactor, no behavior change.

---

### N2. **risk.py:479-550** — `risk.check()` does 6 distinct checks; split into `check_halt()`, `check_cooldown()`, `check_market_loss()`, `check_liquidity()`, `check_sizing()`, `check_cash_floor()`

**Why:** Each check has different semantics (some mutate state, some pure). Splitting allows callers to run subsets (e.g., `max_trade_dollars` only needs sizing).

**Effort:** 45 min, refactor with same public API.

---

### N3. **gates.py:391-619** — `evaluate_entry()` is 230 lines; split into `gate_atr()`, `gate_time_decay()`, `gate_ev()`

**Why:** Each gate is independent. Shadow mode logic (lines 598-604) applies to all — centralize it.

**Effort:** 45 min, pure refactor.

---

### N4. **local_db.py:110-176** — `__init__` does schema creation, migration, integrity check, backup restore, sync thread start — too much

**Why:** Split into `_init_schema()`, `_run_migrations()`, `_startup_integrity()`, `_start_sync()`. Easier to test and debug.

**Effort:** 30 min, refactor.

---

### N5. **config.py** — 986 lines of flat constants; group into nested classes (`RiskConfig`, `GateConfig`, `PaperConfig`, `LLMConfig`)

**Why:** 200+ config vars are hard to navigate. Namespacing (`config.Risk.CASH_FLOOR_PCT`) improves IDE autocomplete and prevents collisions.

**Effort:** 60 min, mechanical rename with find/replace.

---

### N6. **trader.py** — `BotState` class (lines 240-295) has 40+ attributes; use `@dataclass` with type hints

**Why:** Current `__init__` manually initializes each attr. Dataclass reduces boilerplate, adds type safety, enables `state_dict()`/`load_state_dict()` auto-generation.

**Effort:** 45 min, careful migration (some attrs are computed properties).

---

### N7. **market_finder.py** — `find_active_markets()` returns `List[MarketInfo]` with `total_available` attached as attribute (line 263)

**Why:** `state.available_active_markets = getattr(markets, "total_available", len(markets))` (line 2012) is a hack. Return a named tuple or dataclass with `markets` and `total_available` fields.

**Effort:** 20 min, clean API change.

---

### N8. **scripts/** — 40+ scripts in root; organize into `scripts/trading/`, `scripts/backtest/`, `scripts/maintenance/`, `scripts/diagnostic/`

**Why:** Flat list is hard to navigate. Grouping by purpose helps operators find the right tool.

**Effort:** 30 min, `mv` + update imports/references.

---

### N9. **tests/** — No integration test for full cycle (settlement → P&L → risk update → next cycle)

**Why:** Unit tests cover individual functions. A cycle integration test (mock client → cycle() → verify state) would catch cross-module bugs like M1, M2.

**Effort:** 60 min, new test file `tests/test_cycle_integration.py`.

---

## Lean Code Opportunities (Delete / Simplify)

### L1. **latency_arb.py** — 434 lines, "deliberately NOT wired in" (trader.py:2903-2906), kept for experiments

**Action:** Move to `scripts/experimental/latency_arb.py` or delete. Not in production path.

---

### L2. **crossvenue.py, crossvenue_edge.py, crossvenue_history.py, crossvenue_collect.py** — Cross-venue arb modules, not used by main loop

**Action:** Move to `scripts/experimental/crossvenue/` or delete. The Polymarket US path is in `poly_us_trader.py`.

---

### L3. **ladder_strategy.py** — 400 lines, only used by `_ml_filter_signals()` (trader.py:1788-1844) when `_ml_model` loaded

**Action:** Keep (ML path), but document it's ML-only. Could be lazy-imported.

---

### L4. **historical_downloader.py** — 535 lines, duplicates `data_collector.py` / `kalshieye_downloader.py` functionality

**Action:** Audit which is used; delete duplicates. `data_collector.py` is the active one.

---

### L5. **backtest_real.py, parallel_backtest_hf.py** — Old backtest variants, superseded by `backtest.py` + `parallel_backtest.py`

**Action:** Delete or archive. `backtest.py --quick --jobs 4` is the canonical path.

---

### L6. **cfb_monitor.py** — 534 lines, monitors College Football betting markets (not crypto)

**Action:** Move to separate repo or delete. Not part of SuperBot core.

---

### L7. **agent_bus.py, mempalace_bridge.py, agent_identity.py, control.py, emit.py, events.py** — MemPalace coordination stack, only used by `trader.py` for session start/bridge

**Action:** Keep (required for M3+ coordination), but document they're infrastructure, not trading logic.

---

## Verdict

| Category | Count | Est. Fix Time |
|----------|-------|---------------|
| **Must Fix** | 12 | ~4 hours |
| **Should Fix** | 18 | ~6 hours |
| **Nice to Have** | 9 | ~5 hours |
| **Lean (Delete)** | 7 | ~2 hours |

**Total:** ~17 hours of focused work to close all findings.

**Priority Order:**
1. M1, M3, M4, M6, M11, M12 — crash/data-loss risks (fix first)
2. M2, M5, M7, M8, M9, M10 — correctness/logic bugs
3. S1-S18 — observability/maintainability (batch by module)
4. N1-N9 — refactor/cleanup (opportunistic)
5. L1-L7 — deletions (safe, do anytime)

---

## Not Checked (Out of Scope)

- **n8n workflow definitions** (injected via `inject_n8n_workflows.py`, not source-controlled)
- **Systemd unit files** (`deploy/*.service`, `deploy/*.timer`) — assume correct if services run
- **Supabase schema/migrations** — managed separately, local DB is source of truth
- **Bitwarden credential sync** (`scripts/n8n_creds_sync.py`, `scripts/bw_pull_secure_notes.py`) — ops scripts
- **ML training pipeline** (`scripts/train_ml_hf.py`, `ml_model.py`) — experimental, not in live path
- **Polymarket US live trading** (`poly_us_trader.py`, `polymarket_us_client.py`) — separate service, PREPROD only
- **Job bridge / scheduled jobs** (`scripts/job_bridge.py`, `scripts/scheduled_jobs.py`) — infrastructure

---

## Appendix: Key Files by Risk Area

| Area | Files | Lines |
|------|-------|-------|
| **Core Loop** | trader.py | 3,901 |
| **Risk** | risk.py | 610 |
| **Signals** | signals.py | 808 |
| **Gates** | gates.py | 641 |
| **Market Data** | market_finder.py, binance_feed.py | 1,164 |
| **Kalshi API** | kalshi_client.py, rate_limit.py | 1,380 |
| **Local DB** | local_db.py | 2,684 |
| **n8n Client** | n8n_client.py | 842 |
| **Config** | config.py, fees.py | 1,080 |
| **LLM Eval** | llm_eval.py, model.py | 1,050 |
| **Polymarket** | poly_us_trader.py, polymarket_us_client.py | 3,735 |
| **Tests** | tests/*.py | ~15,000 |

---

*End of audit. All findings are actionable with small, focused changes. No architectural rewrites needed.*