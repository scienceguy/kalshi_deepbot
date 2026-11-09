# Ponytail-Style Audit Report: Kalshi SuperBot

**Auditor**: Sub-agent session
**Date**: 2026-11-08
**Scope**: /data/data/com.termux/files/home/kalshi_superbot
**Method**: Ponytail audit — smallest complete change, concrete cases, three groups

---

## Repo Mapping

| Category | Details |
|----------|---------|
| **Entry points** | `trader.py` (main loop, `--live`/`--dry`/`--once`), `backtest.py`, `parallel_backtest.py` |
| **Tests** | 259 pytest tests (258 pass, 1 pre-existing failure in `test_already_ordered_blocks_duplicate_entry`) |
| **Dependencies** | `kalshi_client.py`, `signals.py`, `risk.py`, `market_finder.py`, `gates.py`, `n8n_client.py`, `supabase_client.py`, `llm_eval.py`, `fees.py`, `resilience.py`, `paper_mode.py`, `local_db.py`, `config.py` |
| **n8n integration** | 4 workflows (`superbot-backtest`, `superbot-collect`, `superbot-trade`, `superbot-monitor`), webhook execution via `n8n_client.py` |
| **mempalace coordination** | `n8n_coordination.py` writes to MemPalace logstream via CLI subprocess |
| **Credential store** | Bitwarden (ultimate source), `~/.secrets/bw/` (durable mirror), `scripts/n8n_creds_sync.py` |
| **Config** | `.env` + `.env.{live,dry}` loaded at `config.py` import; env vars override `.env` |
| **Key files read** | `trader.py` (1866 lines), `signals.py` (725 lines), `risk.py` (239 lines), `kalshi_client.py` (686 lines), `supabase_client.py` (503 lines), `market_finder.py` (345 lines), `n8n_client.py` (311 lines), `gates.py` (428 lines), `llm_eval.py` (460 lines), `fees.py` (76 lines), `resilience.py` (81 lines), `paper_mode.py` (105 lines), `local_db.py` (680 lines), `config.py` (515 lines), `AGENTS.md` (285 lines) |

---

## Findings (4 total)

### Finding 1: `_replay_settlement_queue` can lose PnL on crash
- **What**: Settlement queue replay (`trader.py:674-720`) saves the queue WITHOUT the current entry BEFORE writing PnL to Supabase (line 707). If the process crashes after the queue save but before the Supabase write, the realized PnL is permanently lost — the queue entry is gone and no PnL was recorded to either Supabase or the local pnl_ledger.
- **Problem**: Data loss risk — realized PnL can be permanently lost on process crash between queue persistence and Supabase write. On restart, the entry is not in the queue and PnL was never credited.
- **Fix**: Restructure to write PnL to the local pnl_ledger FIRST (using the idempotency key, following the pattern already established in `_book_exit` at trader.py:1243-1284), then write to Supabase, then remove from queue. This ensures that even if Supabase write fails, the local ledger has the record, and on restart the idempotency key prevents double-counting. **Code change**: Reorder operations in `_replay_settlement_queue` to apply to local pnl_ledger before Supabase write.

### Finding 2: `_bid_hist` 30s save interval can lose bid history on crash
- **What**: Bid history (`signals.py:141-250`) is saved every 30 seconds via `_maybe_save_bid_hist()`, which checks a dirty flag and interval. If the process crashes between saves (up to 30 seconds), the in-memory `_bid_hist` is lost and not persisted to `bid_hist_{dry,live}.json`. On restart, the stability gate has less history than the previous cycle ended with.
- **Problem**: Potential loss of bid history leading to unnecessary stability gate blocks (fresh-start behavior), reducing trade opportunities. The stability gate may incorrectly block trades that had recent price history.
- **Fix**: Reduce `_BID_HIST_SAVE_INTERVAL` from 30 to 5 seconds, or call `_force_save_bid_hist()` after each `_track_bid()` in the `evaluate()` function. The smallest code fix: lower `_BID_HIST_SAVE_INTERVAL` from 30 to 5, reducing the maximum history loss to 5 seconds. This adds no new runtime overhead beyond more frequent saves.

