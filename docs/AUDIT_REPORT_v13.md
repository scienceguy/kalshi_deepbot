# Audit Report v13 - kalshi_deepbot
**Run date:** 2026-11-20
**Model:** Nemotron 3 Ultra (in-session)
**Scope:** Full repository (/opt/superbot), 1392 tests passing, 4 source-corruption gates clean

## What this repo does
Kalshi SuperBot: Live trading bot for Kalshi 15-minute binary crypto markets and Polymarket US. Places favourite-maker limit orders (YES/NO at 92-99c), evaluates signals via LLM with 4-layer fallback. Writes signals/orders/bot-state to Supabase, routes settlement P&L through n8n webhooks. Two concurrent trader processes (Kalshi + Polymarket US), sub-second tick-to-order target.

## Must Fix

### 1. No test file for `local_db.py` (critical data layer)
- **What this is:** `local_db.py` is the single source of truth for positions, orders, fills, ledger, settlements, and sync state (Pillar 2). It handles WAL-mode SQLite with transactional outbox, retention pruning, and day-0 balance snapshots.
- **Problem:** No dedicated test file exists. Coverage relies on integration tests in `test_d2_poly_local_db.py`, `test_d3_sync_hardening.py`, `test_m4_local.py` but no direct unit tests for the `LocalDB` class methods.
- **Fix:** Create `tests/test_local_db.py` with unit tests for every public method: `insert_order`, `confirm_order_intent`, `insert_fill`, `insert_ledger`, `upsert_position`, `get_positions`, `set_bot_state`, `get_bot_state`, `get_sync_lag_stats`, `prune_retention`, `insert_spot_tick`, `get_spot_ticks`, `insert_day0_balance`, `get_day0_balance`.
- **If we skip it:** A regression in the data layer could silently corrupt the ledger or lose settlements. The outbox and retention logic are untested in isolation.

### 2. `poly_us_trader.py` is 3,478 lines / 165 KB — violates single responsibility (F9)
- **What this is:** The Polymarket US trader mixes state management, ledger writer, fees, paper fills, MemPalace hooks, resting guard wiring, and the main cycle loop in one file.
- **Problem:** Hard to test in isolation, slow to reason about under latency work (T13-45), any change risks unrelated logic. The `test_poly_us_trader.py` is 1,964 lines just to cover it.
- **Fix:** Split into modules per T13-46: `poly_state.py` (positions, risk, config), `poly_ledger.py` (writer thread, spill, outbox), `poly_fees.py` (fee model), `poly_paper.py` (paper fill simulator), `poly_mempalace.py` (bridge hooks), `poly_guard.py` (resting guard wiring), `poly_cycle.py` (main loop). Behavior-preserving with tests first.
- **If we skip it:** Latency work (T13-45) will be blocked by cognitive load; bugs in one area leak into others.

### 3. `local_db.py` print statements in production paths
- **What this is:** `local_db.py` has no print statements (good), but several utility scripts (`rate_limit.py`, `check_paper.py`, `probe_band.py`, `grid_latency.py`, `crossvenue.py`, `trader.py` debug prints, `crossvenue_history.py`) have `print()` calls that execute during normal operation or self-checks.
- **Problem:** Self-check prints in `rate_limit.py` run on import if invoked directly; `trader.py` prints outbox metrics and sync lag on SIGUSR1 (debug path, acceptable); `crossvenue.py` prints on every `measure_edge` call (used in backtests).
- **Fix:** Move all diagnostic prints behind a `--debug` flag or logging at DEBUG level. `rate_limit.py` self-check should be a pytest, not a `__main__` print block.
- **If we skip it:** Log noise in production; stdout pollution when scripts are imported.

### 4. `poly_us_ws.py` has unimplemented unsubscribe (TODOs in hot path)
- **What this is:** Line 378: `pass  # TODO: implement unsubscribe with request_id tracking` in the WebSocket message handler.
- **Problem:** If the WS connection needs to resubscribe or clean up channels, unsubscribes are silently dropped. This can cause duplicate subscriptions or stale data on reconnect.
- **Fix:** Implement proper unsubscribe with request_id tracking matching the subscribe pattern.
- **If we skip it:** Reconnect storms or memory leaks on long-running WS connections.

## Should Fix

### 5. Duplicate `get_balance` / `get_markets` / `Client` patterns across 3+ venues
- **What this is:** Three venues (Kalshi REST, Kalshi WS, Polymarket US WS, Binance) each implement their own client class with overlapping method names but different signatures and error handling.
- **Problem:** No common interface; caller must know which client it has. `KalshiClient.get_balance()` returns `float` (dollars), `PolyUSWS.get_balance()` returns `Optional[dict]`, `PolymarketUSWS.get_balance()` returns `Optional[AccountBalanceUpdate]`.
- **Fix:** Define a `VenueClient` protocol in `venue_client.py` with `get_balance() -> float`, `get_markets(...) -> List[Market]`, `place_order(...) -> OrderResult`. Wrap each venue client with an adapter. Start with Kalshi + Polymarket US.
- **If we skip it:** Cross-venue orchestration (T13-34) requires type checks and adapters at every call site.

### 6. `supabase_client.py:544` shortcut comment admits pagination can skip rows
- **What this is:** `shortcut: offset pagination can re-read/skip rows under concurrent writes`
- **Problem:** The sync worker uses `offset`/`limit` for batched reads. Under concurrent writes (trader writing, sync reading), rows can be missed or double-processed.
- **Fix:** Use cursor-based pagination (keyset) on a monotonically increasing column (`id` or `created_at` with tiebreaker). The `sync_health` breaker already exists; make the read path correct.
- **If we skip it:** Sync lag metric becomes unreliable; replica drift goes undetected.

