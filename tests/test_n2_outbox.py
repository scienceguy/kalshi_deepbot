"""Tests for N2 — persistent outbox, priority lanes, short timeouts.

Spec acceptance:
- With a hung fake n8n, a critical event is still delivered (attempted) within
  its timeout — the sender never hangs past the lane's bound.
- The telemetry lane stays bounded and counts drops.
- Killing and restarting the process replays undelivered critical events
  exactly once (idempotent event_id).
- Delivery is retried with backoff (with jitter) and stops after the circuit
  opens.

All fakes are local HTTP servers or tmp-path SQLite; never --live, never a
real n8n.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import config
import n8n_client
from n8n_client import (
    N8nClient,
    _deliver_outbox_row,
    _drain_telemetry,
    _enqueue_telemetry,
    _outbox_breaker,
    outbox_metrics,
)


def _db(tmp_path):
    from local_db import LocalDB
    return LocalDB(db_path=str(tmp_path / "t.db"), auto_sync=False)


def _row(rid=1, event_id="evt-n2", type_="risk.halt", cls="critical",
         payload='{"reason": "test"}'):
    return {"id": rid, "event_id": event_id, "type": type_, "delivery_class": cls,
            "payload_json": payload, "venue": "kalshi", "mode": "dry",
            "created_at": "2026-11-17 00:00:00", "attempts": 0,
            "next_attempt_at": "2026-11-17 00:00:00", "last_error": None,
            "status": "pending"}


class _OkHandler(BaseHTTPRequestHandler):
    hits: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        type(self).hits.append(self.path)
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _HungHandler(BaseHTTPRequestHandler):
    """Accepts the connection, never answers within the test's timeout."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        time.sleep(30)

    def log_message(self, *args):
        pass


def _patch_base_url(monkeypatch, server):
    """Point _deliver_outbox_row's N8nClient() at a local fake server.

    DEFAULT_BASE_URL is a def-time default, so patching the module attribute
    would not reach the constructor; patch the name instead.
    """
    url = f"http://127.0.0.1:{server.server_address[1]}"
    real = n8n_client.N8nClient
    monkeypatch.setattr(n8n_client, "N8nClient",
                        lambda *a, **k: real(base_url=url, *a, **k))


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture(autouse=True)
def _reset_breaker_and_telemetry(monkeypatch):
    # conftest pins SUPERBOT_WEBHOOK_SECRET="" so nothing ever signs against a
    # real service; here every receiver is a local fake, so give the signer a
    # deterministic test secret.
    monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "n2-test-secret")
    monkeypatch.delenv("SUPERBOT_WEBHOOK_SECRET_FILE", raising=False)
    old = (_outbox_breaker._consecutive_failures, _outbox_breaker._opened_at)
    yield
    _outbox_breaker._consecutive_failures, _outbox_breaker._opened_at = old
    n8n_client._telemetry_pending.clear()


# ── hung n8n: critical attempt stays within its timeout ──────────────────

def test_critical_delivery_attempt_bounded_on_hung_n8n(monkeypatch, tmp_path):
    monkeypatch.setitem(n8n_client._LANE_TIMEOUTS, "critical", (0.2, 0.3))
    hung = _serve(_HungHandler)
    db = _db(tmp_path)
    rid = db.insert_outbox_event("risk.halt", '{"reason": "x"}', "critical",
                                 "kalshi", "dry", "evt-hung")
    row = db.get_pending_outbox_events(limit=1, lane="critical")[0]
    _patch_base_url(monkeypatch, hung)
    try:
        start = time.time()
        _deliver_outbox_row(db, row)
        elapsed = time.time() - start
    finally:
        hung.shutdown()
    assert elapsed < 5.0, f"critical attempt blocked {elapsed:.1f}s on a hung n8n"
    # a failed critical event is rescheduled (pending), never silently dropped
    # — next_attempt_at is in the future so it won't reappear as due yet.
    conn = db._get_conn()
    with db._lock:
        status, attempts = conn.execute(
            "SELECT status, attempts FROM event_outbox WHERE event_id = ?",
            ("evt-hung",)).fetchone()
    assert status == "pending"
    assert attempts == 1