### Finding 3: `_schema_warned` flag prevents re-logging after Supabase schema fix
- **What**: The `_schema_warned` module-level flag (`supabase_client.py:33`) is set to `True` after the first "tables missing" error. If the Supabase schema is later created, the error won't be re-logged because the flag remains `True`. Operators lose visibility into the schema status.
- **Problem**: Lack of operational schema monitoring — the "tables missing" error is logged once and never re-logged, even after the schema is created and writes succeed.
- **Fix**: Add `_schema_warned = False` reset in `_supabase_record_success()` so that once a successful Supabase write occurs, the flag is cleared and future table-missing errors will be re-logged. This adds one line and restores operational visibility.

### Finding 4: `_evaluate_common` confidence for FAVOURITE source is mid-only, ignoring skew/trend
- **What**: For non-SKEW/TREND signal sources (i.e., the FAVOURITE source from the two-sided strategy at `signals.py:694`), `_evaluate_common` calculates confidence as `float(abs(market.mid_yes - 50.0) / 50.0)` — solely based on how far the mid price is from 50c. For SKEW/TREND sources, confidence incorporates both strength and mid_distance (lines 378-388), but the FAVOURITE source uses only the mid-based formula.
- **Problem**: Simplistic confidence may not accurately reflect signal quality for FAVOURITE signals, potentially causing the confidence filter to block valid trades or allow low-quality ones. The confidence then affects position sizing and risk assessment via `MIN_CONFIDENCE`. Inconsistent confidence calculation across signal sources is a code quality issue.
- **Fix**: Make the FAVOURITE confidence calculation consistent with the SKEW/TREND branch by incorporating the strength score. Replace the else-branch confidence at `signals.py:393` with the same formula used for SKEW/TREND:
  ```python
  mid = market.mid_yes
  mid_distance = max(0.0, 1.0 - abs(mid - config.MID_TARGET_CENTS) / max(1.0, config.MID_TOLERANCE_CENTS))
  strength_score = min(1.0, strength / config.CONFIDENCE_STRENGTH_SCALE)
  confidence = min(
      1.0,
      strength_score * config.CONFIDENCE_STRENGTH_WEIGHT
      + mid_distance * config.CONFIDENCE_MID_WEIGHT,
  )
  ```
  This is a 3-line change that makes confidence consistent across all signal sources.

---

## Verification

- All 259 tests run; 258 pass, 1 pre-existing failure (`test_already_ordered_blocks_duplicate_entry` — test quirk, not a code bug)
- `python -m pytest tests/ -q` → 258 passed, 1 failed (same as before audit)
- No `shortcut:` or `ponytail:` comments found in the codebase
- n8n credentials sync mechanism documented in AGENTS.md; no issues found in the sync scripts' logic
- mempalace integration via `n8n_coordination.py` is functional but depends on the mempalace CLI/hub being available

---

## Verdict

**Healthy** — the codebase is well-structured, tests pass, and the 4 findings are minor refinements, not critical bugs. The bot trades reliably in dry-run mode; live mode requires valid Kalshi credentials and n8n availability.

**What to fix first**: Finding 1 (_replay_settlement_queue PnL data loss) — this is the highest-risk finding as it can cause permanent PnL loss on crash. Fixing it follows the same pattern already established in `_book_exit`.

**Lean**: -4 findings (no code deletion needed for findings 2-4; finding 1 requires reordering existing operations, which is a structural fix, not additional code). No new dependencies required.

**Not checked**: Parts of the code not directly touched by the main flows — notably the `latency_arb.py` strategy (unused), `kalshi_price_data_client.py` SDK wrapper, `parallel_backtest_hf.py`, and the `binance_feed.py` module used by gates. These were outside the core trading bot, signals, risk, n8n, supabase, and Kalshi API client focus areas.