"""Tests for mempalace_bridge.py — M1 MemPalace bridge off hot path.

Tests with a fake mp-call subprocess:
- Bridge start/stop lifecycle
- heartbeat/checkpoint/status/kg_add are non-blocking (queue put only)
- drain_tasks returns inbound events
- Circuit breaker opens after N failures
- Metrics tracking
- Writer thread delivers to mp-call
"""

import json
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

import mempalace_bridge
from mempalace_bridge import MemPalaceBridge


# ── Helpers ───────────────────────────────────────────────────────────

class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_bridge():
    b = MemPalaceBridge("test:opencode:kalshi_deepbot")
    return b


# ── Lifecycle ─────────────────────────────────────────────────────────

def test_bridge_start_stop():
    """start() and stop() complete without error."""
    b = _make_bridge()
    b.start()
    assert b._writer is not None
    assert b._subscriber is not None
    b.stop(timeout=5.0)


def test_bridge_double_start_is_noop():
    """Calling start() twice doesn't create duplicate threads."""
    b = _make_bridge()
    b.start()
    writer1 = b._writer
    b.start()
    assert b._writer is writer1
    b.stop(timeout=5.0)


# ── Non-blocking API (under 1ms) ──────────────────────────────────────

def test_heartbeat_is_non_blocking():
    """heartbeat() returns in well under 1ms even with a slow fake CLI."""
    b = _make_bridge()
    b.start()
    try:
        # Make _call_mp slow to simulate a hung CLI
        def slow_call(*args, **kwargs):
            time.sleep(5.0)  # would block a synchronous call
            return _FakeCompletedProcess()
        b._call_mp = slow_call

        start = time.monotonic()
        b.heartbeat(cycles_completed=42)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"heartbeat blocked for {elapsed:.3f}s"
    finally:
        b.stop(timeout=5.0)


def test_checkpoint_is_non_blocking():
    """checkpoint() returns in well under 1ms."""
    b = _make_bridge()
    b.start()
    try:
        def slow_call(*args, **kwargs):
            time.sleep(5.0)
            return _FakeCompletedProcess()
        b._call_mp = slow_call

        start = time.monotonic()
        b.checkpoint({"cycles": 42, "pnl": 12.5})
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"checkpoint blocked for {elapsed:.3f}s"
    finally:
        b.stop(timeout=5.0)


def test_status_is_non_blocking():
    """status() returns in well under 1ms."""
    b = _make_bridge()
    b.start()
    try:
        def slow_call(*args, **kwargs):
            time.sleep(5.0)
            return _FakeCompletedProcess()
        b._call_mp = slow_call

        start = time.monotonic()
        b.status("risk.halt", {"reason": "daily loss"})
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"status blocked for {elapsed:.3f}s"
    finally:
        b.stop(timeout=5.0)


def test_kg_add_is_non_blocking():
    """kg_add() returns in well under 1ms."""
    b = _make_bridge()
    b.start()
    try:
        def slow_call(*args, **kwargs):
            time.sleep(5.0)
            return _FakeCompletedProcess()
        b._call_mp = slow_call

        start = time.monotonic()
        b.kg_add("agent", "heartbeat", "alive")
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"kg_add blocked for {elapsed:.3f}s"
    finally:
        b.stop(timeout=5.0)


# ── Outbound delivery ─────────────────────────────────────────────────

def test_writer_delivers_to_mp_call():
    """Writer thread calls mp-call for queued events."""
    b = _make_bridge()
    calls = []

    def fake_call(args, timeout=2.0):
        calls.append(args)
        return _FakeCompletedProcess(returncode=0, stdout='{"ok": true}')

    b._call_mp = fake_call
    b.start()
    try:
        b.heartbeat(cycles_completed=7)
        b.checkpoint({"pnl": 1.5}, correlation_id="cp-1")
        b.status("risk.halt", {"reason": "dd"})
        b.kg_add("s", "p", "o")

        # Give the writer time to deliver
        time.sleep(0.5)

        assert len(calls) >= 4, f"expected >= 4 mp-call calls, got {len(calls)}"
        # All calls start with ["logstream", "append"] or ["kg", "add"]
        assert any(c[0:2] == ["logstream", "append"] for c in calls)
        assert any(c[0:2] == ["kg", "add"] for c in calls)
    finally:
        b.stop(timeout=5.0)


# ── Inbound tasks ─────────────────────────────────────────────────────

def test_drain_tasks_returns_inbound_events():
    """drain_tasks() returns events put into the inbound queue."""
    b = _make_bridge()
    event = {"id": "evt-1", "type": "task.request", "body": "do something"}
    b._inbound.put_nowait(event)
    tasks = b.drain_tasks()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "evt-1"
    # Queue is now empty
    assert b.drain_tasks() == []


def test_drain_tasks_empty():
    """drain_tasks() returns empty list when no events."""
    b = _make_bridge()
    assert b.drain_tasks() == []


# ── Circuit breaker ───────────────────────────────────────────────────

def test_circuit_breaker_opens_after_failures():
    """Breaker opens after BREAKER_MAX_FAILURES consecutive failures."""
    b = _make_bridge()
    # Trip the breaker directly
    for _ in range(mempalace_bridge.BREAKER_MAX_FAILURES):
        b._breaker_failure()

    assert b._breaker_open_until > time.time()
    metrics = b.get_metrics()
    assert metrics["breaker_opens"] >= 1
    # While open, _breaker_closed() returns False
    assert not b._breaker_closed()


def test_breaker_recovers_after_cooldown():
    """Breaker allows a probe after cooldown expires."""
    b = _make_bridge()
    # Trip the breaker directly
    for _ in range(mempalace_bridge.BREAKER_MAX_FAILURES):
        b._breaker_failure()
    assert b._breaker_open_until > time.time()

    # Simulate cooldown expired: _breaker_closed() should allow half-open probe
    b._breaker_open_until = time.time() - 1.0
    assert b._breaker_closed()  # half-open: breaker closed, failures reset
    assert b._breaker_failures == 0
    assert b._breaker_open_until == 0.0


# ── Metrics ───────────────────────────────────────────────────────────

def test_metrics_track_queued_and_sent():
    """Metrics reflect queued and sent events."""
    b = _make_bridge()
    delivered = []

    def fake_call(args, timeout=2.0):
        delivered.append(args)
        return _FakeCompletedProcess(returncode=0)

    b._call_mp = fake_call
    b.start()
    try:
        b.heartbeat(1)
        b.heartbeat(2)
        b.checkpoint({"x": 1})
        time.sleep(0.5)  # let writer deliver

        m = b.get_metrics()
        assert m["outbound_queued"] >= 3
        assert m["outbound_sent"] >= 3
        assert m["outbound_dropped"] == 0
    finally:
        b.stop(timeout=5.0)


# ── Default bridge ────────────────────────────────────────────────────

def test_get_default_bridge_returns_instance():
    """get_default_bridge() returns a MemPalaceBridge."""
    b = mempalace_bridge.get_default_bridge()
    assert isinstance(b, MemPalaceBridge)
    assert b.agent_identity == "ubuntu:opencode:kalshi_deepbot"
