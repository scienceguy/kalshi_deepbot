# Comprehensive To-Do List — Kalshi UltraBot
**Goal:** Reach $10,000 paper balance with continuous compounding (no decreases)
**Method:** Continuous development loop using ponytail-audit + ponytail-review skills
**Status:** Active — Started 2026-11-09

---

## Phase 1: Must Fix (Critical — Data Loss / Bugs)

### 1.1 Fix `_replay_settlement_queue` PnL data loss (trader.py:674-720)
- [ ] Reorder operations: write to local `pnl_ledger` FIRST (idempotent), then Supabase, then remove from queue
- [ ] Follow pattern from `_book_exit` (lines 1243-1284)
- [ ] Add test for crash-recovery scenario
- [ ] Verify idempotency key prevents double-counting

### 1.2 Fix `_bid_hist` 30s save interval (signals.py:141-250)
- [ ] Reduce `_BID_HIST_SAVE_INTERVAL` from 30 → 5 seconds
- [ ] Or call `_force_save_bid_hist()` after each `_track_bid()` in `evaluate()`
- [ ] Test that stability gate doesn't block valid trades on fresh restart

### 1.3 Fix `_schema_warned` flag (supabase_client.py:33)
- [ ] Reset `_schema_warned = False` in `_supabase_record_success()`
- [ ] Test that schema errors re-log after schema is created

### 1.4 Fix FAVOURITE confidence calculation (signals.py:694)
- [ ] Use same formula as SKEW/TREND branch (lines 378-388)
- [ ] Incorporate strength score + mid_distance
- [ ] Verify consistency across all signal sources

---

## Phase 2: Should Fix (Risk / Missing Tests / Duplication)

### 2.1 Add integration tests
- [ ] `tests/test_gates.py` — test `gates.evaluate_entry()` with real market data
- [ ] `tests/test_llm_eval.py` — test `llm_eval.evaluate_trade()` with mock providers
- [ ] `tests/test_paper_mode.py` — test paper ledger persistence and recovery
- [ ] `tests/test_market_finder.py` — test `find_active_markets()` with mocked client
- [ ] `tests/test_risk.py` — test risk limits, drawdown halt, market daily loss

### 2.2 Backtest lookahead bias (backtest_engine.py:89-94)
- [ ] Replace synthetic 45c entry with actual bid/ask mid from historical data
- [ ] Document limitation clearly if synthetic must remain

### 2.3 Fee math deduplication
- [ ] Consolidate fee calculation in `fees.py` only
- [ ] Remove duplicate `taker_fee_cents` from `model.py:145-152`
- [ ] Ensure single source of truth

### 2.4 Config validation auto-call
- [ ] Call `validate_credentials()`, `validate_dry_run()`, `validate_bands()` in `config.py` import
- [ ] Or ensure trader.py calls all three before any API connection
- [ ] Add test for invalid config rejection

### 2.5 Supabase circuit breaker (supabase_client.py:189-232)
- [ ] Track 4xx errors and alert/stop rather than silent return None
- [ ] Add circuit breaker state to logstream

### 2.6 Dead code removal
- [ ] Move `crossvenue.py`, `crossvenue_collect.py`, `crossvenue_history.py` → `legacy/`
- [ ] Move `polymarket_arb.py`, `polymarket_client.py` → `legacy/`
- [ ] Move `redstone_client.py`, `binance_client.py`, `coinbase_client.py` → `legacy/`
- [ ] Move `latency_arb.py` → `legacy/` (or delete — unused in main flow)
- [ ] Move `kalshieye_downloader.py` → `legacy/`
- [ ] Move `hf_backtest.py`, `parallel_backtest_hf.py` → `legacy/`
- [ ] Move `polymarket_arb.py` → `legacy/`
- [ ] Update `.gitignore` for `legacy/` folder

### 2.7 Duplicate signal evaluation logic
- [ ] Consolidate `_evaluate_common` usage
- [ ] Remove redundant paths in `signals.py`

### 2.8 Hedge config documentation
- [ ] Add comment: `MAX_HEDGE_PER_SERIES=0` disables hedges by default
- [ ] Document how to enable: set to >0

### 2.9 Stale order cleanup O(n)
- [ ] Optimize `clear_resting_orders` — use dict by ticker for O(1) lookup
- [ ] Currently iterates all orders to find matching ticker

