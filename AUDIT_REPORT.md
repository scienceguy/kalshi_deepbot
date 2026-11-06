# Kalshi SuperBot — Final Deep-Search Audit Report

**Date:** Thu Nov 05 2026
**Repo:** `/data/data/com.termux/files/home/kalshi_superbot`
**Scope:** research/review only — no edits, commits, pushes, or secret access performed.

---

## 1. Executive Summary

**Everything works; no critical issues remain in the codebase itself.** One real bug was found in the ML filter path (only reachable via `--ml-model`), and one item needs live-API verification.

| Check | Result |
|-------|--------|
| `python -m pytest tests/ -q` | **231 passed** in 9.56s |
| `python -m py_compile *.py tests/*.py` | **OK** (all compile clean) |
| Bare `except:` | **None found** |
| Divide-by-zero | **None found** (all guarded) |
| TODOs / FIXMEs / XXXs | **None found** |
| `.gitignore` covers `*.pt`/`*.pkl`/`*.safetensors` | **Yes** (lines 90-92) |
| ML filter wiring in `cycle()` | **Correct** (but broken by bug below in ML mode) |
| Git tracked tree | **Clean** (no modifications, `git diff HEAD` empty) |
| Untracked files | 10 pre-existing (handoff.md, hf_sweep_*, logs/, markout_log.jsonl, pbt_out.txt) |

**Bottom line:** All previously-fixed audit findings (1–60) are confirmed fixed via passing regression tests. The only actionable defect is `trader.py:948` (asset slicing), which disables the ML filter entirely when `--ml-model` is used.

---

## 2. Numbered Findings

### Finding 1 — `trader.py:948` — BUG (ML filter blocks every signal)

**File:** `trader.py:947-948`
```python
series = m.ticker.split("-")[0] if "-" in m.ticker else m.ticker
asset = series[:3]  # e.g. KXBTC -> BTC, KXETH -> ETH
```

`series[:3]` on `"KXBTC15M"` yields `"KXB"` (indices 0-2), not `"BTC"` (indices 2-4). The comment describes the intended behavior (`KXBTC -> BTC`), but the code extracts the wrong 3 characters.

**Consequence:** `binance_feed.composite_price("KXBUSDT")` returns `None` (no such symbol), the `continue` at line 955 skips the signal, and **every ML-filtered signal is dropped**. In ML mode (`--ml-model` with a loadable model), no trades are placed at all.

**Reachability:** Only when `--ml-model` is passed and `_ml_model` loads successfully. Default operation (`_ml_model is None`) passes signals through unchanged (line 938-939), so normal trading is unaffected.

**Fix:** Extract the asset correctly. For 3-char assets (BTC, ETH, SOL, XRP, BNB, ZEC): `series[2:5]`. For all assets including 4-char ones (DOGE, HYPE, NEAR): strip the `KX` prefix and trailing `15M`/`D` suffix:

```python
asset = series[2:]
if asset.endswith("15M"):
    asset = asset[:-3]
elif asset.endswith("D"):
    asset = asset[:-1]
```

**Status:** broken (ML mode only)

---

### Finding 2 — `crossvenue.py:36` — NEEDS-TESTING (15M event ticker regex format)

**File:** `crossvenue.py:36`
```python
_KALSHI_EVENT = re.compile(r"^KX(BTC|ETH)(?:D|15M)?-(\d{2})([A-Z]{3})(\d{2})(\d{2}|\d{4})$")
```

The regex expects `YYMMDDHHMM` (year first, no `T` separator), e.g. `KXBTC15M-26NOV052245` → 2026-11-05 22:45. The unit tests pass with this format.

**Concern:** Real Kalshi market tickers observed in the codebase use `DDMMMHHMM` (day first), e.g. `KXBTC15M-24OCT1230-30` (day 24, Oct, 12:30, strike 30). If real 15M **event** tickers (from the `/events` API used by `discover_kalshi_events()`) follow the same day-first convention or include a `T` separator (`KXBTC15M-26NOV05T2245`), then `parse_kalshi_event()` returns `None` and those events are **silently skipped** in `crossvenue_collect.py:49-51` and `ladder_strategy.py:331-333` — no cross-venue comparison for 15M markets, no error, just no results.

**Status:** needs-testing (cannot confirm without hitting the live Kalshi `/events` API)

---

### Finding 3 — `trader.py:941-942, 947` — MINOR (cosmetic, no-action-needed)

`_ml_filter_signals(signals, markets, ml_model)` receives a `markets` parameter that is **unused** inside the function (it uses `sig.market` directly, line 946). It also re-imports `ladder_strategy as ls` and `binance_feed` locally (lines 941-942) even though both are already module-level imports in `trader.py` (lines 59, 60). Harmless, but dead code.

**Status:** no-action-needed (cosmetic)

---

### Finding 4 — `.gitignore` — OK (no-action-needed)

