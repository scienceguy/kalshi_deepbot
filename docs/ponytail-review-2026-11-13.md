What this change does:
Commit b296b74 replaces the paper-trading fill simulator (a random-number model with probability based on distance from mid, time left, and volatility) with a deterministic book-based rule: a resting dry-run order fills only when the live ask on its side is at or below the posted price, capped by the quoted COUNTERPARTY depth (buying YES lifts the NO bid, so depth = no_liquidity). No quote means no fill. Settlement P&L is unified through a single `_settle_pnl` helper used by the live path, the pending-settlement queue, and the replay queue, so a taker entry (AGGRESSIVE_FILL=true) pays the same Kalshi fee in paper as it does live. Exits now charge fees based on how the order was placed (entry: AGGRESSIVE_FILL; exit: PAPER_TAKER_EXIT_FEE for paper, always taker for live) and report effective cent-grid fees matching the P&L. The backtest engine skips markets with no quote instead of inventing a 45c entry price. Load assumed: one bot process, ~15s cycle, plus optional scripts; not many users.

**Must fix**

### 1. `_dry_run_fill_count` fills full size when ask exists but depth is zero (trader.py:656-661)

**What this is:** When `depth` (counterparty liquidity) is 0 or missing but `ask <= entry_price_cents`, the function returns `floor_contracts(contracts)` — the full order size — because "the crossing ask still proves a counterparty exists."

**Problem:** An ask with zero reported size can be a stale quote, a display artifact, or a market maker showing a price without willingness to trade size. Filling the full paper order on a zero-depth ask overstates fill rates and flatters the paper curve. In live trading, a zero-size ask means no counterparty is actually offering contracts at that price.

**Fix:** Add a config flag (e.g., `PAPER_FILL_ZERO_DEPTH`) defaulting to `False`. When depth is missing/zero, return `0.0` unless the flag is explicitly enabled. Or, more conservatively, always require `depth > 0` for a fill; the "ask proves counterparty" assumption is not safe for paper P&L that mirrors live.

**If we skip it:** Paper fills on phantom liquidity. The paper curve will show fills that would never happen live, especially in thin markets. The whole point of this commit — "paper fills come from the live book, never a random number" — is undermined if a zero-depth ask counts as a real fill.

---

### 2. Replay settlement uses current `AGGRESSIVE_FILL` config, not the value at trade time (trader.py:792-795, 689-712)

**What this is:** `_replay_settlement_queue` calls `_entry_fee_dollars(payload.get("contracts", 0), payload.get("entry_price", 0))`, which reads `config.AGGRESSIVE_FILL` at replay time. The payload does not store whether that specific trade was placed as a taker or maker.

**Problem:** If `AGGRESSIVE_FILL` changes between trade placement and replay (e.g., operator toggles it, or a dry-run session restarts with a different value), the replay will apply the wrong fee. A maker trade replayed with `AGGRESSIVE_FILL=True` will incorrectly charge a taker entry fee; a taker trade replayed with `AGGRESSIVE_FILL=False` will incorrectly waive the fee. The P&L booked on replay will not match the P&L that would have been booked live.

**Fix:** Store `entry_was_taker: bool` in the settlement queue payload at placement time (in the dry-run fill branch and the live fill-confirmation branch). In `_replay_settlement_queue` and `_resolve_pending_settlement`, pass this flag to `_settle_pnl` instead of reading the global config. Change `_settle_pnl` signature to accept `entry_taker: bool` explicitly.

**If we skip it:** Replay P&L becomes config-dependent rather than trade-dependent. A config toggle silently rewrites history. The pending queue and replay queue are crash-recovery paths — they must be deterministic and faithful to the original trade.

---

### 3. `_book_exit` uses current `AGGRESSIVE_FILL` for entry fee, not the value at entry (trader.py:889-891)

**What this is:** `_book_exit` computes `entry_taker = bool(config.AGGRESSIVE_FILL)` at exit time. The position dict does not record whether the entry was a taker.

**Problem:** Same as finding 2. If `AGGRESSIVE_FILL` flips between entry and exit, the exit-time fee calc uses the wrong entry mode. A position entered as maker (`AGGRESSIVE_FILL=False`) but exited when `AGGRESSIVE_FILL=True` will be charged a taker entry fee it never paid. Conversely, a taker entry replayed under `AGGRESSIVE_FILL=False` will show zero entry fee.

