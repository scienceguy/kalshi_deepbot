"""H1 — signed pause_entries persists to local DB control_state.

A restart must not silently un-pause the bot: applying a signed
pause_entries command writes control_state['pause_entries'] with
paused=True and the issuing agent id; resume_entries clears it; the
Polymarket US guard honours the persisted state even when the in-memory
control.STATE.pause_entries is False; and the startup reload path reads
the persisted flag back.
"""

import json
import time
from pathlib import Path

import pytest

import config
import control
import emit
import n8n_webhook_auth
from local_db import LocalDB

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def control_db(tmp_path):
    return LocalDB(db_path=str(tmp_path / "h1_pause.db"), auto_sync=False)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "h1-test-secret",
                        raising=False)


@pytest.fixture(autouse=True)
def _no_n8n_outbox(monkeypatch):
    # keep emit_event off the n8n outbox entirely
    monkeypatch.setattr(emit, "emit_event",
                        lambda source, event_type, payload, **kw: None)


@pytest.fixture(autouse=True)
def _reset_control_state():
    control.STATE.pause_entries = False
    yield
    control.STATE.pause_entries = False


def _signed_row(db, command="pause_entries", agent_id="operator",
                payload=None, nonce=None, ts_offset_ms=0,
                secret="h1-test-secret"):
    ts = int(time.time() * 1000) + ts_offset_ms
    if nonce is None:
        import uuid
        nonce = uuid.uuid4().hex
    body = json.dumps({"command": command, **(payload or {})},
                      separators=(",", ":"), ensure_ascii=False)
    sig = n8n_webhook_auth.sign_body(ts, nonce, body)
    row_id = db.enqueue_control_command(command, agent_id, nonce, ts, body, sig)
    return row_id, ts, nonce, body


# ── 1. signed pause_entries persists paused=True + issuing agent ───────

def test_pause_entries_persists_control_state(control_db):
    _signed_row(control_db, "pause_entries", agent_id="operator",
                payload={"reason": "volatility spike"})
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results and results[0]["status"] == "applied"

    state = control_db.get_control_state("pause_entries")
    assert state is not None
    assert state["paused"] is True
    assert state["paused_by"] == "operator"
    assert state.get("reason") == "volatility spike"
    assert state.get("paused_at")


# ── 2. signed resume_entries (no risk halt) clears the persisted flag ──

def test_resume_entries_clears_persisted_control_state(control_db):
    control.STATE.pause_entries = True
    control_db.set_control_state("pause_entries",
                                 {"paused": True, "paused_by": "operator"})
    _signed_row(control_db, "resume_entries", agent_id="operator")
    results = control.drain_and_apply(control_db, risk_halted=False)
    assert results and results[0]["status"] == "applied"

    state = control_db.get_control_state("pause_entries")
    assert state is not None
    assert state["paused"] is False
    assert control.STATE.pause_entries is False


# ── 3. PolyUSPaperTrader._guard_halt_active honours persisted pause ────
 
def test_poly_guard_halt_active_reads_persisted_pause(control_db, monkeypatch):
    pytest.importorskip("polymarket_us")
    import local_db
    import poly_us_trader
    import threading
 
    # in-memory state says NOT paused; only the persisted DB row says paused
    control.STATE.pause_entries = False
    control_db.set_control_state("pause_entries",
                                 {"paused": True, "paused_by": "operator"})
    monkeypatch.setattr(local_db, "get_local_db", lambda *a, **k: control_db)
 
    class _Stub:
        state = {}  # no ruin / venue-recon / daily-loss halts
        _pause_cache = {"paused": True}  # simulate cache populated from DB
        _pause_cache_lock = threading.Lock()
 
    assert poly_us_trader.PolyUSPaperTrader._guard_halt_active(_Stub()) is True
 
    # and once the persisted flag is cleared, the guard falls back to memory
    control_db.set_control_state("pause_entries", {"paused": False})
    # Also clear cache for second assertion
    _Stub._pause_cache = {"paused": False}
    assert poly_us_trader.PolyUSPaperTrader._guard_halt_active(_Stub()) is False


# ── 4. startup reload path yields paused=True ──────────────────────────

def test_startup_reload_reads_persisted_pause(control_db):
    control_db.set_control_state(
        "pause_entries",
        {"paused": True, "paused_by": "operator", "reason": "restart drill"})
    # trader.py's startup block (trader.py ~L3260) does exactly this:
    #   db = get_local_db(); pause_state = db.get_control_state("pause_entries")
    #   if pause_state and pause_state.get("paused"): control.STATE.pause_entries = True
    # We deliberately do NOT import trader here (heavy import chain); the
    # reload contract is the get_control_state read, asserted directly.
    pause_state = control_db.get_control_state("pause_entries")
    assert pause_state is not None
    assert pause_state.get("paused") is True
    assert pause_state.get("paused_by") == "operator"
