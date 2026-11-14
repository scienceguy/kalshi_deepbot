# Ponytail Review: trader.py entry_taker / paper positions changes

**Date:** 2026-11-14  
**Base commit:** b296b74  
**Files changed:** `trader.py`, `tests/test_trader.py`, `conftest.py`  
**Tests:** 438 passed (17.71s)

---

## What this change does

This change introduces two related fixes:

1. **Paper positions now persist across restarts** — Previously, dry-run positions lived only in memory. A restart dropped all open fills for the current 15-minute window, and their settlement P&L was never booked. New `_load_paper_positions()` / `_save_paper_positions()` persist positions atomically to `paper_positions.json` (dry-run only). `BotState.__init__` loads them and pre-computes `position_risk` so cash isn't double-spent on new trades.

2. **Entry fee mode (`entry_taker`) is now recorded per-trade instead of read from global config at settlement/exit time** — `_entry_fee_dollars()` and `_settle_pnl()` now take an explicit `entry_taker: bool` parameter. `_record_filled_order()` stores `entry_taker` in the position dict. `_book_exit()`, settlement processing, and queue replay all read `entry_taker` from the position (falling back to `config.AGGRESSIVE_FILL` for legacy positions). This prevents fee drift when `AGGRESSIVE_FILL` changes mid-session or on restart.

**Load assumed:** Single-process bot, ~15s cycle, one operator. Dry-run and live share the same code paths for fee math. Paper P&L must mirror live including fees.

---

## Findings

### Must fix

#### 1. Adopted/seeded positions lack `entry_taker` — wrong fee on settlement/exit
**File:** `trader.py:1402-1405`, `trader.py:2315-2320`  
**What this is:** When the bot adopts a position from the exchange (reconciliation loop, line 1377) or seeds positions at startup (line 2292), the position dict is created without an `entry_taker` field.  
**Problem:** On settlement (line 1495) or early exit (line 955), the code falls back to `config.AGGRESSIVE_FILL`. If the global config differed when the order was originally placed, the fee calculation will be wrong — a maker entry could be charged a taker fee, or vice versa. This affects live P&L accuracy for any position not created by this bot instance.  
**Fix:** When adopting/seeding, infer `entry_taker` from the order if possible (check if order_id starts with `sb_` and was placed by this bot), or at minimum store `entry_taker: None` and treat `None` as "unknown — use current config but log a warning".  
**If we skip it:** Live P&L for adopted/seeded positions may carry the wrong entry fee. Edge case but real — a manual trade or another bot's position adopted on restart will settle with whatever `AGGRESSIVE_FILL` is *now*, not what it was at entry.

#### 2. `_replay_settlement_queue` leaves settled positions in `state.positions`
**File:** `trader.py:749-914`  
**What this is:** On restart, `main()` calls `_replay_settlement_queue(client)` before the first cycle. It processes queued settlements, credits P&L via idempotent ledger, and drains the queue file.  
**Problem:** It never removes the corresponding tickers from `state.positions` (which was loaded from `paper_positions.json`). The first `cycle()` then sees the position as "settled" (not in API), re-processes it, hits the idempotency key (duplicate), and only then pops it. The position lingers in memory for one cycle, `position_risk` is wrong until then, and paper cash sizing may be off.  
**Fix:** After successfully replaying a settlement (where `applied is True` or `applied is False`), pop the ticker from `state.positions` and `state.position_risk`, and call `_save_paper_positions(state.positions)`.  
**If we skip it:** One cycle of stale position risk on restart; cash sizing slightly off; no double-count thanks to idempotency key.

---

### Should fix

#### 3. Tests for `_record_filled_order` don't pass `entry_taker` explicitly
**File:** `tests/test_trader.py:160`, `tests/test_trader.py:1201`, `tests/test_trader.py:1397`  
**What this is:** Three tests call `_record_filled_order` without the new `entry_taker` parameter, relying on the default `False`.  
**Problem:** The tests exercise the fill-confirmation logic but don't verify the `entry_taker` field is stored in the position dict. A regression where `entry_taker` isn't saved would not be caught.  
**Fix:** Add `entry_taker=True/False` to each call to make the tested scenario explicit.  
**If we skip it:** Test coverage gap for the new field; silent regression possible.