### 2.10 Bid history reload on every read
- [ ] `_load_bid_hist_for_read()` reloads file on every call
- [ ] Cache in memory, only reload on file modification time change

### 2.11 Paper mode two sources of truth
- [ ] `paper_mode.py` + `local_db.py` both track paper PnL
- [ ] Consolidate to single source: `local_db.pnl_ledger` (already idempotent)

---

## Phase 3: Nice to Have (Small Improvements)

### 3.1 ThreadPoolExecutor workers configurable
- [ ] Move `max_workers=4` to config: `MARKET_FINDER_MAX_WORKERS`
- [ ] Apply to `gates.prefetch`, `market_finder`, `kalshi_client`

### 3.2 Request gap lower
- [ ] `REQUEST_MIN_GAP_SECONDS=0.2` → could go to 0.1 (10 req/s, Kalshi allows ~10)
- [ ] Add config and test

### 3.3 Similar dataclasses consolidation
- [ ] `MarketInfo` (market_finder.py) vs similar in other files
- [ ] `GateResult` (gates.py) vs `TradeSignal` (signals.py) — different purposes but similar structure

### 3.4 ATR hurdle constants repetitive
- [ ] `GATES_ATR_HURDLE_BTC`, `_ETH`, `_SOL`, etc. → dict or loop
- [ ] Use `getattr(config, f"GATES_ATR_HURDLE_{asset}", default)`

### 3.5 Momentum function module-level
- [ ] `_compute_momentum` in `signals.py` → move to `model.py` with other models
- [ ] Or keep but document why separate

### 3.6 Split trader.py
- [ ] `trader.py` 1866 lines → split into `trader_loop.py`, `trader_orders.py`, `trader_settlement.py`, `trader_state.py`
- [ ] Keep main entry point in `trader.py`

---

## Phase 4: Operational / Compounding

### 4.1 Verify dry-run compounding works
- [ ] Run `python trader.py --dry --cycle-limit 100` 
- [ ] Verify paper balance increases continuously
- [ ] Check no balance decreases (only fees reduce, but wins > losses)

### 4.2 Optimize parameters for compounding
- [ ] Use backtest optimal params: MIN_SKEW=3, MIN_TREND=8, ENTRY_BAND=15-55c
- [ ] Set `MODEL_MIN_EDGE_CENTS=0.5`, `PER_TRADE_PCT=0.08`, `AGGRESSIVE_FILL=False`
- [ ] Verify with extended dry run

### 4.3 Run until $10,000 reached
- [ ] Start continuous dry-run loop
- [ ] Monitor paper balance every cycle
- [ ] Stop only at $10,000+

### 4.4 MemPalace coordination
- [ ] File decisions to MemPalace after each fix
- [ ] Use logstream for cross-agent coordination
- [ ] Checkpoint session state periodically

---

## Phase 5: Verification & Commit

### 5.1 After each fix
- [ ] Run `python -m pytest tests/ -q` — all pass
- [ ] Run `ponytail-review` on changes
- [ ] Commit with message: `Must fix: <description>` / `Should fix: <description>` / `Nice-to-have: <description>`
- [ ] Push to origin/main

### 5.2 Final verification
- [ ] All 4 audit findings fixed
- [ ] All 62 AGENTS.md audit items resolved
- [ ] All tests pass (273+)
- [ ] Paper balance ≥ $10,000 with continuous compounding
- [ ] Git history clean, all changes committed

---

## Progress Tracking

| Phase | Total Tasks | Done | In Progress | Blocked |
|-------|-------------|------|-------------|---------|
| 1: Must Fix | 4 | 0 | 0 | 0 |
| 2: Should Fix | 11 | 0 | 0 | 0 |
| 3: Nice to Have | 6 | 0 | 0 | 0 |
| 4: Operational | 4 | 0 | 0 | 0 |
| 5: Verification | 5 | 0 | 0 | 0 |
| **Total** | **30** | **0** | **0** | **0** |

---

## MemPalace Coordination Keys

- `wing: ultrabot, room: todo, drawer: phase1` — Phase 1 tasks
- `wing: ultrabot, room: todo, drawer: phase2` — Phase 2 tasks
- `wing: ultrabot, room: decisions` — Key decisions made
- `wing: ultrabot, room: status` — Current status broadcasts
- `correlation_id: task_fix_<finding>` — Task delegation IDs

---

**Last Updated:** 2026-11-09
**Next Action:** Start Phase 1.1 — Fix `_replay_settlement_queue` PnL data loss