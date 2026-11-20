"""C1 acceptance: push-based localhost message bus with acks and presence.

All sign/verify uses the shared webhook secret (monkeypatched, same
pattern as test_n1_webhook_auth). One real 127.0.0.1 round-trip exercises
the socket path; the rest are socket-free or inject fakes.
"""
import json
import time
import urllib.request
from datetime import datetime, timezone


import agent_bus
import agent_identity
import config
import mempalace_bridge
import n8n_coordination
import n8n_webhook_auth


def _secret(monkeypatch, value="c1-test-secret"):
    monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", value)
    monkeypatch.delenv("SUPERBOT_WEBHOOK_SECRET_FILE", raising=False)
    # B3: clear the nonce store so tests don't hit replay protection
    n8n_coordination._clear_nonce_store_for_test()


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc) \
        .isoformat().replace("+00:00", "Z")


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, env):
        self.calls.append(env)


class _FakeLS:
    def __init__(self, events):
        self._events = events

    def list_events(self, **kwargs):
        return list(self._events)


def test_message_reaches_receiver_without_poll(monkeypatch):
    _secret(monkeypatch)
    rec = _Recorder()
    receiver = agent_bus.BusReceiver(agent_identity.POLY_TRADER, rec)
    server = agent_bus.BusServer(receiver, port=0)
    port = server.start()
    try:
        env = agent_bus.envelope(to_agent=agent_identity.POLY_TRADER,
                                 body="push me", type="task.request",
                                 topic="c1-test")
        resp = agent_bus.post_envelope(env, port=port, timeout=1.0)
        assert resp.get("ok") is True
        assert rec.calls and rec.calls[0]["body"] == "push me"
        assert rec.calls[0]["to_agent"] == agent_identity.POLY_TRADER
    finally:
        server.stop()


def test_duplicate_message_id_processed_once(monkeypatch):
    _secret(monkeypatch)
    rec = _Recorder()
    receiver = agent_bus.BusReceiver(agent_identity.KALSHI_TRADER, rec)
    env = agent_bus.envelope(to_agent=agent_identity.KALSHI_TRADER,
                             body="once", type="task.request")
    r1 = receiver.receive(env)
    r2 = receiver.receive(dict(env))
    assert r1["ok"] is True and not r1.get("duplicate")
    assert r2["ok"] is True and r2["duplicate"] is True
    assert len(rec.calls) == 1


def test_expired_message_dropped(monkeypatch):
    _secret(monkeypatch)
    rec = _Recorder()
    receiver = agent_bus.BusReceiver(agent_identity.KALSHI_TRADER, rec)
    old_ms = int((time.time() - 10) * 1000)
    env = agent_bus.envelope(to_agent=agent_identity.KALSHI_TRADER, body="late",
                             type="task.request", ttl_s=5.0, now_ms=old_ms)
    res = receiver.receive(env)
    assert res["ok"] is False and res["reason"] == "expired"
    assert rec.calls == []
    assert len(receiver.dead_letters) == 1


def test_tampered_envelope_rejected(monkeypatch):
    _secret(monkeypatch)
    rec = _Recorder()
    receiver = agent_bus.BusReceiver(agent_identity.KALSHI_TRADER, rec)
    env = agent_bus.envelope(to_agent=agent_identity.KALSHI_TRADER,
                             body="original", type="task.request")
    env["body"] = "changed after signing"
    res = receiver.receive(env)
    assert res["ok"] is False
    assert "signature mismatch" in res["reason"]
    assert rec.calls == []
    assert len(receiver.rejected) == 1


def test_not_addressed_to_me_refused(monkeypatch):
    _secret(monkeypatch)
    rec = _Recorder()
    receiver = agent_bus.BusReceiver(agent_identity.KALSHI_TRADER, rec)
    env = agent_bus.envelope(to_agent=agent_identity.POLY_TRADER,
                             body="for poly", type="task.request")
    res = receiver.receive(env)
    assert res["ok"] is False and "not addressed" in res["reason"]
    assert rec.calls == []


def test_stale_peer_reported_absent():
    now = time.time()
    stale = _FakeLS([{"created_at": _iso(now - 300)}])
    fresh = _FakeLS([{"created_at": _iso(now - 5)}])
    empty = _FakeLS([])
    assert agent_bus.peer_status("p1", ls=stale, now=now) == "stale"
    assert agent_bus.peer_alive("p1", ls=stale, now=now) is False
    assert agent_bus.peer_status("p1", ls=fresh, now=now) == "alive"
    assert agent_bus.peer_alive("p1", ls=fresh, now=now) is True
    assert agent_bus.peer_status("p1", ls=empty, now=now) == "unknown"

    class _Boom:
        def list_events(self, **kwargs):
            raise RuntimeError("db down")

    assert agent_bus.peer_status("p1", ls=_Boom(), now=now) == "unknown"


def test_bus_down_returns_error_not_exception(monkeypatch):
    _secret(monkeypatch)

    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()  # nothing listening now

    env = agent_bus.envelope(to_agent=agent_identity.KALSHI_TRADER,
                             body="anyone home?", type="task.request")
    res = agent_bus.post_envelope(env, port=free_port, timeout=0.5)
    assert res["ok"] is False  # error dict, never an exception


