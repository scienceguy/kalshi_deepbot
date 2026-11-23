T17-06 DRY/LIVE PARITY AUDIT
============================
Repo: /opt/superbot  HEAD: 884b70d (T17-01/03/04 evidence)
Agent: ubuntu:opencode:kalshi_deepbot  (subagent of a parent session)
Mode: read-only audit. No orders, no live, no balances, no secrets, no
network to venues, no git mutations. Only project-tree write: this doc.
v17 rule (AGENT_TASKSv17.md directive 6, lines 46-50): one code path and one
config between dry and live. Only the execution adapter, key selection, state
namespace and log paths may differ. Remove PAPER_PER_TRADE_PCT and all
dry-only sizing. Log a config hash; live refuses to start unless it equals
the dry-validated hash.

SCOPE OF REVIEW
---------------
Read (not modified): config.py, trader.py, risk.py, signals.py, gates.py,
fees.py, sizing.py, poly_us_trader.py, conftest.py.
Tests run: tests/test_config.py, tests/test_config_consistency.py,
tests/test_risk.py, tests/test_gates.py.
Command:
  .venv/bin/python -m pytest tests/test_config.py tests/test_config_consistency.py tests/test_risk.py tests/test_gates.py -q
Output: 123 passed in 0.85s, exit 0.

Single config module confirmed: config.py is the only config source. There is
no separate dry config file and no live config file. Defaults come from
config.py; runtime values come from env/.env overrides applied uniformly.
Runtime import (no .env read directly; values observed via config module):
  DRY_RUN=True  NO_ONLY=True  ALLOW_NO_BUYS=True (env override; code default False)
  PAPER_PER_TRADE_PCT=0.15  PER_TRADE_PCT=0.08  BALANCE_PCT_PER_TRADE=0.08
  DRY_STARTING_BALANCE=100.0
Code defaults read from config.py:
  DRY_RUN=_env_bool("DRY_RUN", True)                    config.py:817
  NO_ONLY=_env_bool("NO_ONLY", True)                    config.py:592
  ALLOW_NO_BUYS=_default_allow_no_buys(...) -> False    config.py:587, 94-101
  PER_TRADE_PCT=_env_float("PER_TRADE_PCT", 0.05)       config.py:704
  BALANCE_PCT_PER_TRADE=_env_float(..., 0.08)           config.py:692
  PAPER_PER_TRADE_PCT=_env_float("PAPER_PER_TRADE_PCT", 0.12) config.py:722
  DRY_STARTING_BALANCE=_env_float("DRY_STARTING_BALANCE", 100.0) config.py:820


ITEM 1 - ONE CODE PATH AND ONE CONFIG (dry vs live)
-----------------------------------------------------
Result: PARTIAL

Evidence:
- ONE config: PASS. Single config module config.py; no dry/live config split.
  config.py:817 DRY_RUN default True; all other knobs are shared defaults.
- Code path: PARTIAL. DRY_RUN / paper branches exist beyond the four allowed
  categories (execution adapter, key selection, state namespace, log paths).
  Allowed branches (execution adapter / state / log paths):
    trader.py:153   log target = LOG_FILE vs DRY_LOG_FILE             (log path - allowed)
    trader.py:190,200,214  paper positions load/save gated on not DRY_RUN (state namespace - allowed)
    trader.py:1329-1330 paper_mode.add_pnl only if config.DRY_RUN     (paper ledger/state - allowed)
    signals.py:249  bid_hist file name 'dry' vs 'live'                (data path - allowed)
    fees.py:104-111 unknown fee_type: refuse live, warn dry           (execution adapter - allowed)
  Disallowed branches (strategy/sizing/risk read the mode flag):
    risk.py:466     per_trade_pct = PAPER_PER_TRADE_PCT if paper else PER_TRADE_PCT
    sizing.py:127   _paper_constraints uses PAPER_PER_TRADE_PCT; _live_constraints uses PER_TRADE_PCT (sizing.py:145)
    trader.py:3448,3480  startup log/params use PAPER_PER_TRADE_PCT if DRY_RUN else PER_TRADE_PCT
    risk.py:131,450,462-464  paper-only drawdown halt, cash floor, exposure ceiling
    trader.py:1752  filled = floor_contracts if config.DRY_RUN else _order_fill_count
    trader.py:1606,1775  further DRY_RUN-gated fill/exit logic
Verdict: the config is single, but the code path is NOT mode-blind in the
sizing/risk layer. PAPER_PER_TRADE_PCT (dry) vs PER_TRADE_PCT (live) is a
real sizing divergence, not merely an adapter/state/log difference.


ITEM 2 - PAPER_PER_TRADE_PCT AND ANY DRY-ONLY SIZING / STRATEGY / RISK SETTINGS
--------------------------------------------------------------------------------
Result: FAIL

PAPER_PER_TRADE_PCT found (dry-only sizing knob):
  config.py:722  PAPER_PER_TRADE_PCT = _env_float("PAPER_PER_TRADE_PCT", 0.12)
  (runtime override observed: 0.15 dry vs PER_TRADE_PCT 0.08 live)
