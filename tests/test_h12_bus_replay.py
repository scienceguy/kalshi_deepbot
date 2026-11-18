"""H12 acceptance: bus replay protection — required message_id + TTL dedup."""
import json
import time
import urllib.error
import urllib.request

import pytest

import agent_bus
import agent_identity
import config


def _secret(monkeypatch, value="h12-test-secret"):
    monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", value)
    monkeypatch.delenv("SUPERBOT_WEBHOOK_SECRET_FILE", raising=False)


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, env):
        self.calls.append(env)


def test_missing_message_id_rejected(monkeypatch):
    _secret(monkeypatch)
    rec = _Recorder()
    receiver = agent_bus.BusReceiver(agent_identity.KALSHI_TRADER, rec)
    env = agent_bus.envelope(to_agent=agent_identity.KALSHI_TRADER,
                             body="no id", type="task.request")
    del env["message_id"]
    res = receiver.receive(env)
    assert res["ok"] is False
    assert "message_id" in res["reason"]
    assert rec.calls == []


def test_push_without_message_id_is_400(monkeypatch):
    """The HTTP push path rejects a missing message_id before dispatch."""
    _secret(monkeypatch)
    rec = _Recorder()
    server = agent_bus.BusServer(
        agent_bus.BusReceiver(agent_identity.POLY_TRADER, rec), port=0)
    port = server.start()
    try:
        env = agent_bus.envelope(to_agent=agent_identity.POLY_TRADER,
                                 body="no id", type="task.request")
        del env["message_id"]
        data = json.dumps(env).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/message", data=data, method="POST",
            headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=1.0)
        assert exc_info.value.code == 400
        assert rec.calls == []
    finally:
        server.stop()


def test_first_push_dispatched(monkeypatch):
    _secret(monkeypatch)
    rec = _Recorder()
    receiver = agent_bus.BusReceiver(agent_identity.KALSHI_TRADER, rec)
    env = agent_bus.envelope(to_agent=agent_identity.KALSHI_TRADER,
                             body="first", type="task.request")
    res = receiver.receive(env)
    assert res["ok"] is True
    assert res.get("duplicate") is not True
    assert len(rec.calls) == 1
    assert rec.calls[0]["body"] == "first"


def test_second_identical_push_not_dispatched(monkeypatch):
    _secret(monkeypatch)
    rec = _Recorder()
    receiver = agent_bus.BusReceiver(agent_identity.KALSHI_TRADER, rec)
    env = agent_bus.envelope(to_agent=agent_identity.KALSHI_TRADER,
                             body="replay me", type="task.request")
    r1 = receiver.receive(env)
    r2 = receiver.receive(dict(env))
    assert r1["ok"] is True
    assert r2["ok"] is True
    assert r2.get("duplicate") is True
    assert r2["message_id"] == env["message_id"]
    assert len(rec.calls) == 1


def test_distinct_message_ids_both_dispatched(monkeypatch):
    _secret(monkeypatch)
    rec = _Recorder()
    receiver = agent_bus.BusReceiver(agent_identity.KALSHI_TRADER, rec)
    env1 = agent_bus.envelope(to_agent=agent_identity.KALSHI_TRADER,
                              body="one", type="task.request")
    env2 = agent_bus.envelope(to_agent=agent_identity.KALSHI_TRADER,
                              body="two", type="task.request")
    assert env1["message_id"] != env2["message_id"]
    r1 = receiver.receive(env1)
    r2 = receiver.receive(env2)
    assert r1["ok"] is True and r2["ok"] is True
    assert r2.get("duplicate") is not True
    assert [c["body"] for c in rec.calls] == ["one", "two"]


def test_replay_allowed_again_after_ttl(monkeypatch):
    _secret(monkeypatch)
    monkeypatch.setattr(agent_bus, "MESSAGE_ID_TTL_MS", 1_000)
    rec = _Recorder()
    receiver = agent_bus.BusReceiver(agent_identity.KALSHI_TRADER, rec)
    t0 = int(time.time() * 1000)
    fake_now = [t0]
    receiver.now_ms = lambda: fake_now[0]

    env = agent_bus.envelope(to_agent=agent_identity.KALSHI_TRADER,
                             body="ttl check", type="task.request",
                             now_ms=t0)
    r1 = receiver.receive(env)
    assert r1["ok"] is True and r1.get("duplicate") is not True
    assert len(rec.calls) == 1

    fake_now[0] += 1_001  # past the 1s de-dup window; still within 60s nonce window
    r2 = receiver.receive(dict(env))
    assert r2["ok"] is True
    assert r2.get("duplicate") is not True
    assert len(rec.calls) == 2
