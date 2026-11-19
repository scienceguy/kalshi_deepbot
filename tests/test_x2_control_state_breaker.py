"""X2 — local_db control_state hardening: failures are loud (ERROR log +
exception), never silently stale, and a repeatedly failing DB trips a circuit
breaker that fails closed without touching the DB.
"""

import logging

import pytest

import local_db

# NOTE: access LocalDB / local_db.ControlStateError / _control_state_breaker through the
# module (never `from local_db import ...` at module import time): other test
# files reload local_db, which re-creates the class and exception objects.


@pytest.fixture
def breaker_reset():
    b = local_db._control_state_breaker
    saved = (b._consecutive_failures, b._opened_at)
    b._consecutive_failures = 0
    b._opened_at = 0.0
    yield b
    b._consecutive_failures, b._opened_at = saved


@pytest.fixture
def valid_db(tmp_path):
    db = local_db.LocalDB(db_path=str(tmp_path / "ok.db"), auto_sync=False)
    yield db
    try:
        db.close()
    except Exception:
        pass


def test_pause_state_roundtrip_on_valid_db(valid_db, breaker_reset):
    assert valid_db.get_control_state("pause_entries") is None
    assert valid_db.set_control_state("pause_entries", {"paused": True}) is True
    got = valid_db.get_control_state("pause_entries")
    assert got == {"paused": True}
    assert breaker_reset._consecutive_failures == 0


def test_set_control_state_broken_db_raises_and_logs_error(
        valid_db, breaker_reset, monkeypatch, caplog):
    def boom():
        raise RuntimeError("db is gone")
    monkeypatch.setattr(local_db.LocalDB, "_get_conn", lambda self: boom())
    with caplog.at_level(logging.ERROR, logger="local_db"):
        with pytest.raises(local_db.ControlStateError, match="set_control_state"):
            valid_db.set_control_state("pause_entries", {"paused": True})
    assert "pause_entries" in caplog.text
    assert "db is gone" in caplog.text
    assert "db_path" in caplog.text
    assert breaker_reset._consecutive_failures == 1


def test_get_control_state_broken_db_raises_and_logs_error(
        valid_db, breaker_reset, monkeypatch, caplog):
    def boom():
        raise RuntimeError("db is gone")
    monkeypatch.setattr(local_db.LocalDB, "_get_conn", lambda self: boom())
    with caplog.at_level(logging.ERROR, logger="local_db"):
        with pytest.raises(local_db.ControlStateError, match="get_control_state"):
            valid_db.get_control_state("pause_entries")
    assert "pause_entries" in caplog.text
    assert "db_path" in caplog.text
    assert breaker_reset._consecutive_failures == 1


def test_breaker_opens_after_threshold_and_refuses_without_db_access(
        valid_db, breaker_reset, monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("db is gone")
    monkeypatch.setattr(local_db.LocalDB, "_get_conn", lambda self: boom())
    for _ in range(breaker_reset.threshold):
        with pytest.raises(local_db.ControlStateError):
            valid_db.get_control_state("pause_entries")
    assert breaker_reset._opened_at
    hits = len(calls)
    with pytest.raises(local_db.ControlStateBreakerOpen):
        valid_db.get_control_state("pause_entries")
    with pytest.raises(local_db.ControlStateBreakerOpen):
        valid_db.set_control_state("pause_entries", {"paused": True})
    assert len(calls) == hits  # breaker refused without touching the DB


def test_success_resets_breaker(valid_db, breaker_reset, monkeypatch):
    def boom():
        raise RuntimeError("db is gone")
    monkeypatch.setattr(local_db.LocalDB, "_get_conn", lambda self: boom())
    with pytest.raises(local_db.ControlStateError):
        valid_db.get_control_state("pause_entries")
    assert breaker_reset._consecutive_failures == 1
    monkeypatch.undo()
    # a real success closes the breaker again
    assert valid_db.set_control_state("pause_entries", {"paused": False}) is True
    assert breaker_reset._consecutive_failures == 0
    assert breaker_reset._opened_at == 0.0