Consumed by:
  risk.py:466   per_trade_pct = config.PAPER_PER_TRADE_PCT if paper else config.PER_TRADE_PCT
  sizing.py:127 per_trade_pct=config.PAPER_PER_TRADE_PCT   (_paper_constraints)
  sizing.py:145 per_trade_pct=config.PER_TRADE_PCT          (_live_constraints)
  trader.py:3448  (config.PAPER_PER_TRADE_PCT if config.DRY_RUN else config.PER_TRADE_PCT) * 100
  trader.py:3480  "per_trade_pct": config.PAPER_PER_TRADE_PCT if config.DRY_RUN else config.PER_TRADE_PCT
  scripts/dry_compound.py:81   --pct default=config.PAPER_PER_TRADE_PCT
  scripts/dryrun_growth_sim.py:492 trade_d = min(b * config.PAPER_PER_TRADE_PCT, config.MAX_TRADE_DOLLARS)

Other dry-only sizing/risk settings (config.py paper block):
  PAPER_CASH_FLOOR_PCT=0.70   config.py:720  (same value as live, but a separate paper knob)
  PAPER_EXPOSURE_CEILING_PCT=0.30  config.py:721
  PAPER_MIN_CASH_ABSOLUTE=0.02  config.py:723
  PAPER_GUARDS_ENABLED=True   config.py:741  (risk.py:44-51 _paper_guards_relaxed bypasses live guards)
  PAPER_DRAWDOWN_HALT_PCT=0.90 config.py:742  vs DRAWDOWN_HALT_PCT=0.08 config.py:743 (risk.py:131)
  DRY_STARTING_BALANCE=100.0  config.py:820  (dry-only bankroll; live reads venue balances at runtime)

No test asserts PAPER_PER_TRADE_PCT == PER_TRADE_PCT or that risk/sizing ignore
the mode flag. v17 requires deleting PAPER_PER_TRADE_PCT and all dry-only
sizing so live matches the dry-validated config.

NO_ONLY / ALLOW_NO_BUYS (strategy, not dry-only):
  config.py:592 NO_ONLY=True   config.py:587 ALLOW_NO_BUYS=False
  signals.py:70  NO_ONLY blocks YES entries at TradeSignal.is_trade (single choke point)
  signals.py:594,819 ALLOW_NO_BUYS gating
  These are mode-independent and consistent between dry and live. PASS for this sub-item.


ITEM 3 - CONFIG HASH LOGGING AND LIVE-REFUSE-UNLESS-HASH-MATCHES-DRY
---------------------------------------------------------------------
Result: FAIL (gap, not implemented)

config.py: no config-hash function exists.
trader.py: no config hash computed or logged at startup; main() logs startup
params (trader.py:3467-3484) but no hash and no refuse-unless-match check.
trader.py:3403-3408 only refuses when DRY_RUN=false without --live -- that is
a dry/live mode guard, NOT a config-hash equality check.