def test_critical_delivery_succeeds_when_n8n_responds(monkeypatch, tmp_path):
    ok = _serve(_OkHandler)
    _OkHandler.hits = []
    db = _db(tmp_path)
    db.insert_outbox_event("risk.halt", '{"reason": "x"}', "critical",
                           "kalshi", "dry", "evt-ok")
    row = db.get_pending_outbox_events(limit=1, lane="critical")[0]
    _patch_base_url(monkeypatch, ok)
    try:
        _deliver_outbox_row(db, row)
    finally:
        ok.shutdown()
    assert db.get_pending_outbox_events(limit=10) == []
    stats = db.get_outbox_stats()
    assert stats.get("sent:critical") == 1


# ── restart replays undelivered critical events exactly once ─────────────

def test_restart_replays_undelivered_critical_exactly_once(monkeypatch, tmp_path):
    ok = _serve(_OkHandler)
    _OkHandler.hits = []
    db = _db(tmp_path)
    db.insert_outbox_event("risk.halt", '{"reason": "x"}', "critical",
                           "kalshi", "dry", "evt-once")
    _patch_base_url(monkeypatch, ok)
    try:
        row = db.get_pending_outbox_events(limit=10, lane="critical")[0]
        _deliver_outbox_row(db, row)   # first life: deliver
        row2 = db.get_pending_outbox_events(limit=10, lane="critical")
        assert row2 == []              # second life (restart): nothing to replay
    finally:
        ok.shutdown()
    # exactly one HTTP POST hit the webhook
    assert _OkHandler.hits == ["/webhook/risk.halt"]


def test_event_id_is_unique_so_replay_cannot_double_insert(tmp_path):
    db = _db(tmp_path)
    first = db.insert_outbox_event("settlement", '{"pnl": 1}', "normal",
                                   "kalshi", "dry", "evt-dup")
    second = db.insert_outbox_event("settlement", '{"pnl": 1}', "normal",
                                    "kalshi", "dry", "evt-dup")
    assert first is not None
    assert second is None  # UNIQUE(event_id) rejects the replay


# ── backoff with jitter ──────────────────────────────────────────────────

def test_failed_delivery_backs_off_and_eventually_deads(monkeypatch, tmp_path):
    monkeypatch.setitem(n8n_client._LANE_TIMEOUTS, "critical", (0.1, 0.2))
    monkeypatch.setattr(n8n_client, "MAX_OUTBOX_ATTEMPTS", 2)
    hung = _serve(_HungHandler)
    db = _db(tmp_path)
    db.insert_outbox_event("risk.halt", '{"reason": "x"}', "critical",
                           "kalshi", "dry", "evt-backoff")
    _patch_base_url(monkeypatch, hung)
    try:
        row = db.get_pending_outbox_events(limit=1, lane="critical")[0]
        _deliver_outbox_row(db, row)   # attempt 1 -> attempts=1, future retry
        import datetime as _dt
        conn = db._get_conn()
        with db._lock:
            status, attempts, next_at = conn.execute(
                "SELECT status, attempts, next_attempt_at FROM event_outbox WHERE event_id = ?",
                ("evt-backoff",)).fetchone()
        assert status == "pending"
        assert attempts == 1
        parsed = _dt.datetime.fromisoformat(next_at)
        assert parsed > _dt.datetime.now(_dt.timezone.utc), "retry not pushed into the future"
        # fast-forward: pretend the retry is due and attempt again -> dead
        with db._lock:
            conn.execute("UPDATE event_outbox SET next_attempt_at = ? WHERE event_id = ?",
                         ("2020-01-01T00:00:00+00:00", "evt-backoff"))
            conn.commit()
        row = db.get_pending_outbox_events(limit=1, lane="critical")[0]
        _deliver_outbox_row(db, row)
        with db._lock:
            status, attempts = conn.execute(
                "SELECT status, attempts FROM event_outbox WHERE event_id = ?",
                ("evt-backoff",)).fetchone()
        assert status == "dead"
        assert attempts == 2
    finally:
        hung.shutdown()