#### 4. No test for `_book_exit` reading `entry_taker` from position dict
**File:** `tests/test_trader.py` (tests `test_paper_exit_charges_taker_exit_fee_when_config_on`, `test_paper_exit_fee_config_off_restores_entry_fee`)  
**What this is:** The existing exit-fee tests create position dicts *without* `entry_taker`, relying on the `config.AGGRESSIVE_FILL` fallback.  
**Problem:** The primary code path — reading `entry_taker` from the position — is untested. If `pos.get("entry_taker", ...)` were changed to `pos["entry_taker"]` (KeyError) or the fallback logic broken, tests would still pass.  
**Fix:** Add a test that creates a position with explicit `"entry_taker": True/False` and verifies `_book_exit` uses it over the config fallback.  
**If we skip it:** The main fee-accuracy feature for exits has no direct test.

#### 5. `_load_paper_positions` doesn't handle `entry_price_cents` key
**File:** `trader.py:167-185`  
**What this is:** The loader returns raw JSON. `BotState.__init__` computes `position_risk` using `p.get("entry_price", 0)`.  
**Problem:** Settlement queue payloads use `entry_price_cents`. If a position dict ever has `entry_price_cents` instead of `entry_price` (manual edit, version skew, future change), `position_risk` becomes 0 — cash is not reserved, double-spend possible.  
**Fix:** In `_load_paper_positions`, normalize keys: if `"entry_price_cents" in p and "entry_price" not in p`, set `p["entry_price"] = p["entry_price_cents"]`.  
**If we skip it:** Low probability but high impact — a corrupted paper_positions.json could silently break risk limits.

---

### Nice to have

#### 6. Inconsistent key naming: `entry_price` vs `entry_price_cents`
**File:** `trader.py` (position dict uses `entry_price`; settlement queue uses `entry_price_cents`)  
**What this is:** The same value (entry price in cents) has two keys across the codebase.  
**Problem:** Cognitive load; conversion bugs (e.g., line 1480 copies `entry_price` → `entry_price_cents`; line 736 reads both).  
**Fix:** Standardize on one name (prefer `entry_price_cents` for clarity) throughout. Or add a helper `get_entry_price_cents(pos)` that checks both.  
**If we skip it:** Minor maintenance burden; current conversions are correct.

#### 7. Adopted positions missing `paper` flag (always False in live)
**File:** `trader.py:1402-1405`  
**What this is:** Adopted positions don't set `"paper": False` explicitly.  
**Problem:** `_book_exit` checks `pos.get("paper")` to decide exit fee mode. Missing key → `False` → live behavior. Correct for live, but implicit.  
**Fix:** Add `"paper": False` for clarity.  
**If we skip it:** No functional bug; implicit False works.

---

## Verdict

The core changes are sound: per-trade `entry_taker` eliminates fee drift, and paper position persistence fixes a real dry-run data-loss bug. All 438 tests pass.

**Two Must-fix items** should be addressed before merging:
1. Adopted/seeded positions need `entry_taker` (or explicit `None` with warning).
2. `_replay_settlement_queue` should clean `state.positions` after replay.

**Three Should-fix items** improve test coverage and robustness:
3. Tests pass `entry_taker` explicitly.
4. Add test for `_book_exit` using position's `entry_taker`.
5. Normalize `entry_price_cents` in paper position loader.

---

## Lean: -N lines possible

- **~15 lines:** The `entry_price` → `entry_price_cents` conversion at lines 1480, 1713, 1481, 1714 could be a one-line helper.
- **~10 lines:** `_load_paper_positions` and `_save_paper_positions` could share a normalization helper for `entry_taker` / `entry_price` keys.
- **~5 lines:** The `entry_taker` fallback `bool(pos.get("entry_taker", config.AGGRESSIVE_FILL))` appears 4 times (lines 955, 1495, 1481, 1713) — could be a helper `get_entry_taker(pos)`.

---

## Not checked

- **`fees.py` compatibility** — `_entry_fee_dollars` uses `fees.buy_cash_out(..., taker=True) - contracts * price`. The fill-time fee at lines 2069/2107 uses `fees.taker_fee_dollars(...)`. These should be equivalent; not verified against `fees.py` source.
- **`paper_mode.py` interaction** — Paper P&L credits at settlement (line 1567) and exit (line 972) use `paper_mode.add_pnl`. Not verified that the P&L amounts passed match what the ledger records.
- **Concurrent restart race** — Two bot processes starting simultaneously could both load paper positions, both replay queue, both credit P&L. The idempotency key in `apply_settlement_pnl` should prevent double-count, but the paper_positions.json write (atomic replace) could lose one process's position updates. Not analyzed deeply.
- **`config.AGGRESSIVE_FILL` type** — Assumed bool; `bool()` calls guard against string "true"/"false" from env.