Partial analogue exists only for Polymarket US:
  poly_us_trader.py:93-109  _mp_config_hash() (sha256 over sorted non-credential config attrs, truncated to 16 hex)
  poly_us_trader.py:113     _MP_CONFIG_HASH computed at import
  poly_us_trader.py:747,778,848  config_hash logged in outbox/status payloads
  tests/test_m2_poly_mempalace.py:81,104  asserts payload config_hash == _MP_CONFIG_HASH
  NO comparison to a dry-validated reference hash and NO live-refuse-on-mismatch
  anywhere. Item 3 requirement ("live refuses to start unless hash equals the
  dry-validated hash") is unimplemented for both venues.

Gap to close: add a single config_hash() over the resolved config (excluding the
allowed-differing keys: execution-adapter/key/state/log-path values), log it at
trader.py startup, and make live refuse to start unless it equals the
dry-validated hash stored from the dry run. Same pattern should apply to the
Polymarket trader for consistency.


ITEM 4 - DRY VS LIVE CONFIG DEFAULTS COMPARISON
-----------------------------------------------
Result: PARTIAL

Single source of defaults (config.py), so there are no separate dry-default
and live-default blocks. Mode-independent defaults are identical by
construction:

  NO_ONLY=True            config.py:592   (same dry & live)
  ALLOW_NO_BUYS=False     config.py:587,94-101   (same dry & live; code default off)
  DRY_RUN=True            config.py:817   (this IS the mode flag itself)
  ENTRY_BAND_MIN/MAX=95/99 config.py:581-582   (shared)
  ENTRY_WINDOW 6-11 min   config.py:??? (shared; validated by tests/test_config_consistency.py)
  Gates: all config.GATES_* shared; gates.py has no DRY_RUN branches. PASS.
  PER_TRADE_PCT=0.05      config.py:704   (live)
  BALANCE_PCT_PER_TRADE=0.08 config.py:692   (live, used by signals.py:737,880,899,931,989,991)
  PAPER_PER_TRADE_PCT=0.12  config.py:722   (dry only)  <-- DIVERGENCE
  PAPER_CASH_FLOOR_PCT/EXPOSURE_CEILING_PCT/MIN_CASH_ABSOLUTE  config.py:720-723 (dry-only knobs)
  PAPER_DRAWDOWN_HALT_PCT=0.90 vs DRAWDOWN_HALT_PCT=0.08  config.py:742 vs 743 (dry-only)
  DRY_STARTING_BALANCE=100.0 config.py:820  (dry-only bankroll)
  PAPER_GUARDS_ENABLED=True config.py:741  (dry-only guard relaxation)

Divergences to remove per v17: PAPER_PER_TRADE_PCT, the PAPER_* cash floor /
ceiling / min-cash sizing knobs, PAPER_DRAWDOWN_HALT_PCT, PAPER_GUARDS_ENABLED,
DRY_STARTING_BALANCE. Live and dry must use ONE sizing policy (PER_TRADE_PCT /
BALANCE_PCT_PER_TRADE) and ONE risk policy.


ITEM 5 - FOCUSED CONFIG/GATE TESTS
----------------------------------
Result: PASS

Command: .venv/bin/python -m pytest tests/test_config.py tests/test_config_consistency.py tests/test_risk.py tests/test_gates.py -q
Output: 123 passed in 0.85s, exit 0.

Coverage: config env parsing (test_config.py), band/window/sizing single
definition (test_config_consistency.py:45-82), risk sizing/guards/halts
(test_risk.py), gate ATR/sigma/EV/time-decay/book-age (test_gates.py).
No existing test covers config-hash equality, live-refuse-on-hash-mismatch, or
PAPER_PER_TRADE_PCT == PER_TRADE_PCT parity. Those are test gaps to add with
any fix (TDD).


REQUIRED MINIMAL FIX (documented, NOT implemented here)
-------------------------------------------------------
Per the tasking, this is a read-only audit; money-path diffs require
independent review (R3) and no git mutations were permitted in this session.
The minimal parity fix, to be done by the reviewing agent with TDD:

1. TDD test (new, e.g. tests/test_parity_sizing.py):
   - assert config.PAPER_PER_TRADE_PCT == config.PER_TRADE_PCT  (or that PAPER_PER_TRADE_PCT is removed)
   - assert risk.max_trade_dollars(...) is identical for paper=True and paper=False when the same cash/exposure/win_prob are passed (sizing must not branch on mode)
   - assert a config_hash() exists and that live startup refuses when hash != dry-validated reference hash
   Run: watch it fail.

2. Fix:
   - Remove PAPER_PER_TRADE_PCT (config.py:722) and its consumers: risk.py:466,
     sizing.py:127, trader.py:3448, trader.py:3480 -> use PER_TRADE_PCT / live knobs for both modes.
   - Collapse PAPER_CASH_FLOOR_PCT/PAPER_EXPOSURE_CEILING_PCT/PAPER_MIN_CASH_ABSOLUTE into the live knobs (risk.py:462-466, sizing.py:124-127).
   - Remove DRY_STARTING_BALANCE (config.py:820) and PAPER_GUARDS_ENABLED / PAPER_DRAWDOWN_HALT_PCT paper-only relaxation (risk.py:44-51,131).
   - Add config_hash() and live-refuse-on-mismatch (config.py + trader.py main; mirror poly_us_trader pattern).
   Keep only allowed mode branches: execution adapter, key selection, state
   namespace, log paths.

3. Verify: focused tests above + .venv/bin/python scripts/verify_source.py --all
   + ASCII check on changed files, then STOP for independent review; do not
   commit the money-path diff.


REMAINING GAPS
--------------
1. PAPER_PER_TRADE_PCT dry-only sizing not removed (Item 2 FAIL).
2. Config hash not logged at Kalshi trader startup; no live-refuse-unless-hash-equals-dry (Item 3 FAIL). Polymarket logs _MP_CONFIG_HASH but also lacks the refuse-on-mismatch enforcement.
3. No test for config-hash equality or PAPER_PER_TRADE_PCT == PER_TRADE_PCT parity (Item 5 test gap).
4. PAPER_* cash floor/ceiling/min-cash, PAPER_DRAWDOWN_HALT_PCT, PAPER_GUARDS_ENABLED, DRY_STARTING_BALANCE are dry-only risk/sizing knobs still present.
5. Runtime .env overrides PAPER_PER_TRADE_PCT=0.15 vs PER_TRADE_PCT=0.08 (observed via config import; .env contents not read, no secrets exposed). Confirms the divergence is live in configuration, not just source defaults.

PASS/FAIL/PARTIAL SUMMARY
-------------------------
Item 1 one code path / one config:        PARTIAL (one config yes; sizing code path diverges)
Item 2 PAPER_PER_TRADE_PCT / dry-only sizing:  FAIL
Item 3 config hash log + live refuse:     FAIL (gap; Poly logs hash only, no refuse)
Item 4 dry vs live config defaults:       PARTIAL (shared defaults; sizing/risk diverge conditionally)
Item 5 focused config/gate tests:         PASS (123 passed, exit 0)

Overall: T17-06 is not green. Items 2 and 3 are concrete gaps; Items 1 and 4
are PARTIAL pending the same fix. No source/config/test edits were made in
this session; the minimal fix and its TDD test are documented above for the
reviewing agent.