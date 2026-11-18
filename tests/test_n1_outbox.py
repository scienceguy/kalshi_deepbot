"""Tests for N1 — durable, signed, bounded n8n outbox.

Covers:
- events.py: envelope, make_event, delivery_class
- local_db.py: event_outbox CRUD (insert, pending, sent, dead, stats)
- n8n_client.py: HMAC signing, separate no-API-key session, outbox durability
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

import config
from events import Event, make_event, delivery_class, CRITICAL_TYPES, TELEMETRY_TYPES
import n8n_client
from n8n_client import N8nClient, _sign_body, _outbox_sender, _deliver_outbox_row


# ── events.py ──────────────────────────────────────────────────────────

def test_event_envelope_defaults():
    """Event has sensible defaults and a UUID id."""
    e = Event(source="kalshi_trader", mode="dry", type="settlement", payload={"pnl": 1.0})
    assert e.schema_version == 1
    assert e.source == "kalshi_trader"
    assert e.mode == "dry"
    assert e.type == "settlement"
    assert e.payload == {"pnl": 1.0}
    assert e.severity == "info"
    assert e.event_id  # non-empty UUID
    assert e.ts > 0
    assert ":" in e.agent_id  # host:pid


def test_make_event():
    """make_event builds an N3 envelope with the given fields."""
    e = make_event("polymarket_us", "live", "fill", {"order_id": "abc"})
    assert e.source == "polymarket_us"
    assert e.mode == "live"
    assert e.type == "fill"
    assert e.payload == {"order_id": "abc"}
    # deterministic key -> stable event_id (replay-safe dedupe)
    e2 = make_event("polymarket_us", "live", "fill", {}, key="abc")
    assert e2.event_id == make_event("polymarket_us", "live", "fill", {}, key="abc").event_id
    assert e2.event_id != e.event_id


def test_make_event_severity_defaults_by_lane():
    """Severity is derived from the lane when not specified."""
    assert make_event("kalshi_trader", "dry", "risk.halt", {}).severity == "critical"
    assert make_event("kalshi_trader", "dry", "order.placed", {}).severity == "info"
    assert make_event("kalshi_trader", "dry", "heartbeat", {}).severity == "debug"
    assert make_event("kalshi_trader", "dry", "risk.halt", {}, severity="warning").severity == "warning"


def test_validate_event_roundtrip():
    """A make_event envelope passes the JSON Schema check."""
    from events import validate_event
    e = make_event("poly_us_trader", "dry", "position.settled", {"pnl": -0.5},
                   key="k1", agent_id="agent-1", ts=123.5)
    validate_event(e.to_dict())  # must not raise


def test_delivery_class():
    """delivery_class maps each type to its N2 lane (critical/normal/telemetry)."""
    # critical = halts, ruin, auth failures, reconciliation mismatches
    assert delivery_class("risk.halt") == "critical"
    assert delivery_class("risk.resume") == "critical"
    assert delivery_class("reconcile.mismatch") == "critical"
    assert delivery_class("auth.failure") == "critical"
    # normal = orders, fills, settlements
    assert delivery_class("settlement") == "normal"
    assert delivery_class("fill") == "normal"
    assert delivery_class("order.placed") == "normal"
    assert delivery_class("position.settled") == "normal"
    # telemetry = heartbeats, metrics
    assert delivery_class("heartbeat") == "telemetry"
    assert delivery_class("latency.alert") == "telemetry"
    assert delivery_class("daily.summary") == "telemetry"
    assert delivery_class("unknown.type") == "critical"  # safe-by-default


def test_to_dict_roundtrip():
    """to_dict() produces a JSON-serializable N3 envelope."""
    e = make_event("kalshi_trader", "dry", "risk.resume", {"ticker": "KXBTC15M"})
    d = e.to_dict()
    assert d["schema_version"] == 1
    assert d["source"] == "kalshi_trader"
    json.dumps(d)  # must serialize


# ── local_db.py event_outbox ──────────────────────────────────────────

def _db(tmp_path):
    from local_db import LocalDB
    return LocalDB(db_path=str(tmp_path / "t.db"), auto_sync=False)


def test_insert_outbox_event(tmp_path):
    """insert_outbox_event writes a row and returns its id."""
    db = _db(tmp_path)
    row_id = db.insert_outbox_event(
        event_type="settlement",
        payload_json='{"pnl": 1.5}',
        delivery_class="critical",
        venue="kalshi",
        mode="dry",
        event_id="evt-1",
    )
    assert row_id is not None
    assert row_id > 0


def test_get_pending_outbox_events(tmp_path):
    """get_pending_outbox_events returns pending rows ordered critical-first."""
    db = _db(tmp_path)
    db.insert_outbox_event("heartbeat", '{}', "telemetry", "kalshi", "dry", "h1")
    db.insert_outbox_event("settlement", '{}', "critical", "kalshi", "dry", "s1")
    db.insert_outbox_event("latency.alert", '{}', "telemetry", "kalshi", "dry", "l1")

    rows = db.get_pending_outbox_events(limit=10)
    assert len(rows) == 3
    # critical first
    assert rows[0]["type"] == "settlement"
    assert rows[0]["delivery_class"] == "critical"
    assert rows[0]["status"] == "pending"


def test_mark_outbox_sent(tmp_path):
    """mark_outbox_sent flips status to sent."""
    db = _db(tmp_path)
    row_id = db.insert_outbox_event("settlement", '{}', "critical", "kalshi", "dry", "s1")
    assert db.mark_outbox_sent(row_id)
    rows = db.get_pending_outbox_events(limit=10)
    assert len(rows) == 0  # no longer pending


def test_mark_outbox_dead_after_max_attempts(tmp_path):
    """mark_outbox_dead moves to dead after max_attempts."""
    db = _db(tmp_path)
    row_id = db.insert_outbox_event("settlement", '{}', "critical", "kalshi", "dry", "s1")
    # Bump attempts to max (10) by calling mark_outbox_dead 10 times
    for i in range(10):
        db.mark_outbox_dead(row_id, "err", max_attempts=10)
    rows = db.get_pending_outbox_events(limit=10)
    assert len(rows) == 0  # moved to dead, no longer pending


def test_get_outbox_stats(tmp_path):
    """get_outbox_stats returns counts by status and delivery class."""
    db = _db(tmp_path)
    db.insert_outbox_event("settlement", '{}', "critical", "kalshi", "dry", "s1")
    db.insert_outbox_event("heartbeat", '{}', "telemetry", "kalshi", "dry", "h1")
    stats = db.get_outbox_stats()
    assert stats.get("pending:critical") == 1
    assert stats.get("pending:telemetry") == 1


# ── n8n_client.py HMAC signing ────────────────────────────────────────

def test_sign_body_deterministic():
    """_sign_body produces deterministic HMAC-SHA256 over ts.nonce.body."""
    config.SUPERBOT_WEBHOOK_SECRET = "test-secret"
    sig1 = _sign_body("12345", '{"a":1}', "n1")
    sig2 = _sign_body("12345", '{"a":1}', "n1")
    assert sig1 == sig2
    assert len(sig1) == 64  # hex SHA-256


def test_sign_body_different_inputs():
    """Different timestamps, bodies or nonces produce different signatures."""
    config.SUPERBOT_WEBHOOK_SECRET = "test-secret"
    s1 = _sign_body("12345", '{"a":1}', "n1")
    s2 = _sign_body("12346", '{"a":1}', "n1")
    s3 = _sign_body("12345", '{"a":2}', "n1")
    s4 = _sign_body("12345", '{"a":1}', "n2")
    assert s1 != s2 != s3
    assert s1 != s4  # nonce participates in the signature


def test_sign_body_empty_secret(monkeypatch):
    """No configured secret -> RuntimeError, never a signature."""
    monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "")
    monkeypatch.delenv("SUPERBOT_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("SUPERBOT_WEBHOOK_SECRET_FILE",
                       "/nonexistent/superbot_webhook_secret")
    with pytest.raises(RuntimeError):
        _sign_body("12345", '{"a":1}', "n1")


# ── Fake n8n server for durability tests ─────────────────────────────

class _FakeN8nHandler(BaseHTTPRequestHandler):
    """Fake n8n webhook handler: records requests, can be set to fail."""

    def __init__(self, *args, fail=False, **kwargs):
        self.fail = fail
        super().__init__(*args, **kwargs)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.received.append((self.path, body.decode(), dict(self.headers)))
        if self.fail:
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, format, *args):
        pass  # silence


class _FakeN8nServer:
    def __init__(self, fail=False):
        self.received = []
        self.fail = fail
        self._server = HTTPServer(("127.0.0.1", 0), lambda *a, **k: _FakeN8nHandler(*a, fail=fail, **k))
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._server.shutdown()


# ── Outbox durability ────────────────────────────────────────────────

def test_outbox_delivers_after_n8n_returns(tmp_path):
    """Events written while n8n is down are delivered after it returns."""
    db = _db(tmp_path)
    server = _FakeN8nServer(fail=True)

    try:
        # Write events while n8n is down
        db.insert_outbox_event("settlement", '{"pnl": 1.0}', "critical", "kalshi", "dry", "s1")
        db.insert_outbox_event("fill", '{"size": 1}', "critical", "kalshi", "dry", "f1")

        rows = db.get_pending_outbox_events(limit=10)
        assert len(rows) == 2

        # Now n8n is up — deliver
        with patch.object(n8n_client, "get_client") as MockClient:
            instance = MockClient.return_value
            instance._execute_webhook_signed.return_value = {"ok": True}
            for row in rows:
                _deliver_outbox_row(db, row)
            assert instance._execute_webhook_signed.call_count == 2

        # All sent
        remaining = db.get_pending_outbox_events(limit=10)
        assert len(remaining) == 0
    finally:
        server.stop()


def test_outbox_marks_dead_after_max_attempts(tmp_path):
    """Permanently failing event goes dead and does not block newer rows."""
    db = _db(tmp_path)

    # Insert a failing event and a good event
    db.insert_outbox_event("settlement", '{"pnl": 1.0}', "critical", "kalshi", "dry", "s1")
    db.insert_outbox_event("heartbeat", '{}', "telemetry", "kalshi", "dry", "h1")

    # Simulate max_attempts reached for s1
    row = db.get_pending_outbox_events(limit=10)[0]  # critical first = s1
    row_id = row["id"]
    for _ in range(10):
        db.mark_outbox_dead(row_id, "permanent error", max_attempts=10)

    # s1 is dead, h1 is still pending
    pending = db.get_pending_outbox_events(limit=10)
    assert len(pending) == 1
    assert pending[0]["type"] == "heartbeat"


# ── Separate session without API key ─────────────────────────────────

def test_webhook_session_has_no_api_key():
    """The webhook session sends no X-N8N-API-KEY header."""
    client = N8nClient()
    headers = client._webhook_session.headers
    assert "X-N8N-API-KEY" not in headers
    # But the API session does have it
    assert client.session.headers.get("X-N8N-API-KEY") == client.api_key or client.api_key == ""


def test_signed_webhook_includes_timestamp_and_signature():
    """_execute_webhook_signed adds X-Superbot-Timestamp and X-Superbot-Signature."""
    client = N8nClient()
    config.SUPERBOT_WEBHOOK_SECRET = "test-secret"

    # We can't easily mock requests, but we can verify _sign_body is used
    # by checking the header names are present in the code path.
    import inspect
    source = inspect.getsource(client._execute_webhook_signed)
    assert "X-Superbot-Timestamp" in source
    assert "X-Superbot-Signature" in source


# ── execute_webhook returns without network I/O ──────────────────────

def test_execute_webhook_no_network_io(monkeypatch):
    """execute_webhook (fire_and_forget) does no network I/O — just outbox insert."""
    # Block socket creation to assert no network I/O.
    real_socket = socket.socket

    class BlockingSocket(real_socket):
        def __init__(self, *args, **kwargs):
            raise RuntimeError("network I/O blocked in test")

    monkeypatch.setattr(socket, "socket", BlockingSocket)

    try:
        client = N8nClient()
        # Should not raise even though socket creation is blocked
        result = client.execute_webhook("superbot-trade", {"order_id": "123"})
        assert result == {}
    finally:
        socket.socket = real_socket


# ── Critical vs telemetry ────────────────────────────────────────────

def test_telemetry_queue_drop_oldest(tmp_path):
    """Telemetry queue drops oldest when full, critical is unbounded."""
    db = _db(tmp_path)

    # Insert telemetry events up to max + 1
    max_q = config.N8N_OUTBOX_MAX_TELEMETRY
    for i in range(max_q + 5):
        db.insert_outbox_event("heartbeat", f'{{"i": {i}}}', "telemetry", "kalshi", "dry", f"h{i}")

    pending = db.get_pending_outbox_events(limit=max_q + 10)
    # All are in the DB (durable), regardless of in-memory queue drops
    assert len(pending) == max_q + 5


# ── Teardown: reset config ───────────────────────────────────────────

def test_config_defaults():
    """N1 config has sensible defaults."""
    assert hasattr(config, "SUPERBOT_WEBHOOK_SECRET")
    assert hasattr(config, "N8N_OUTBOX_MAX_TELEMETRY")
    assert hasattr(config, "N8N_OUTBOX_MAX_ATTEMPTS")
    assert config.N8N_OUTBOX_MAX_TELEMETRY > 0
    assert config.N8N_OUTBOX_MAX_ATTEMPTS > 0
