# T17-03/T17-04 configuration verification

Date: 2026-11-23
HEAD: f46d6ccac9e03705c6befa7d2f6bbb658a636b08
Scope: read-only verification at the current checkout. Only focused tests were
run. This reviewer did not edit source, config, or tests; no full suite, orders,
live mode, balance or secret reads, process changes, or git mutations were
performed. Runtime/test files were redirected outside the project tree. This
report is the only project-tree file created by this reviewer.

Concurrent-worktree note: config.py, tests/test_config.py, and .env.example were
modified by another agent during this review. The final verification below was
re-run against that final worktree, not just clean HEAD. The final config.py
SHA256 was bb8f95ebc12d2f753ca8aa3381e5a15eead2bfa1520264bd20af656b36774854;
the final tests/test_config.py SHA256 was
2e010d8d69aac196b352a45ffcfd9fa50fc74ac7fb0a48e3051599bd5dbe6d47.
Those concurrent edits were not made or included in any git mutation by this
reviewer.

## Verdicts

| Item | Verdict | Evidence |
|---|---|---|
| NO_ONLY default and tests | PASS | Default is True at config.py:592; entry and hold-to-expiry paths are gated at signals.py:63-77 and trader.py:1654-1658; four focused strategy tests passed. |
| ALLOW_NO_BUYS default and gating | PASS | The final worktree defaults unset NO buys off in both modes, keeps explicit configuration authoritative, and gates both NO evaluation paths; the focused default test passed. Direct signal-gate behavior coverage remains a gap. |
| NO-side floor removed with four-corner tests | PASS | gates.py:642-644 applies the measured floor only when side == "yes"; all four corners and the related side tests passed. |
| GATES_SIGMA_FROM_ATR_V2 default false | PASS | config.py:389 defaults False; V1/V2 selection is explicit at gates.py:259-271 and gates.py:582-587; three focused sigma tests passed. |
| tests/test_config_consistency.py current result | PASS | 5 passed in 0.35s, exit 0. |
| T17-03 overall | PARTIAL | The safety fixes verify on the final worktree, but stale comments, direct ALLOW_NO_BUYS signal-gate test coverage, and open per-asset sigma promotion evidence remain. |
| T17-04 overall | PARTIAL | The consistency file is green, but exactly one definition, complete pin-risk/sizing coverage, runtime agreement, and full README agreement are not established. |

## Source and test evidence

### 1. NO_ONLY: PASS

- config.py:588-592 defines the standing NO-only strategy and uses
  `NO_ONLY = _env_bool("NO_ONLY", True)`.
- signals.py:63-77 makes a YES signal untradeable when NO_ONLY is true.
- trader.py:1654-1658 returns before exit processing when NO_ONLY is true, so
  positions are not sold or stopped out by that path.
- tests/test_signals.py:102-115 covers YES rejected, NO accepted, and YES
  accepted only when the flag is explicitly off.
- tests/test_exit.py:90-98 covers hold-to-expiry with no sell call.
- The focused run passed all three TestNoOnlyStrategy tests and
  test_manage_exits_holds_to_expiry_under_no_only.

### 2. ALLOW_NO_BUYS: PASS

Implementation evidence on the final worktree:

- config.py:94-101 gives an explicit setting precedence and otherwise returns
  False regardless of NO_ONLY or DRY_RUN. This matches AGENT_TASKSv17.md:39-43,
  which keeps NO buys off until T17-03 verification and approval.
- config.py:817-819 applies that helper after DRY_RUN is resolved.
- .env.example:9-13 also says unset NO buys stay off in dry mode and require
  recorded approval before explicit enablement.
- signals.py:592-595 returns PASS for a bearish/NO evaluation when NO buys are
  disabled. signals.py:818-824 only adds a NO taker candidate when the flag is
  enabled. market_finder.py:427-436 applies the same flag to NO tradeability.
- tests/test_config.py:246-258 checks that the unset default is False for dry
  NO-only, live NO-only, and dry with NO_ONLY off, plus explicit false override.
  The renamed test test_allow_no_buys_defaults_off_until_approved passed.

Remaining coverage gap: the executed test covers `_default_allow_no_buys`; it
does not directly exercise enabled/disabled behavior at signals.py:594 and
signals.py:819. Those two gates are verified by source inspection here.
conftest.py:35 also pins ALLOW_NO_BUYS=false for the test session. No approval
or enablement was performed by this reviewer.

### 3. NO-side measured floor and four corners: PASS

- gates.py:626-645 keys band membership to own-side entry price. The only
  assignment that can set `q_measured_floor` is guarded by
  `q_floor is not None and side == "yes"` at gates.py:642-644. NO therefore
  keeps model q; no YES-derived complement is applied.
- tests/test_gates.py:790-876 covers YES 97c, YES 3c, NO 97c, and NO 3c. The NO
  97c assertions require no measured-floor flag and no NO-complement source;
  the NO 3c assertions require no floor.
- tests/test_gates.py:433-501 independently checks NO favorite, NO longshot,
  and YES longshot behavior, including exact model q preservation for NO 97c.
- All four TestMeasuredFloorFourCorners tests and all six focused
  TestMeasuredFloorSide tests passed (the latter class also includes one
  pin-risk test).