def test_unknown_peer_status_still_attempts_post(monkeypatch):
    _secret(monkeypatch)
    monkeypatch.setattr(agent_bus, "peer_status", lambda *a, **kw: "unknown")
    posted = []

    def fake_post(env, *, port=None, timeout=1.0):
        posted.append(env)
        return {"ok": True, "message_id": env["message_id"]}

    monkeypatch.setattr(agent_bus, "post_envelope", fake_post)
    result = n8n_coordination._post_to_bus(
        agent_identity.KALSHI_TRADER, "task.request", "hello",
        "kalshi-superbot", None)
    assert result["delivered"] is True
    assert posted and posted[0]["to_agent"] == agent_identity.KALSHI_TRADER


def test_stale_peer_refused_no_post(monkeypatch):
    _secret(monkeypatch)
    monkeypatch.setattr(agent_bus, "peer_status", lambda *a, **kw: "stale")
    posted = []
    monkeypatch.setattr(agent_bus, "post_envelope",
                        lambda env, **kw: posted.append(env) or {"ok": True})
    result = n8n_coordination._post_to_bus(
        agent_identity.KALSHI_TRADER, "task.request", "hello",
        "kalshi-superbot", None)
    assert result == {"delivered": False, "reason": "stale peer refused"}
    assert posted == []


def test_broadcast_is_not_pushed():
    result = n8n_coordination._post_to_bus(
        "*", "status", "everyone", "kalshi-superbot", None)
    assert result == {"delivered": False, "reason": "broadcast"}


def test_ack_through_bridge_when_event_id_present(monkeypatch):
    _secret(monkeypatch)

    class _FakeBridge:
        def __init__(self):
            self.acks = []

        def ack_event(self, event_id, body=""):
            self.acks.append((event_id, body))

    bridge = _FakeBridge()
    rec = _Recorder()
    receiver = agent_bus.BusReceiver(agent_identity.KALSHI_TRADER, rec,
                                     bridge=bridge)
    env = agent_bus.envelope(to_agent=agent_identity.KALSHI_TRADER,
                             body="do the thing", type="task.request",
                             event_id="evt_c1_test_001")
    receiver.receive(env)
    assert bridge.acks == [("evt_c1_test_001",
                            f"received by {agent_identity.KALSHI_TRADER}")]


def test_bridge_ack_event_enqueues_without_writer(monkeypatch):
    bridge = mempalace_bridge.MemPalaceBridge("ubuntu:test:c1ack")
    bridge.ack_event("evt_c1_ack_enqueue", body="hi")
    item = bridge._outbound.get_nowait()
    cmd_type, subtype, payload = item[0], item[1], item[2]
    assert cmd_type == "ack" and subtype == "ack"
    assert payload == {"event_id": "evt_c1_ack_enqueue", "body": "hi", "status": "claimed"}


def test_http_status_and_healthz(monkeypatch):
    _secret(monkeypatch)
    rec = _Recorder()
    receiver = agent_bus.BusReceiver(agent_identity.POLY_TRADER, rec)
    server = agent_bus.BusServer(receiver, port=0)
    port = server.start()
    try:
        health = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=1.0)
        assert health.status == 200
        assert json.loads(health.read())["ok"] is True

        status = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/status", timeout=1.0)
        assert json.loads(status.read())["identity"] == agent_identity.POLY_TRADER
    finally:
        server.stop()


def test_traders_wire_bus_receiver():
    """The startup wiring reaches both traders: kalshi via handle_task glue,
    poly log-only. Mirrors the M1 bridge wiring gate."""
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent
    trader_src = (src / "trader.py").read_text()
    assert "BusServer(BusReceiver(agent_identity.KALSHI_TRADER" in trader_src
    assert "_bus_task" in trader_src and "_handle_task(" in trader_src
    poly_src = (src / "poly_us_trader.py").read_text()
    assert "_bus_log" in poly_src
    assert "POLY_TRADER" in poly_src


def test_coordination_webhook_pushes_to_bus(monkeypatch, capsys):
    """End-to-end shape: the n8n coordination path appends to the logstream
    (faked) and pushes the same message to the target's bus (faked)."""
    import io
    import subprocess
    import sys

    _secret(monkeypatch, "handler-secret")
    ts = str(int(time.time() * 1000))
    nonce = "ab" * 16
    msg = {"to_agent": agent_identity.KALSHI_TRADER,
           "type": "task.request", "body": "please report"}
    body_str = json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
    sig = n8n_webhook_auth.sign_body(ts, nonce, body_str)
    payload = {"body": msg, "headers": {
        "x-superbot-timestamp": ts, "x-superbot-nonce": nonce,
        "x-superbot-signature": sig}}

    class _Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Result())
    monkeypatch.setattr(agent_bus, "peer_status", lambda *a, **kw: "unknown")
    posted = []
    monkeypatch.setattr(agent_bus, "post_envelope",
                        lambda env, **kw: posted.append(env) or {"ok": True})
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    rc = n8n_coordination.main()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["success"] is True
    assert out["bus"]["delivered"] is True
    assert posted and posted[0]["to_agent"] == agent_identity.KALSHI_TRADER
    assert posted[0]["body"] == "please report"