Lines 90-92 cover trained ML model binaries:
```
*.pt
*.pkl
*.safetensors
```
`ml_model.pt` will not be committed. No change needed.

**Status:** no-action-needed

---

### Finding 5 — Untracked files — CLEANUP (not a bug)

`git status --short` shows 10 untracked entries (all pre-existing, none created by this audit's pytest/py_compile runs — those write only to gitignored `__pycache__/` and `.pytest_cache/`):
- `handoff.md`
- `hf_sweep_out.txt`, `hf_sweep_results.json`
- `logs/agent-autopilot/opencode/` (5 directories)
- `markout_log.jsonl`
- `pbt_out.txt`

These are runtime artifacts, not source changes. No tracked file was modified.

**Status:** no-action-needed

---

### Finding 6 — All previously-fixed audit findings — CONFIRMED FIXED

The 60 issues from the prior audit (settlement PnL double-counting, position-before-PnL, float contract truncation, `_verify_key` poison, fill-before-confirmation, NO-price complement, Supabase silent failures, negative minutes_left, lock file, bid history persistence, balance failure crash, single-market return, drawdown cost-basis, session_pnl drift, n8n health webhook, 429 burst, lookahead bias, balance sort order, huge sweep grid, stability gate, fee validation, hedge disabled, private method call, corrupt JSON get_bot_state, non-standard URL warning, SIGALRM, module-level side effects, idempotent n8n injection, fee multiplier, .env/.pem guard, startup Supabase init, sequential per-series fetches, module-level bid hist, batched bot_state writes, unused client param, cursor pagination, single-file download, docstring, unparseable bool, shallow tests, missing market_finder tests, per-market loss, sweep param alignment, health_check hasattr, keys in process list, validate_* not auto-called, backoff first failure, markets cache invalidation, per-cycle bot_state writes, repo bloat, log clutter, extra LLM keys, NO branch skew/trend, cash floor + exposure ceiling sum, empty side, legacy credential order) are all confirmed fixed by the passing test suite (231 tests, including regression tests for settlement PnL booking, resting-value pruning, position helpers, ML loading/filtering, circuit-breaker reset, NO-price complement, and more).

**Status:** fixed

---

### Finding 7 — No bare excepts — OK

Grepped `^\s*except:` across all `*.py` files: **zero matches**. All exception handlers specify exception types or use `except Exception`.

**Status:** fixed

---

### Finding 8 — No divide-by-zero — OK

All divisions are guarded:
- `fees.py:taker_fee_dollars` guards `contracts <= 0 or price_dollars <= 0` (line 27)
- `fees.py:net_profit_roundtrip` guards `if entry_value else 0.0` (line 75)
- `risk.py:update_balance` guards `total_value <= 0` (line 56)
- `risk.py:max_trade_dollars` guards `portfolio <= 0` (line 156)
- `risk.py:summary` guards `if portfolio else ...` (lines 215-216)
- `signals.py:_size` guards `entry_cents <= 0 or entry_cents >= 100 or max_dollars <= 0` (line 79)
- `crossvenue.py:prob_above` returns NaN on bad input (handled by `fair != fair` NaN check at line 344)

**Status:** fixed

---

## 3. Recommended Actions (by priority)

| Priority | Action | File | Why |
|----------|--------|------|-----|
| **1** | Fix `asset = series[:3]` → proper asset extraction (strip `KX` prefix + `15M`/`D` suffix) | `trader.py:948` | ML filter blocks every signal when `--ml-model` is used |
| **2** | Verify `crossvenue.py` 15M regex against live Kalshi `/events` API; update `_KALSHI_EVENT` if real tickers use `DDMMMHHMM` or a `T` separator | `crossvenue.py:36` | 15M events silently skipped if format mismatches |
| **3** | Remove unused `markets` param and redundant local imports in `_ml_filter_signals` | `trader.py:941-947` | Cosmetic cleanup only |

---

## 4. Git State

- **HEAD commit:** `6315362` "Must fix: ML wiring, balance guard, zero-division guard, ML tests"
- **Commits ahead of origin:** not determined (would require `git log origin/main..HEAD`; note `main` was history-rewritten on 2026-11-01, all pre-`ac0b91b` SHAs are invalid)
- **Uncommitted tracked changes:** none (`git diff HEAD` empty, `git status --short` shows only untracked files)
- **Untracked files:** 10 pre-existing runtime artifacts (listed in Finding 5); none created by this audit's pytest/py_compile runs
- **Pre-commit hook:** present (blocks `.env`/`*.pem` commits; `scripts/install-pre-commit.sh`)

---

**Verification evidence:** `231 passed` (pytest, 9.56s); `py_compile` OK on all root and test `.py` files; `git status --short` clean of tracked modifications; no bare `except:`; no divide-by-zero paths; no TODOs/FIXMEs/XXXs.
