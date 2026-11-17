"""Tests for N4 — n8n as a safe control plane.

Acceptance (AGENT_TASKS_v7.md N4):
- a valid signed pause_entries stops new entries within one fast-loop tick and emits an ack;
- an unsigned, expired, replayed or unknown command is rejected and logged;
- reduce_exposure_caps cannot raise a cap;
- resume_entries after a risk halt is refused without confirmation.
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import config
import control
import emit
import n8n_webhook_auth
import trader
from local_db import LocalDB

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def control_db(tmp_path):
    return LocalDB(db_path=str(tmp_path / "n4_control.db"))


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "n4-test-secret",
                        raising=False)


@pytest.fixture(autouse=True)
def _reset_control_state():
    control.STATE.pause_entries = False
    yield
    control.STATE.pause_entries = False


def _signed_row(db, command="pause_entries", agent_id="operator",
                payload=None, nonce=None, ts_offset_ms=0,
                secret="n4-test-secret"):
    ts = int(time.time() * 1000) + ts_offset_ms
    if nonce is None:
        import uuid
        nonce = uuid.uuid4().hex
    body = json.dumps({"command": command, **(payload or {})},
                      separators=(",", ":"), ensure_ascii=False)
    sig = n8n_webhook_auth.sign_body(ts, nonce, body)
    row_id = db.enqueue_control_command(command, agent_id, nonce, ts, body, sig)
    return row_id, ts, nonce, body


def _pending(db):
    return db.get_pending_control_commands()


# ── valid pause_entries stops entries within one tick ──────────────────

def test_valid_pause_entries_stops_new_entries_within_one_tick(
        control_db, monkeypatch):
    from test_trader import _mkts, _reset_cycle_state

    _signed_row(control_db, "pause_entries", agent_id="operator")
    monkeypatch.setattr(trader, "get_local_db", lambda: control_db)
    monkeypatch.setattr(trader, "find_active_markets", lambda c: _mkts())
    monkeypatch.setattr(trader, "get_n8n_client", lambda: MagicMock())
    monkeypatch.setattr(config, "LLM_EVALUATION_ENABLED", False)
    monkeypatch.setattr(trader.se, "evaluate",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("signal evaluated despite pause_entries")))
    _reset_cycle_state(monkeypatch)

    client = MagicMock()
    client.get_balance.return_value = 100.0
    client.get_positions.return_value = []
    client.get_orders.return_value = []
    client.invalidate_markets.return_value = None
    client.get_market.return_value = {"expiration_value": 1.0}

    trader.cycle(client)  # must return at the N4 pause gate

    assert control.STATE.pause_entries is True
    assert client.place_order.call_count == 0
    # the drain applied the command and acknowledged it in the inbox
    rows = control_db._get_conn().execute(
        "SELECT status FROM control_inbox").fetchall()
    assert [r[0] for r in rows] == ["applied"]


def test_pause_entries_ack_event_emitted(control_db, monkeypatch):
    captured = []
    monkeypatch.setattr(emit, "emit_event",
                        lambda source, event_type, payload, **kw:
                        captured.append((source, event_type, payload)))
    _signed_row(control_db, "pause_entries", agent_id="operator",
                payload={"reason": "volatility spike"})
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results and results[0]["status"] == "applied"
    applied = [c for c in captured if c[1] == "control.applied"]
    assert applied and applied[0][2]["reason"] == "volatility spike"


# ── unsigned / expired / replayed / unknown → rejected and logged ──────

def test_unsigned_command_rejected_and_logged(control_db, monkeypatch, caplog):
    captured = []
    monkeypatch.setattr(emit, "emit_event",
                        lambda source, event_type, payload, **kw:
                        captured.append((event_type, payload)))
    ts = int(time.time() * 1000)
    control_db.enqueue_control_command(
        "pause_entries", "attacker", f"nonce{ts}", ts,
        '{"command": "pause_entries"}', "")  # no signature material
    with caplog.at_level("WARNING"):
        results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "rejected"
    assert "rejected" in caplog.text.lower()
    assert any(t == "control.rejected" for t, _ in captured)
    assert control_db._get_conn().execute(
        "SELECT status FROM control_inbox").fetchone()[0] == "rejected"
    assert control.STATE.pause_entries is False


def test_expired_command_rejected(control_db, caplog):
    _signed_row(control_db, "pause_entries", ts_offset_ms=-120_000)  # 2 min old
    with caplog.at_level("WARNING"):
        results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "rejected"
    assert "timestamp" in results[0]["reason"].lower()
    assert control.STATE.pause_entries is False


def test_replayed_command_rejected_at_enqueue(control_db):
    row_id, ts, nonce, body = _signed_row(control_db, "pause_entries")
    assert row_id is not None
    first_id = row_id
    # exact replay: same command bytes, same nonce
    dup = control_db.enqueue_control_command(
        "pause_entries", "agent-a", nonce, ts, body,
        n8n_webhook_auth.sign_body(ts, nonce, body))
    assert dup is None
    # and it is applied exactly once
    control.drain_and_apply(control_db, risk_halted=False)
    control.drain_and_apply(control_db, risk_halted=False)
    assert control_db._get_conn().execute(
        "SELECT COUNT(*) FROM control_inbox").fetchone()[0] == 1


def test_unknown_command_rejected_and_logged(control_db, caplog):
    _signed_row(control_db, "delete_everything", agent_id="operator")
    with caplog.at_level("WARNING"):
        results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "rejected"
    assert "unknown command" in results[0]["reason"]
    assert "rejected" in caplog.text.lower()


# ── reduce_exposure_caps can only lower caps ───────────────────────────

def test_reduce_exposure_caps_cannot_raise(control_db, monkeypatch):
    monkeypatch.setattr(config, "PER_TRADE_PCT", 0.08)
    monkeypatch.setattr(config, "EXPOSURE_CEILING_PCT", 0.30)

    _signed_row(control_db, "reduce_exposure_caps",
                payload={"per_trade_pct": 0.05})          # lower: allowed
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "applied"
    assert config.PER_TRADE_PCT == 0.05

    _signed_row(control_db, "reduce_exposure_caps",
                payload={"per_trade_pct": 0.09})          # raise: rejected
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "rejected"
    assert "only lower" in results[0]["reason"]
    assert config.PER_TRADE_PCT == 0.05

    _signed_row(control_db, "reduce_exposure_caps",
                payload={"exposure_ceiling_pct": 0.50})   # raise: rejected
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "rejected"
    assert config.EXPOSURE_CEILING_PCT == 0.30

    _signed_row(control_db, "reduce_exposure_caps",
                payload={"per_trade_pct": 2.0})           # out of bounds
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "rejected"


# ── resume_entries after a risk halt requires confirmation ─────────────

def test_resume_after_risk_halt_refused_without_confirmation(control_db):
    control.STATE.pause_entries = True

    _signed_row(control_db, "resume_entries", agent_id="operator")
    results = control.drain_and_apply(control_db, risk_halted=True)
    assert results[0]["status"] == "rejected"
    assert "confirm" in results[0]["reason"].lower()
    assert control.STATE.pause_entries is True

    # confirmation from the *same* identity does not count
    _signed_row(control_db, "resume_entries", agent_id="operator",
                payload={"confirm_by": "operator"})
    results = control.drain_and_apply(control_db, risk_halted=True)
    assert results[0]["status"] == "rejected"

    # a different identity confirms: applied (risk halt itself stays;
    # that needs HALT_AUTO_RESUME or a VPS-side --resume)
    _signed_row(control_db, "resume_entries", agent_id="operator",
                payload={"confirm_by": "agent-b"})
    results = control.drain_and_apply(control_db, risk_halted=True)
    assert results[0]["status"] == "applied"
    assert control.STATE.pause_entries is False


def test_resume_without_risk_halt_applies(control_db):
    control.STATE.pause_entries = True
    _signed_row(control_db, "resume_entries", agent_id="operator")
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "applied"
    assert control.STATE.pause_entries is False


# ── request_status / request_report acks ───────────────────────────────

def test_request_status_and_report_emit_events(control_db, monkeypatch):
    captured = []
    monkeypatch.setattr(emit, "emit_event",
                        lambda source, event_type, payload, **kw:
                        captured.append((source, event_type, payload)))
    _signed_row(control_db, "request_status", agent_id="operator")
    _signed_row(control_db, "request_report", agent_id="operator")
    results = control.drain_and_apply(control_db, risk_halted=False,
                                      positions={"T": {"contracts": 1}})
    assert [r["status"] for r in results] == ["applied", "applied"]
    kinds = [c[1] for c in captured]
    assert "control.status" in kinds and "control.report" in kinds
    status = next(c for c in captured if c[1] == "control.status")[2]
    assert status["positions_open"] == 1


# ── send_command.py stdin path (n8n side) ──────────────────────────────

def test_send_command_stdin_roundtrip(monkeypatch, tmp_path, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "send_command", REPO / "scripts" / "send_command.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db = LocalDB(db_path=str(tmp_path / "stdin.db"))
    monkeypatch.setattr(mod, "get_local_db", lambda: db)

    ts = int(time.time() * 1000)
    nonce = "a" * 32
    body = json.dumps({"command": "pause_entries", "agent_id": "n8n",
                       "reason": "manual"},
                      separators=(",", ":"), ensure_ascii=False)
    sig = n8n_webhook_auth.sign_body(ts, nonce, body)
    verify_out = json.dumps({
        "body": json.loads(body),
        "headers": {"x-superbot-timestamp": str(ts),
                    "x-superbot-nonce": nonce,
                    "x-superbot-signature": sig}})
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(verify_out))
    assert mod.main(["--stdin"]) == 0
    rows = db.get_pending_control_commands()
    assert len(rows) == 1 and rows[0]["command"] == "pause_entries"

    # replay through the same path: enqueued once (UNIQUE nonce)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(verify_out))
    assert mod.main(["--stdin"]) == 2
    assert len(db.get_pending_control_commands()) == 1


def test_send_command_stdin_unsigned_refused(monkeypatch, tmp_path, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "send_command", REPO / "scripts" / "send_command.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db = LocalDB(db_path=str(tmp_path / "stdin2.db"))
    monkeypatch.setattr(mod, "get_local_db", lambda: db)
    verify_out = json.dumps({
        "body": {"command": "pause_entries", "agent_id": "n8n"},
        "headers": {"x-superbot-timestamp": str(int(time.time() * 1000)),
                    "x-superbot-nonce": "b" * 32}})
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(verify_out))
    assert mod.main(["--stdin"]) == 2
    assert db.get_pending_control_commands() == []


def test_drain_empty_inbox_is_noop(control_db):
    assert control.drain_and_apply(control_db, risk_halted=False) == []


def test_control_workflow_verify_code_matches_installer_js():
    """superbot-control's verify node must stay in sync with VERIFY_JS."""
    import inject_n8n_workflows as installer

    wf = json.loads((REPO / "n8n" / "workflows" / "superbot-control.json").read_text())
    verify = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.code"
                  and "x-superbot-signature" in n["parameters"].get("jsCode", ""))
    assert verify["parameters"]["jsCode"].strip() == installer.VERIFY_JS.strip()


def test_send_command_operator_path(tmp_path, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "send_command", REPO / "scripts" / "send_command.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db = LocalDB(db_path=str(tmp_path / "operator.db"))
    monkeypatch.setattr(mod, "get_local_db", lambda: db)
    assert mod.main(["--command", "reduce_exposure_caps",
                     "--by", "ubuntu:opencode:kalshi_deepbot",
                     "--payload-json", '{"per_trade_pct": 0.05}']) == 0
    rows = db.get_pending_control_commands()
    assert len(rows) == 1 and rows[0]["command"] == "reduce_exposure_caps"
    out = json.loads(capsys.readouterr().out)
    assert out["accepted"] is True