**Fix:** Add `entry_taker: bool` to the position dict when the position is created (in `_record_filled_order` and in the live sync-from-exchange path). Read it in `_book_exit` instead of the global config. This also fixes the live path, which currently has the same bug.

**If we skip it:** Early-exit P&L (and the fees reported in the order row) can diverge from the actual cash flows. The divergence is silent — the bot still runs — but the audit trail is wrong.

---

### 4. Settlement queue payload key inconsistency: `entry_price` vs `entry_price_cents` (trader.py:792-795, 1986-1995)

**What this is:** The dry-run fill branch stores `sig.entry_price` (which is in cents, e.g., 97) into the payload under key `entry_price`. The live fill-confirmation branch stores `sig.entry_price` under the same key. But `_replay_settlement_queue` reads `payload.get("entry_price", 0)` and passes it to `_entry_fee_dollars(..., entry_price_cents)`. The parameter name suggests cents, and the value is cents, so this works — but the naming is inconsistent with other parts of the codebase that use `entry_price_cents` explicitly.

**Problem:** Low risk today, but a maintenance hazard. A future edit that changes the payload key to `entry_price_cents` in one branch but not the other will silently break replay fees (defaulting to 0).

**Fix:** Standardize on `entry_price_cents` in the payload. Update both the dry-run branch and the live fill-confirmation branch to write `entry_price_cents`. Update `_replay_settlement_queue` to read `payload.get("entry_price_cents", 0)`.

**If we skip it:** A future refactor introduces a silent fee bug in the crash-recovery path.

---

**Should fix**

### 5. `backtest_engine.py` final_taker strategy uses `ya` (yes_ask) for NO side entry price via `100 - bid_at_entry` (backtest_engine.py:106-108)

**What this is:** For the NO side, the code computes `ep = 100 - bid_at_entry` where `bid_at_entry` is the YES bid. This assumes NO ask = 100 - YES bid.

**Problem:** Kalshi's book structure: buying YES lifts the NO bid, so YES ask = 100 - NO bid. The NO ask is a separate quote, not necessarily 100 - YES bid (the spread on each side can differ). Using `100 - YES bid` as a proxy for NO ask introduces a pricing error that compounds over thousands of backtest trades.

**Fix:** When minute_quotes are available, read the NO ask directly from the quote data (the quote tuple should include both sides). If the quote format only stores (yes_bid, yes_ask), fetch NO bid/ask from the market snapshot. Do not derive NO ask from YES bid.

**If we skip it:** Backtest entry prices for NO trades are approximate. The error is small when spreads are tight but grows in volatile windows. The backtest claims "never invents an entry price" but derives one for NO.

---

### 6. `_dry_run_fill_count` has no test for the zero-depth-but-ask-exists case (tests/test_trader.py:1049-1060)

**What this is:** The test `test_dry_run_fill_uses_counterparty_depth_not_our_own_bid` verifies the depth cap works when depth > 0. There is no test for `depth=0, ask<=price`.

**Problem:** The current behavior (return full contracts) is the riskiest branch in the new fill logic. It is untested and undocumented as a configurable assumption.

**Fix:** Add a test: `m = _book(yes_ask=96, yes_liq=999, no_liq=0); assert trader._dry_run_fill_count(m, "yes", 96, 10) == 0.0` (if conservative) or `== 10.0` (if optimistic, with a comment explaining why). Either way, the test pins the behavior so a future change cannot silently flip it.

**If we skip it:** The zero-depth behavior can change accidentally. The test suite won't catch it.

---

### 7. `paper_state_precutover_2026-11-13.json` created in repo root, not gitignored (git status)

**What this is:** The commit adds `paper_state_precutover_2026-11-13.json` as a new untracked file in the working tree (shown in the diff as a new file). It is not listed in `.gitignore`.

**Problem:** This file contains pre-cutover paper curve data for audit purposes. It should either be committed (if it's a permanent audit record) or ignored (if it's a one-off artifact). Leaving it untracked clutters `git status` and risks accidental commit.

**Fix:** If it's an audit record, add it to the repo with a clear commit message. If it's a transient artifact, add `paper_state_precutover_*.json` to `.gitignore`.

**If we skip it:** Noise in `git status`; potential accidental commit of a file that serves no runtime purpose.

---

### 8. `scripts/maker_fill_test.py` uses `config.AGGRESSIVE_FILL = False` globally but imports trader after mutating config (scripts/maker_fill_test.py:28-31)

