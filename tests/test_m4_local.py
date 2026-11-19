"""M4 acceptance: local.py lapse arithmetic + lean checkpoints.

No mempalace CLI is invoked — local._mp is replaced with a recording fake.
"""
import json
import time
from datetime import datetime, timezone

import pytest

import local


def _fake_mp_factory(events=None, rc=0):
    calls = []

    def fake(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("logstream", "list"):
            return type("R", (), {"returncode": rc,
                                  "stdout": json.dumps({"events": events or []}),
                                  "stderr": ""})()
        return type("R", (), {"returncode": rc, "stdout": "{}", "stderr": ""})()

    return fake, calls


@pytest.fixture(autouse=True)
def clean_store(monkeypatch):
    # A fresh _STORE and no narrative flag per test.
    monkeypatch.setattr(local, "_STORE", {})
    monkeypatch.delenv("LOCAL_CHECKPOINT_NARRATIVE", raising=False)


def test_lapse_float_epoch_seconds():
    now = time.time()
    assert local._lapse_minutes(now - 600) == pytest.approx(10.0, abs=0.05)
    assert local._lapse_minutes(int(now - 60)) == pytest.approx(1.0, abs=0.05)


def test_lapse_iso_string():
    ts = datetime.fromtimestamp(time.time() - 300, tz=timezone.utc).isoformat()
    assert local._lapse_minutes(ts) == pytest.approx(5.0, abs=0.05)
    # Z-suffix variant
    z = datetime.fromtimestamp(time.time() - 120, tz=timezone.utc) \
        .isoformat().replace("+00:00", "Z")
    assert local._lapse_minutes(z) == pytest.approx(2.0, abs=0.05)


def test_lapse_naive_iso_assumed_utc():
    naive = datetime.fromtimestamp(time.time() - 60, tz=timezone.utc) \
        .replace(tzinfo=None).isoformat()
    assert local._lapse_minutes(naive) == pytest.approx(1.0, abs=0.05)


def test_lapse_garbage_and_future_clamp_to_zero():
    assert local._lapse_minutes("not a date") == 0.0
    assert local._lapse_minutes(None) == 0.0
    assert local._lapse_minutes(12345.0 * 10**12) == 0.0  # far future
    assert local._lapse_minutes(float("nan")) == 0.0


def test_checkpoint_update_flows_into_latest_checkpoint(monkeypatch):
    fake, calls = _fake_mp_factory()
    monkeypatch.setattr(local, "_mp", fake)
    local.checkpoint({"cycles_completed": 3, "session_pnl": 1.5,
                              "session_total": 2, "resume_key": "abc",
                              "last_activity": "2026-11-17T10:00:00+00:00"})
    latest = local.get_latest_checkpoint()
    assert latest["cycles_completed"] == 3
    assert latest["session_pnl"] == 1.5
    assert latest["session_total"] == 2
    assert latest["checkpoint_key"] == "abc"
    assert latest["last_activity"] == "2026-11-17T10:00:00+00:00"
    # Operational fields only — no narrative keys leak through.
    assert not set(latest) & {"self_narrative", "interests", "recent_thoughts",
                              "exploration_depth", "last_topic"}
    # And the checkpoint event really went through the mempalace append path.
    assert any(args[:2] == ("logstream", "append") for args in calls)


def test_default_checkpoint_state_is_lean(monkeypatch):
    fake, _ = _fake_mp_factory()
    monkeypatch.setattr(local, "_mp", fake)
    state = local.checkpoint()
    assert set(state) <= {"last_activity", "cycles_completed", "session_pnl",
                          "session_total", "resume_key"}
    assert "self_narrative" not in state


def test_default_checkpoint_state_with_narrative_flag(monkeypatch):
    monkeypatch.setenv("LOCAL_CHECKPOINT_NARRATIVE", "1")
    state = local._default_checkpoint_state()
    for key in ("self_narrative", "interests", "recent_thoughts"):
        assert key in state


def test_first_session_state_is_lean(monkeypatch, caplog):
    fake, _ = _fake_mp_factory(events=[])
    monkeypatch.setattr(local, "_mp", fake)
    state = local.on_session_start()
    assert "self_narrative" not in state
    assert state["resume_key"] == "first-session-ever"


def test_first_session_state_with_narrative_flag(monkeypatch):
    monkeypatch.setenv("LOCAL_CHECKPOINT_NARRATIVE", "1")
    fake, _ = _fake_mp_factory(events=[])
    monkeypatch.setattr(local, "_mp", fake)
    state = local.on_session_start()
    assert "self_narrative" in state


def test_session_start_float_last_activity_does_not_crash(monkeypatch):
    body = "CHECKPOINT_ENTITY:" + json.dumps(
        {"last_activity": time.time() - 600, "cycles_completed": 7,
         "resume_key": "k", "session_total": 4})
    fake, _ = _fake_mp_factory(events=[{"body": body}])
    monkeypatch.setattr(local, "_mp", fake)
    state = local.on_session_start()
    assert state["cycles_completed"] == 7
    assert local.get_latest_checkpoint()["cycles_completed"] == 7


def test_session_start_iso_last_activity_does_not_crash(monkeypatch):
    iso = datetime.fromtimestamp(time.time() - 600, tz=timezone.utc).isoformat()
    body = "CHECKPOINT_ENTITY:" + json.dumps(
        {"last_activity": iso, "cycles_completed": 9,
         "resume_key": "k", "session_total": 2})
    fake, _ = _fake_mp_factory(events=[{"body": body}])
    monkeypatch.setattr(local, "_mp", fake)
    state = local.on_session_start()
    assert state["cycles_completed"] == 9