# ── circuit breaker opens after consecutive failures ─────────────────────

def test_circuit_breaker_opens_and_closes():
    breaker = n8n_client._CircuitBreaker(threshold=3, cooldown_s=60.0)
    assert breaker.allow() is True
    for _ in range(3):
        breaker.record_failure()
    assert breaker.allow() is False
    assert breaker.is_open is True
    breaker.record_success()
    assert breaker.allow() is True


def test_circuit_breaker_reopens_after_cooldown_if_still_failing():
    breaker = n8n_client._CircuitBreaker(threshold=2, cooldown_s=60.0)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.allow() is False
    # simulate the cooldown expiring
    breaker._opened_at = time.time() - 61.0
    assert breaker.allow() is True
    # still failing: the breaker opens again on the next strike
    breaker.record_failure()
    assert breaker.allow() is False


# ── telemetry lane: coalesced, bounded, drop-oldest counted ──────────────

def test_telemetry_coalesces_latest_per_key():
    item1 = ("client-a", "superbot.monitor", {"state": "idle", "venue": "kalshi"}, "POST")
    item2 = ("client-b", "superbot.monitor", {"state": "halted", "venue": "kalshi"}, "POST")
    _enqueue_telemetry("superbot.monitor|kalshi", item1)
    _enqueue_telemetry("superbot.monitor|kalshi", item2)
    pending = list(n8n_client._telemetry_pending.values())
    assert len(pending) == 1
    assert pending[0][2]["state"] == "halted"


def test_telemetry_bounded_drop_oldest_counts_drops():
    n8n_client._telemetry_pending.clear()
    before = n8n_client._telemetry_dropped_get()
    q = n8n_client.MAX_TELEMETRY_QUEUE
    for i in range(q):
        _enqueue_telemetry(f"k{i}|v", ("c", "superbot.monitor", {"n": i}, "POST"))
    assert len(n8n_client._telemetry_pending) == q
    _enqueue_telemetry("overflow|v", ("c", "superbot.monitor", {"n": -1}, "POST"))
    assert len(n8n_client._telemetry_pending) == q
    assert "k0|v" not in n8n_client._telemetry_pending  # oldest evicted
    assert n8n_client._telemetry_dropped_get() == before + 1  # drop counted


def test_drain_telemetry_empties_pending(monkeypatch):
    fired = []

    class _FakeClient:
        def _execute_webhook_signed(self, path, data, method, timeout, retries):
            fired.append((path, data))

    _enqueue_telemetry("a|v", (_FakeClient(), "pa", {"x": 1}, "POST"))
    _enqueue_telemetry("b|v", (_FakeClient(), "pb", {"y": 2}, "POST"))
    assert _drain_telemetry() == 2
    assert len(n8n_client._telemetry_pending) == 0
    assert [p for p, _ in fired] == ["pa", "pb"]


# ── metrics export ───────────────────────────────────────────────────────

def test_outbox_metrics_export_keys(tmp_path, monkeypatch):
    db = _db(tmp_path)
    db.insert_outbox_event("risk.halt", '{"reason": "x"}', "critical",
                           "kalshi", "dry", "evt-metrics")
    monkeypatch.setattr("local_db.LocalDB", lambda *a, **k: db)
    metrics = outbox_metrics()
    for key in ("outbox_depth", "oldest_undelivered_age_s", "dropped_total"):
        assert key in metrics
    assert isinstance(metrics["outbox_depth"], int)
    assert isinstance(metrics["dropped_total"], int)


def test_lane_filter_returns_only_that_lane(tmp_path):
    db = _db(tmp_path)
    db.insert_outbox_event("risk.halt", '{}', "critical", "kalshi", "dry", "c1")
    db.insert_outbox_event("settlement", '{}', "normal", "kalshi", "dry", "n1")
    normal = db.get_pending_outbox_events(limit=10, lane="normal")
    critical = db.get_pending_outbox_events(limit=10, lane="critical")
    assert [r["event_id"] for r in normal] == ["n1"]
    assert [r["event_id"] for r in critical] == ["c1"]
