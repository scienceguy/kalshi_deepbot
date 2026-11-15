"""Tests for scripts/poly_ledger_audit.py — the settlement-tracking audit."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import poly_ledger_audit as audit_mod  # noqa: E402


def _write(tmp_path, rows, state):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))
    return state_file, ledger


def test_audit_counts_settled_rotated_held_and_unsettled(tmp_path):
    rows = [
        {"event": "open", "slug": "a", "side": "no", "cost": 0.95, "timestamp": "2026-11-14T10:00:00Z"},
        {"event": "settlement", "slug": "a", "side": "no", "pnl": 0.05, "timestamp": "2026-11-14T11:00:00Z"},
        {"event": "open", "slug": "b", "side": "no", "cost": 0.94, "timestamp": "2026-11-14T10:00:00Z"},
        {"event": "rotate_exit", "slug": "b", "side": "no", "pnl": -0.94, "timestamp": "2026-11-14T12:00:00Z"},
        {"event": "open", "slug": "c", "side": "yes", "cost": 0.96, "timestamp": "2026-11-14T10:00:00Z"},
    ]
    state = {"cash": 34.0, "positions": [{"slug": "c", "cost": 0.96}],
             "realized_pnl": -0.89, "start_balance": 35.0}
    state_file, ledger = _write(tmp_path, rows, state)

    rep = audit_mod.audit(state_file, ledger)

    assert rep["opens"] == 3
    assert rep["settlements"] == 1
    assert rep["rotated"] == 1
    assert rep["held"] == 1
    assert rep["unsettled_opens"] == 0
    assert rep["ledger_realized"] == -0.89
    assert rep["state_matches_ledger"] is True


def test_audit_flags_positions_that_never_reached_an_outcome(tmp_path):
    rows = [
        {"event": "open", "slug": "a", "side": "no", "cost": 0.95, "timestamp": "2026-11-14T10:00:00Z"},
        {"event": "open", "slug": "a", "side": "no", "cost": 0.95, "timestamp": "2026-11-14T10:00:20Z"},
    ]
    state = {"cash": 33.0, "positions": [], "realized_pnl": 0.0, "start_balance": 35.0}
    state_file, ledger = _write(tmp_path, rows, state)

    rep = audit_mod.audit(state_file, ledger)

    assert rep["unsettled_opens"] == 2
    assert len(rep["unsettled_examples"]) == 2


def test_audit_reports_state_ledger_mismatch(tmp_path):
    rows = [{"event": "settlement", "slug": "a", "pnl": -5.21, "timestamp": "2026-11-14T11:00:00Z"}]
    state = {"cash": 32.11, "positions": [], "realized_pnl": 0.0, "start_balance": 35.0}
    state_file, ledger = _write(tmp_path, rows, state)

    rep = audit_mod.audit(state_file, ledger)

    assert rep["ledger_realized"] == -5.21
    assert rep["state_matches_ledger"] is False
