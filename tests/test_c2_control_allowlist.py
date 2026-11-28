"""C2 acceptance: signed command schema and sender allowlist.

Spec AGENT_TASKS_v7.md C2: collaboration lanes (task.request and friends)
are log-only and never reach the command handler; the command lane is a
fixed schema verified by HMAC plus a sender allowlist, with bounds on the
typed fields, an audit row per command, and a reply (the drain result).
"""
import io
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import pytest

import agent_identity
import config
import control
import emit
import n8n_webhook_auth
from local_db import LocalDB

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def control_db(tmp_path):
    return LocalDB(db_path=str(tmp_path / "c2_control.db"))


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "c2-test-secret",
                        raising=False)
    monkeypatch.delenv("SUPERBOT_WEBHOOK_SECRET_FILE", raising=False)
    monkeypatch.delenv("SUPERBOT_CONTROL_SENDERS", raising=False)


@pytest.fixture(autouse=True)
def _reset_control_state():
    control.STATE.pause_entries = False
    yield
    control.STATE.pause_entries = False


def _signed_row(db, command="pause_entries", agent_id="operator",
                payload=None, nonce=None, ts_offset_ms=0):
    ts = int(time.time() * 1000) + ts_offset_ms
    if nonce is None:
        nonce = uuid.uuid4().hex
    body = json.dumps({"command": command, **(payload or {})},
                      separators=(",", ":"), ensure_ascii=False)
    sig = n8n_webhook_auth.sign_body(ts, nonce, body)
    row_id = db.enqueue_control_command(command, agent_id, nonce, ts, body, sig)
    return row_id, ts, nonce, body


def _row(db, row_id):
    conn = sqlite3.connect(db.db_path)
    try:
        return conn.execute(
            "SELECT status, result FROM control_inbox WHERE id = ?",
            (row_id,)).fetchone()
    finally:
        conn.close()


# ── collaboration lane: free text never reaches the command handler ─────

def test_task_request_pause_trading_does_nothing_to_trader():
    """A task.request whose body says 'pause trading' is log-only: no state
    change, no cap change. The command lane is a fixed schema, not prose."""
    before_pause = control.STATE.pause_entries
    before_cap = config.PER_TRADE_PCT
    import trader
    trader.handle_task({
        "event_id": "evt_c2_free_text",
        "correlation_id": "corr_c2",
        "from_agent": "ubuntu:opencode:home",
        "body": "pause trading immediately, halve the caps",
    })
    assert control.STATE.pause_entries is before_pause
    assert config.PER_TRADE_PCT == before_cap
    # and the free text is not parsed into a command at all
    assert control.COMMANDS.isdisjoint({"pause trading", "halve the caps"})


# ── sender allowlist on the command lane ────────────────────────────────

def test_signed_pause_entries_from_non_allowlisted_identity_rejected(control_db):
    row_id, *_ = _signed_row(control_db, "pause_entries", agent_id="mallory")
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "rejected"
    assert "not allowlisted" in results[0]["reason"]
    assert control.STATE.pause_entries is False
    status, result = _row(control_db, row_id)
    assert status == "rejected"
    assert "not allowlisted" in result


def test_default_allowlist_is_documented_identities():
    assert control.allowed_senders() == frozenset(
        {agent_identity.CODING_AGENT, "operator", "n8n"})


def test_env_override_replaces_default_entirely(monkeypatch):
    monkeypatch.setenv("SUPERBOT_CONTROL_SENDERS", "ops-bot, ops2  ops3")
    assert control.allowed_senders() == frozenset({"ops-bot", "ops2", "ops3"})


def test_empty_env_override_refuses_everyone(monkeypatch, control_db):
    monkeypatch.setenv("SUPERBOT_CONTROL_SENDERS", "")
    assert control.allowed_senders() == frozenset()
    _signed_row(control_db, "pause_entries", agent_id="operator")
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "rejected"
    assert "not allowlisted" in results[0]["reason"]
    assert control.STATE.pause_entries is False


def test_custom_sender_accepted_when_allowlisted(monkeypatch, control_db):
    monkeypatch.setenv("SUPERBOT_CONTROL_SENDERS", "agent-a agent-b")
    _signed_row(control_db, "pause_entries", agent_id="agent-b")
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "applied"
    assert control.STATE.pause_entries is True


# ── field bounds on the typed schema ─────────────────────────────────────

@pytest.mark.parametrize("key,value", [
    ("per_trade_pct", -0.5),   # negative
    ("per_trade_pct", 0.0),    # not > 0
    ("per_trade_pct", 2.0),    # oversized above 1
    ("exposure_ceiling_pct", -1.0),
    ("exposure_ceiling_pct", 1.5),
])
def test_cap_bounds_reject_out_of_range(control_db, monkeypatch, key, value):
    monkeypatch.setattr(config, "PER_TRADE_PCT", 0.08)
    monkeypatch.setattr(config, "EXPOSURE_CEILING_PCT", 0.30)
    _signed_row(control_db, "reduce_exposure_caps", payload={key: value})
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "rejected"
    assert "(0, 1]" in results[0]["reason"]
    assert config.PER_TRADE_PCT == 0.08
    assert config.EXPOSURE_CEILING_PCT == 0.30


