# Ponytail Audit — Kalshi SuperBot (full repo)

**Date:** 2026-11-15  
**Auditor:** ponytail (human-style senior developer review)  
**Repository:** `/data/data/com.termux/files/home/kalshi_deepbot`  
**Commit:** HEAD (all 439 tests pass)

---

## What this repo does

A single-operator live-trading bot for Kalshi 15-minute binary crypto markets.
It places favourite-maker limit orders (YES/NO at 75–99c), evaluates signals
through pre-entry gates (ATR hurdle, fee-adjusted EV, time-decay cushion) plus
an optional 7-layer LLM fallback, writes signals/orders/bot-state to Supabase,
and routes settlement P&L through n8n webhooks. Dry-run mode is the default;
live trading requires explicit `--live` + `KALSHI_CONFIRM_LIVE=true`.

**Load assumed:** One bot process, single account, ~10 req/s sustained against
the Kalshi API, ~20-second cycle, 9 crypto series monitored concurrently. The
bot must survive transient network failures, API rate limits, and restarts
without double-counting P&L or leaking capital. Memory must stay bounded over
weeks of continuous operation.

---

## Must fix

### 1. Dry-run sizes trades against live exchange balance, not paper bankroll (`trader.py:1221-1223`)
**What this is:** `_sizing_cash()` returns `client.get_balance()` for dry-run
mode — the real Kalshi account balance — instead of the paper bankroll.
**Problem:** The docstring says "A dry run holds no shard cash of its own, so
it sizes against its paper bankroll," but the code sizes against live cash.
If the live account has $1–2, dry-run trades are blocked by cent granularity
or oversized relative to the paper curve. The paper balance cannot compound
correctly because sizing doesn't match the paper ledger.
**Fix:** Return `paper_mode.cash(0.0, None)` (paper bankroll) for dry-run,
`client.get_shard_balance(CRYPTO_EXCHANGE_INDEX)` for live.
**If we skip it:** Dry-run compounding is fictitious — trades are sized
against the wrong balance, so the paper curve doesn't reflect real behavior.

### 2. Dry-run drawdown halt uses cost-basis exposure, not mark-to-market (`trader.py:1736-1737`)
**What this is:** Line 1737 sets `mark_to_market_exposure =
sum(state.position_risk.values())` for dry-run, which is cost-basis
(`contracts * entry_price / 100`). But lines 1277–1295 already computed a
proper mark-to-market `paper_mtm_exposure` from live orderbook mids.
**Problem:** A paper position that moved against the bot still shows original
entry cost, so drawdown is understated and the exposure ceiling doesn't bind.
The risk halt fires on stale data — a losing streak won't stop the bot.
**Fix:** Use `paper_mtm_exposure` (computed at lines 1277–1295) as
`mark_to_market_exposure` for dry-run instead of cost-basis sum.
**If we skip it:** Dry-run risk metrics are fiction; drawdown halt is inert in
paper mode, giving false confidence before live deployment.

### 3. Adopted positions lack `entry_taker` field (`trader.py:1408-1411`)
**What this is:** When the bot adopts a position from the exchange (reconciliation
loop), the position dict is created without `entry_taker`.
**Problem:** On settlement (`trader.py:1501`) or early exit (`trader.py:955`),
the code falls back to `config.AGGRESSIVE_FILL`. If the global config differed
when the order was originally placed, the fee calculation is wrong — a maker
entry could be charged a taker fee, or vice versa.
**Fix:** Add `"entry_taker": None` to adopted position dicts so `get_entry_taker()`
warns and falls back to current config with a visible warning.
**If we skip it:** Live P&L for adopted positions may carry the wrong entry fee.

### 4. `_already_ordered` only checks `resting_tickers`, misses exchange fills (`trader.py:1031-1043`)
**What this is:** `_already_ordered` returns True if `ticker in state.resting_tickers`.
**Problem:** If a resting order fills *between* cycles, `resting_tickers` still
has the ticker until the next cycle's reconciliation clears it. The check runs
before reconciliation in the same cycle, so a filled ticker blocks a new entry
until the next cycle. One cycle of missed entries per fill.
**Fix:** Also check `state.live_resting_orders` (populated at cycle start from
`client.get_orders()`) for the ticker.
**If we skip it:** Reduced trade frequency — one missed entry cycle per fill.

---

## Should fix

### 5. `_paper_guards_relaxed` bypasses drawdown halt/cooldown/loss limit for dry-run (`risk.py:17-24`)
**What this is:** When `PAPER_GUARDS_ENABLED=False`, dry-run ignores the drawdown
halt, consecutive-loss cooldown, and per-market daily loss limit.
**Problem:** The comment says "operator directive: drop the guards for now."
This is a temporary hack that made it into the codebase. Dry-run never
exercises the same risk logic as live, so a live deploy with the same params
hits guards that were never triggered in testing.
**Fix:** Remove the bypass or gate it behind a test-only flag. Default should
be `True` so dry-run exercises the same risk logic as live.
**If we skip it:** Dry-run gives false confidence; live behavior differs.

### 6. `spot_for` only queries Binance, which is 451-blocked (`gates.py:115`, `binance_feed.py`)
**What this is:** `gates.spot_for(asset)` calls `binance_feed.composite_price()`
which only hits Binance. Binance returns 451 (geo-blocked) in this
environment, so spot price is always None and GBM model falls back to
momentum.
**Problem:** The comment in `signals.py:605` says "Spot price from Binance/
Coinbase composite feed" but only Binance is queried. No Coinbase fallback
despite Coinbase returning 200 (~$68k BTC-USD). GBM model can't use real spot.
**Fix:** Add Coinbase as a fallback in `binance_feed.composite_price()` or
`gates.spot_for()`.
**If we skip it:** GBM model degrades to momentum heuristic; edge estimation
is weaker than possible.