**What this is:** The script sets `config.AGGRESSIVE_FILL = False` and `config.DRY_RUN = True` before importing `trader`. However, `trader.py` imports `config` at module level and may have already cached values or initialized module-level state based on the original config.

**Problem:** If `trader.py` reads `config.AGGRESSIVE_FILL` at import time (e.g., for a default argument or module-level constant), the mutation comes too late. The script uses `trader._dry_run_fill_count` and `trader._settle_pnl`, which read `config.AGGRESSIVE_FILL` at call time (they're functions), so this works today. But it's fragile — a future change to `trader.py` that reads the config at import time would silently break the script.

**Fix:** Move the config mutation before the `import trader` line (already done via the `sys.argv` trick), or better, pass the mode explicitly to the functions instead of relying on global config. Since `_dry_run_fill_count` and `_settle_pnl` are pure functions of their arguments plus `config.AGGRESSIVE_FILL`, add an `aggressive_fill` parameter to both with a default of `config.AGGRESSIVE_FILL` for backward compatibility. The script can then pass `aggressive_fill=False` explicitly.

**If we skip it:** Fragile dependency on import-order side effects. A refactor of `trader.py` could break the script silently.

---

**Nice to have**

### 9. `_dry_run_fill_count` zero-depth behavior should be configurable (trader.py:656-661)

**What this is:** The optimistic fill-on-zero-depth assumption is a policy choice, not a law of physics.

**Fix:** Add `PAPER_FILL_ZERO_DEPTH = _env_bool("PAPER_FILL_ZERO_DEPTH", False)` to `config.py`. In `_dry_run_fill_count`, check this flag before returning full contracts on zero depth. Document the trade-off in the comment.

**If we skip it:** The behavior is hardcoded. Operators who want conservative paper fills must edit the source.

---

### 10. `scripts/maker_fill_test.py` not integrated into test suite or CI (scripts/maker_fill_test.py)

**What this is:** A valuable live-book validation tool that exercises the exact fill and settle logic the paper trader uses.

**Fix:** Add a pytest marker (e.g., `@pytest.mark.integration`) and a test that runs the script in a subprocess with a short `--minutes 1` against a mock client, or add it as a manual integration step in the README. At minimum, document it in `docs/` so operators know it exists.

**If we skip it:** The tool is discoverable only by reading the scripts directory. It bit-rots unnoticed.

---

### 11. `docs/ponytail-audit-2026-11-13.md` tracked via `.gitignore` negation but not listed explicitly (`.gitignore:112-113`)

**What this is:** The `.gitignore` adds `!docs/*.md` with the comment "Audit reports written under docs/ are part of the record." The audit file is tracked because of this wildcard negation.

**Problem:** The wildcard `!docs/*.md` will track *any* `.md` file created under `docs/`, including future unrelated notes. The existing pattern for other tracked docs (MEMPALACE_GUIDE.md, TOOL_AWARENESS_GUIDE.md, PONYTAIL_AUDIT.md) lists each file explicitly.

**Fix:** Either list `docs/ponytail-audit-2026-11-13.md` explicitly in `.gitignore` (like the other docs) and remove the wildcard, or keep the wildcard but add a comment that only audit reports belong in `docs/`. The explicit list is safer.

**If we skip it:** A stray `docs/notes.md` gets tracked accidentally. Low impact.

---

**Verdict:** The commit is a significant improvement — it replaces a random fill simulator with a deterministic book-based rule and unifies settlement fee logic across three code paths. The tests are thorough (436 pass). The critical issues are the config-dependent replay/exit fees (findings 2, 3) and the optimistic zero-depth fill (finding 1), which can silently distort paper P&L and crash-recovery accounting. These should be fixed before the next live session.

**Lean: -0 lines, -0 dependencies possible.** (The changes add necessary functions and tests; no dead code to remove.)

**Not checked:** Whether the live `kalshi_client.get_orders` returns `fill_count_fp` reliably for all order states (the fill-confirmation tests mock this). Whether the `PAPER_TAKER_EXIT_FEE` default `True` matches the live maker exit fee model (the comment says it does). Whether the `maker_fill_test.py` script's fill logic (polling the book every 15s) matches the trader's cycle (also ~15s) closely enough to be a faithful simulation. Whether the backtest engine's `final_taker` strategy correctly models the 60-second BRTI settlement average (the engine holds to settlement at $1.00/$0.00, which assumes the BRTI average resolves to the strike — correct for a binary that has already crossed the strike, but the engine doesn't verify the strike was crossed).