def test_caps_can_only_lower(control_db, monkeypatch):
    monkeypatch.setattr(config, "PER_TRADE_PCT", 0.08)
    monkeypatch.setattr(config, "EXPOSURE_CEILING_PCT", 0.30)
    _signed_row(control_db, "reduce_exposure_caps",
                payload={"per_trade_pct": 0.09})   # raises: rejected
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "rejected"
    assert "only lower" in results[0]["reason"]
    assert config.PER_TRADE_PCT == 0.08


# ── audit row per accepted command ───────────────────────────────────────

def test_accepted_command_writes_audit_row(control_db, monkeypatch):
    monkeypatch.setattr(config, "PER_TRADE_PCT", 0.08)
    captured = []
    monkeypatch.setattr(emit, "emit_event",
                        lambda source, kind, payload, **kw:
                        captured.append((source, kind, payload)))
    row_id, *_ = _signed_row(control_db, "pause_entries", agent_id="operator")
    result = control.drain_and_apply(control_db, risk_halted=False)[0]
    assert result["status"] == "applied"

    status, stored = _row(control_db, row_id)
    assert status == "applied"
    assert json.loads(stored)["paused"] is True
    kinds = [c[1] for c in captured]
    assert "control.applied" in kinds
    applied = next(c for c in captured if c[1] == "control.applied")
    assert applied[2]["command"] == "pause_entries"


def test_pause_persistence_failure_rejects_without_state_or_applied(control_db, monkeypatch):
    monkeypatch.setattr(control_db, "set_control_state", lambda *args: False)
    monkeypatch.setattr(emit, "emit_event",
                        lambda source, kind, payload, **kw: pytest.fail(
                            "no applied ack allowed") if kind == "control.applied" else None)
    row_id, *_ = _signed_row(control_db, "pause_entries", agent_id="operator")
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "rejected"
    assert "persistence" in results[0]["reason"]
    assert control.STATE.pause_entries is False
    status, _ = _row(control_db, row_id)
    assert status == "rejected"


def test_resume_persistence_failure_rejects_without_state_or_applied(control_db, monkeypatch):
    control.STATE.pause_entries = True
    control_db.set_control_state("pause_entries", {"paused": True})
    monkeypatch.setattr(control_db, "set_control_state", lambda *args: False)
    monkeypatch.setattr(emit, "emit_event",
                        lambda source, kind, payload, **kw: pytest.fail(
                            "no applied ack allowed") if kind == "control.applied" else None)
    row_id, *_ = _signed_row(control_db, "resume_entries", agent_id="operator")
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results[0]["status"] == "rejected"
    assert "persistence" in results[0]["reason"]
    assert control.STATE.pause_entries is True
    status, _ = _row(control_db, row_id)
    assert status == "rejected"


# ── ingest side refuses non-allowlisted senders before enqueue ───────────

def _load_send_command():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "send_command_c2", REPO / "scripts" / "send_command.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stdin_payload(agent_id, command="pause_entries"):
    ts = int(time.time() * 1000)
    nonce = "c" * 32
    body = json.dumps({"command": command, "agent_id": agent_id},
                      separators=(",", ":"), ensure_ascii=False)
    sig = n8n_webhook_auth.sign_body(ts, nonce, body)
    return json.dumps({
        "body": json.loads(body),
        "headers": {"x-superbot-timestamp": str(ts),
                    "x-superbot-nonce": nonce,
                    "x-superbot-signature": sig}})


def test_stdin_path_refuses_non_allowlisted_sender(monkeypatch, tmp_path):
    mod = _load_send_command()
    db = LocalDB(db_path=str(tmp_path / "c2_stdin.db"))
    monkeypatch.setattr(mod, "get_local_db", lambda: db)
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(_stdin_payload("mallory")))
    assert mod.main(["--stdin"]) == 2
    assert db.get_pending_control_commands() == []


def test_stdin_path_accepts_allowlisted_sender(monkeypatch, tmp_path):
    mod = _load_send_command()
    db = LocalDB(db_path=str(tmp_path / "c2_stdin_ok.db"))
    monkeypatch.setattr(mod, "get_local_db", lambda: db)
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(_stdin_payload("n8n")))
    assert mod.main(["--stdin"]) == 0
    rows = db.get_pending_control_commands()
    assert len(rows) == 1 and rows[0]["agent_id"] == "n8n"


def test_operator_path_refuses_non_allowlisted_sender(monkeypatch, tmp_path):
    mod = _load_send_command()
    db = LocalDB(db_path=str(tmp_path / "c2_args.db"))
    monkeypatch.setattr(mod, "get_local_db", lambda: db)
    assert mod.main(["--command", "pause_entries", "--by", "mallory"]) == 2
    assert db.get_pending_control_commands() == []