### 7. `mark_failure` increments on ANY exception, including programmer errors (`resilience.py:39-59`)
**What this is:** The cycle's exception handler calls
`state.resilience.mark_failure(e)` on any unhandled exception.
**Problem:** A `ValueError`, `TypeError`, or `KeyboardInterrupt` counts as a
failure and triggers backoff/client rebuild instead of surfacing the bug. The
`is_auth_error()` check uses fragile string matching ("auth"/"permission"/
"401"/"403").
**Fix:** Catch specific exception types in the cycle handler; only mark
failure for network/API errors, not `KeyboardInterrupt`, `SystemExit`, or
programmer errors.
**If we skip it:** A latent bug triggers client rebuild instead of being fixed.

### 8. Duplicate fee rounding paths (`fees.py:38-49`)
**What this is:** `taker_fee_dollars()` rounds to 6 decimal places; `taker_fee_cents()`
rounds to cents with a different formula (`ceil(raw * 100 - 1e-9)`). `buy_cash_out()`
and `sell_cash_in()` do their own cent-grid rounding.
**Problem:** Two code paths for fee rounding can drift by 1 cent if they
diverge. Cognitive load for anyone maintaining fee math.
**Fix:** One canonical fee function returning effective fee in dollars
(cent-grid rounded); derive cents from it.
**If we skip it:** Minor maintenance burden; risk of 1-cent fee drift.

### 9. Brittle regex ticker parsing in cross-venue (`crossvenue.py:36-37`)
**What this is:** `_KALSHI_EVENT` and `_PM_SLUG` regexes parse tickers/slugs
with string patterns.
**Problem:** Kalshi changed ticker format before (adding `15M` suffix).
Polymarket slugs vary. Regexes break silently on format changes, returning
None and skipping markets.
**Fix:** Use API structured fields (`event_ticker`, `series_ticker`, `asset`,
`expiry`) instead of parsing strings.
**If we skip it:** Cross-venue comparison silently drops markets when formats
change.

---

## Nice to have

### 10. Dead `_evaluate_two_sided` code (`signals.py:652-672`)
**What this is:** `evaluate()` calls `_evaluate_taker_favourite` for MAIN markets.
`_evaluate_two_sided` is not called anywhere.
**Problem:** Dead code inflates the module. Future changes to the live path
won't update the dead path, and it becomes a trap for anyone reading it.
**Fix:** Delete `_evaluate_two_sided` and its helpers, or add a config flag
to switch strategies.
**If we skip it:** Code rot — unreachable code that drifts from the live path.

### 11. Unused `_remaining` timeout parameter (`binance_feed.py:100-102`)
**What this is:** `_remaining(timeout: int, deadline: float)` — the `timeout`
param is shadowed and never read.
**Problem:** Confusing signature; no runtime bug.
**Fix:** Remove unused `timeout` parameter.
**If we skip it:** Confusing code, no functional impact.

### 12. Hardcoded `risk_free_rate=0.05` in GBM (`model.py:67-70`)
**What this is:** `gbm_probability_yes_wins` uses a fixed 5% risk-free rate.
**Problem:** Magic number; effect is tiny for 15-minute expiries (~1.4e-6) but
drifts over years.
**Fix:** Make configurable via `config.MODEL_RISK_FREE_RATE` with default 0.05.
**If we skip it:** Negligible impact; model drift over years.

---

## Verdict

**Healthy but not production-ready for live.** The codebase is well-structured
with 439 passing tests, idempotent P&L, layered risk management, and graceful
degradation. However, three **Must fix** items directly affect money correctness:

1. **Dry-run sizes against live balance** — paper compounding is fictitious.
2. **Dry-run drawdown halt uses cost-basis** — risk halt is inert in paper mode.
3. **Adopted positions lack `entry_taker`** — settlement fees may be wrong for
   adopted positions.

Fix #1 and #2 before any live deployment — they make dry-run results
untrustworthy. Fix #3 before live trading with adopted positions.

---

## Lean: -120 lines, -1 dependencies possible

- **Delete dead code:** `_evaluate_two_sided` in `signals.py` (~100 lines).
- **Merge duplicate fee logic:** `taker_fee_dollars` + `taker_fee_cents` in
  `fees.py` (~40 lines → ~15).
- **Remove unused torch dependency** from requirements: `ml_model` is lazy-
  loaded and only used with `--ml-model`; the backtest/ML training scripts are
  separate. Torch is heavy on aarch64/Termux.

---

## Not checked

- `scripts/` directory (28 files) — operational utilities, not in the trading loop.
- `polymarket_us_client.py` / `derive_poly_creds.py` — separate Polymarket US
  integration; read-only client verified working ($35 balance), but **order
  placement capability exists in the SDK and is NOT wired into the trading
  loop** — zero references to Polymarket in `trader.py`, `signals.py`,
  `gates.py`, or `market_finder.py`. Concurrent Kalshi + Polymarket trading is
  not implemented.
- `backtest.py` / `parallel_backtest.py` — backtest runners, not live path.
- `local_db.py` — local SQLite sync layer; per-row Supabase round-trips flagged
  in prior audits but not re-verified here.
- `n8n_client.py` — verified functional (25s webhook timeout, 4 health-check
  endpoints, retry logic).
- `supabase_client.py` — verified functional (circuit breaker, batch writes,
  credential priority).
- `paper_mode.py` — verified functional (atomic writes, isolated compounding).
- `local.py` — logstream integration works; **KG operations
  (`kg_add`/`supersede`/`invalidate`) and `artifact_put`/`get` were missing**
  and have been added in this session.
- `data_collector.py` — in `legacy/`, not active.
- n8n workflow internals — managed via DB injection, healthy on VPS only.