Non-functional documentation debt remains: gates.py:627-632 and
tests/test_gates.py:373-386 still describe a NO complement as if it existed,
and tests/test_gates.py:790-796 describes the old yes-equivalent implementation
as current. The assertions and executable code implement the safe behavior.

### 4. GATES_SIGMA_FROM_ATR_V2 default: PASS

- config.py:379-389 documents why global V2 promotion is pending and sets the
  default to False.
- gates.py:259-271 returns V2 only when the flag is true, otherwise the legacy
  V1 formula. gates.py:582-587 makes the same selection on the gate ATR path
  and records sigma_source.
- tests/test_gates.py:348-363 asserts both the false default and actual V1
  behavior, then checks explicit V2 opt-in.
- tests/test_gates_sigma_v2.py:76-86 checks the off formula and explicit V2;
  tests/test_gates_sigma_v2.py:115-118 checks the gate uses atr_v1 when off.
- The three selected sigma tests passed.

This is not new real-data calibration evidence. docs/T16_GATE_REVIEW.md:99-120
records Brier before/after results, including the BTC evaluation regression and
unmeasured assets, and recommends keeping the global default off. No promotion
or live-readiness claim is made by this verification.

### 5. Config consistency and T17-04: test PASS, task PARTIAL

- tests/test_config_consistency.py:45-80 checks default band agreement, default
  entry window 6.0-11.0, window ordering, environment override precedence, and
  a broad band/window reachability tripwire. Current result: 5 passed.
- config.py:293-297 supplies the tested default window and YES band;
  config.py:581-584 supplies the same default ENTRY_BAND and NO limits.
- README.md:7 agrees with the 95-99c default band.

Remaining T17-04 gaps:

1. Exactly one config definition is not present: MIN_YES_PRICE_CENTS and
   MAX_YES_PRICE_CENTS are independent environment-backed definitions at
   config.py:296-297, while ENTRY_BAND_MIN and ENTRY_BAND_MAX are separate
   environment-backed definitions at config.py:581-582.
2. tests/test_config_consistency.py:28-49 clears all those keys before checking
   equality. It proves default equality, not that conflicting environment
   overrides are rejected or cannot diverge.
3. The file contains no GATES_PIN_RISK_MINUTES or sizing assertion despite its
   title/docstring claiming band, window, pin-risk, and sizing consistency.
   The pin-risk definition remains separately configurable at config.py:368.
4. tests/test_config_consistency.py:12-15 explicitly records that the operator
   .env overrides the entry window to 8.0-14.5. Thus the green defaults test is
   not evidence that the effective runtime window matches the tested defaults;
   no .env or secret file was read in this verification.
5. README.md:7 documents the band but not the entry window, pin-risk minutes,
   or sizing definition. Full README agreement is therefore not established.

## Exact commands and observed output

All commands ran from /opt/superbot. PYTHONDONTWRITEBYTECODE and -B prevented
Python bytecode writes; no:cacheprovider disabled pytest cache writes.

### Focused strategy/config/gate tests

```bash
set -o pipefail
PYTHONDONTWRITEBYTECODE=1 SUPERBOT_DATA_DIR=/tmp/opencode/t17-config-verification-final TMPDIR=/tmp/opencode .venv/bin/python -B -m pytest -q -p no:cacheprovider \
  tests/test_signals.py::TestNoOnlyStrategy \
  tests/test_exit.py::test_manage_exits_holds_to_expiry_under_no_only \
  tests/test_config.py::test_allow_no_buys_defaults_off_until_approved \
  tests/test_gates.py::TestGatesMath::test_sigma_from_atr_v2_is_opt_in_pending_per_asset_validation \
  tests/test_gates.py::TestMeasuredFloorSide \
  tests/test_gates.py::TestMeasuredFloorFourCorners \
  tests/test_gates_sigma_v2.py::test_flag_off_path_is_byte_identical \
  tests/test_gates_sigma_v2.py::test_flag_off_uses_atr_v1
```

Observed output, including the wrapper's explicit status line:

```text
..................                                                       [100%]
18 passed in 0.77s
EXIT_STATUS=0
```

### Current config consistency result

```bash
set -o pipefail
PYTHONDONTWRITEBYTECODE=1 SUPERBOT_DATA_DIR=/tmp/opencode/t17-config-consistency-final TMPDIR=/tmp/opencode .venv/bin/python -B -m pytest -q -p no:cacheprovider tests/test_config_consistency.py
```

Observed output:

```text
.....                                                                    [100%]
5 passed in 0.35s
EXIT_STATUS=0
```

### Source verification gate

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/verify_source.py --all
```

The command emitted 290 individual `<path>: clean` lines and ended with the
following exact lines; verifier exit status was 0:

```text
all clean
EXIT_STATUS=0
```

### ASCII check for this report

```bash
LC_ALL=C grep -n '[^ -~]' docs/T17_CONFIG_VERIFICATION.md
```

Expected result for a clean ASCII report is no matching lines and grep exit 1.
The final observed status is recorded below after the report was written:

```text
GREP_EXIT_STATUS=1
```

## Final tree scope

The relevant tracked files had no worktree diff at the initial check. Another
agent subsequently modified config.py, tests/test_config.py, and .env.example;
the final tests and source gate were re-run after those edits. HEAD remained
f46d6ccac9e03705c6befa7d2f6bbb658a636b08. This reviewer did not alter or revert
those changes, and no other project-tree file was created by this reviewer. No
full-suite or live-readiness claim is made.