### 7. `trader.py:3430` shortcut: unfilled resting order stays 'pending' until next cycle
- **What this is:** Resting orders that don't fill remain in `resting_value` dict with status 'pending'. Next cycle re-evaluates.
- **Problem:** If the order is partially filled or cancelled at the venue, local state may not match until the next cycle poll. The `_already_ordered()` check prevents duplicate entries but doesn't handle partial fills correctly.
- **Fix:** On each cycle, query venue for all resting order statuses (already done in `sync_resting_orders`) and update local state immediately. The shortcut is acceptable for dry-run but risky for live.
- **If we skip it:** Live trading could place duplicate orders or miscalculate available cash.

### 8. No circuit breaker on `binance_feed.py` / `coinbase_feed.py` WS connections
- **What this is:** WebSocket feeds run forever with reconnect logic but no circuit breaker. If a venue API changes or returns errors, the feed loops forever logging errors.
- **Problem:** Error spam in logs; no alert to operators; other feeds may starve if event loop is busy handling errors.
- **Fix:** Add failure counter per venue; after N consecutive failures, emit `feed.circuit_open` event and pause reconnect for backoff period. Use existing `resilience.py` patterns.
- **If we skip it:** Silent degradation of spot price quality; composite price may use stale data.

### 9. `config.py` validation at import time runs before .env is fully loaded in some test contexts
- **What this is:** `validate_all()` runs at module import (line 978). Tests that patch `os.environ` after import miss the validation.
- **Problem:** Tests like `test_config.py` monkeypatch env vars but validation already ran with defaults. Some tests may pass with invalid configs that would fail at runtime.
- **Fix:** Make `validate_all()` idempotent and call it explicitly in `trader.py`/`poly_us_trader.py` `main()` after env is loaded. Keep import-time validation as a safety net but allow re-validation.
- **If we skip it:** Tests don't catch config errors that would crash production.

### 10. `gates.py` imports `binance_feed` at module level (tight coupling)
- **What this is:** Line 29: `import binance_feed` — gates module depends on feed module for spot price.
- **Problem:** Circular import risk (feed may import gates for validation). Gates should be pure functions taking spot price as argument; the caller fetches spot.
- **Fix:** Remove `import binance_feed` from `gates.py`. Change `evaluate_entry` to accept `spot_price` parameter. Caller (trader) fetches from composite spot service (T13-10).
- **If we skip it:** T13-10 composite spot service cannot be cleanly integrated; gates tests require Binance feed mock.

## Nice to Have

### 11. `rate_limit.py` self-check prints should be pytest
- **What this is:** Lines 395-458 print self-check results when run as `__main__`.
- **Fix:** Move to `tests/test_rate_limit_selfcheck.py` as proper assertions.
- **Impact:** Cleaner imports; CI runs the check.

### 12. Several utility scripts in root (`check_paper.py`, `probe_band.py`, `grid_latency.py`, `crossvenue_history.py`) are not installed or tested
- **What this is:** Operational scripts left in repo root, not in `scripts/` or `tools/`.
- **Fix:** Move to `scripts/` or delete if obsolete. `crossvenue.py` is used by backtests; keep but move.
- **Impact:** Cleaner repo structure; `pip install -e .` doesn't install junk.

### 13. `kalshi-trade-downloader/` subdirectory duplicates `kalshi_client.py`
- **What this is:** Separate `kalshi_client.py` in subdirectory with overlapping functionality.
- **Fix:** Remove if unused, or merge into main client with feature flags.
- **Impact:** Less duplication; single source of truth for Kalshi API.

### 14. `llm_eval.py` 7-layer fallback — layer 7 (template) is a constant string, not a real model
- **What this is:** The fallback chain ends with a hardcoded template response.
- **Problem:** If all 6 real providers fail, the bot gets a fake verdict that looks real.
- **Fix:** Make the final fallback raise an exception or return a clearly marked `LLMVerdict(source="fallback_template", confidence=0.0, ...)`. Hot path should treat confidence=0 as "no LLM input" and rely on GBM/KL only.
- **Impact:** Prevents silent degradation when all LLM providers are down.

## Verdict
**Healthy with structural debt.** The bot runs, tests pass (1392), gates clean. Critical data layer (`local_db.py`) lacks unit tests. Largest file (`poly_us_trader.py`) needs splitting before latency work. Duplicate client patterns block clean cross-venue orchestration. Shortcuts in sync pagination and resting-order tracking are acceptable for paper but must be fixed before live scaling.

## Lean
- `-1,500+ lines` possible by splitting `poly_us_trader.py` (T13-46)
- `-200+ lines` by deduplicating venue client adapters
- `-3 utility scripts` moved to `scripts/` or deleted
- `kalshi-trade-downloader/` can be removed if unused

## Not Checked
- `ml_model.py` / `train_ml_hf.py` internals (opt-in, paper-only)
- `backtest_engine.py` lookahead-bias fix validation (tests exist)
- n8n workflow JSON correctness (import tests pass)
- systemd unit files (verified WorkingDirectory in T13-50)
- OCI deployment (not in repo scope)
- Real Binance.US / Coinbase WS connectivity from this VPS (T13-10 pending)