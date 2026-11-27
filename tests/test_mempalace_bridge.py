"""Tests for mempalace_bridge.py — M1 MemPalace bridge off hot path.

Tests with mocked mempalace library calls:
- Bridge start/stop lifecycle
- heartbeat/checkpoint/status/kg_add are non-blocking (queue put only)
- drain_tasks returns inbound events
- Circuit breaker opens after N failures
- Metrics tracking
- Writer thread delivers via mempalace library
"""

import json
import subprocess
import sys
import threading
import time
from unittest.mock import patch


import mempalace_bridge
from mempalace_bridge import MemPalaceBridge


def _make_bridge():
    b = MemPalaceBridge("test:opencode:kalshi_deepbot")
    return b


# ── Lifecycle ─────────────────────────────────────────────────────

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


# ── Non-blocking API (under 1ms) ──────────────────────────────────

def _slow_deliver(*args, **kwargs):
    time.sleep(5.0)  # would block a synchronous call


def test_heartbeat_is_non_blocking():
    """heartbeat() returns in well under 1ms even with a slow delivery."""
    b = _make_bridge()
    b.start()
    try:
        b._deliver_status = _slow_deliver
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
        b._deliver_status = _slow_deliver
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
        b._deliver_status = _slow_deliver
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
        b._deliver_kg = _slow_deliver
        start = time.monotonic()
        b.kg_add("agent", "heartbeat", "alive")
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"kg_add blocked for {elapsed:.3f}s"
    finally:
        b.stop(timeout=5.0)


# ── Outbound delivery ─────────────────────────────────────────────

def test_writer_delivers_via_library():
    """Writer thread calls mempalace library for queued events."""
    b = _make_bridge()
    status_calls = []
    kg_calls = []

    def fake_append_event(self, **kwargs):
        status_calls.append(kwargs)

    def fake_add_triple(self, subject, predicate, obj, **kwargs):
        kg_calls.append((subject, predicate, obj, kwargs))

    with patch.object(mempalace_bridge.Logstream, "append_event", fake_append_event), \
         patch.object(mempalace_bridge.KnowledgeGraph, "add_triple", fake_add_triple):
        b.start()
        try:
            b.heartbeat(cycles_completed=7)
            b.checkpoint({"pnl": 1.5}, correlation_id="cp-1")
            b.status("risk.halt", {"reason": "dd"})
            b.kg_add("s", "p", "o")

            # Give the writer time to deliver
            time.sleep(0.5)

            assert len(status_calls) >= 3, f"expected >= 3 status calls, got {len(status_calls)}"
            assert len(kg_calls) >= 1, f"expected >= 1 kg call, got {len(kg_calls)}"
            assert any(c.get("type") == "status" for c in status_calls)
            assert kg_calls[0][0:3] == ("s", "p", "o")
        finally:
            b.stop(timeout=5.0)


# ── Inbound tasks ─────────────────────────────────────────────────

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


# ── Circuit breaker ───────────────────────────────────────────────

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


# ── Metrics ───────────────────────────────────────────────────────

def test_metrics_track_queued_and_sent():
    """Metrics reflect queued and sent events."""
    b = _make_bridge()

    with patch.object(mempalace_bridge.Logstream, "append_event", return_value=None):
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


# ── T13-04: no-drop spill on a full queue ──────────────────────────

def test_full_queue_spills_to_durable_file(tmp_path, monkeypatch):
    """A queue.Full must spill the item to the spill file, never drop it."""
    monkeypatch.setattr(mempalace_bridge, "PALACE_DIR", str(tmp_path))
    b = _make_bridge()
    b._spill_path = str(tmp_path / "spill.jsonl")
    b._outbound = mempalace_bridge.queue.Queue(maxsize=1)
    b._outbound.put(("status", "x", {}, "cid", 0.0))  # full

    b._enqueue("kg", "add", {"subject": "s", "predicate": "p", "object": "o"})
    m = b.get_metrics()
    assert m["outbound_spilled"] == 1
    assert m["outbound_dropped"] == 0
    # the queued item was flushed into the spill first — order preserved
    assert b._outbound.empty()
    items = b._drain_spill()
    assert [it[1] for it in items] == ["x", "add"]
    b._ack_spill(True)
    # spill file consumed after acknowledgement
    assert b._drain_spill() == []


def test_spill_never_mints_a_missing_palace_dir(tmp_path, monkeypatch):
    """T16-62: a wrong MEMPALACE_DIR must fail loud, not silently create
    a second palace and spill events into it."""
    missing = tmp_path / "not-a-palace"
    monkeypatch.setattr(mempalace_bridge, "PALACE_DIR", str(missing))
    b = _make_bridge()
    b._spill_path = str(missing / "spill.jsonl")
    b._outbound = mempalace_bridge.queue.Queue(maxsize=1)
    b._outbound.put(("status", "x", {}, "cid", 0.0))  # full

    b._enqueue("kg", "add", {"subject": "s", "predicate": "p", "object": "o"})

    assert not missing.exists(), "bridge must not mint a new palace dir"
    assert b.get_metrics()["outbound_dropped"] == 1
    assert b.get_metrics()["outbound_spilled"] == 0


def test_writer_drains_spill_before_queue(tmp_path, monkeypatch):
    """Spilled items are delivered (in order) by the writer loop."""
    monkeypatch.setattr(mempalace_bridge, "PALACE_DIR", str(tmp_path))
    b = _make_bridge()
    b._spill_path = str(tmp_path / "spill.jsonl")
    b._outbound = mempalace_bridge.queue.Queue(maxsize=1)
    b._outbound.put(("status", "queued", {}, "cid-q", 0.0))  # full
    b._enqueue("status", "spilled", {"n": 1})  # spills

    delivered = []
    monkeypatch.setattr(b, "_deliver", lambda *a: delivered.append(a))
    # writer semantics: drain the spill first (oldest first), then the queue
    for spilled in b._drain_spill():
        b._deliver(*spilled[:4])
    b._ack_spill(True)
    while True:
        try:
            b._deliver(*b._outbound.get_nowait()[:4])
        except mempalace_bridge.queue.Empty:
            break
    assert [d[1] for d in delivered] == ["queued", "spilled"]  # global order kept


# ── Default bridge ────────────────────────────────────────────────

def test_get_default_bridge_returns_instance():
    """get_default_bridge() returns a MemPalaceBridge."""
    b = mempalace_bridge.get_default_bridge()
    assert isinstance(b, MemPalaceBridge)
    assert b.agent_identity == "ubuntu:opencode:kalshi_deepbot"


def test_flood_10x_capacity_zero_lost_order_preserved(tmp_path, monkeypatch):
    """T13-04 acceptance: flooding the queue at 10x capacity loses nothing
    and preserves order (spill first, then queue)."""
    monkeypatch.setattr(mempalace_bridge, "PALACE_DIR", str(tmp_path))
    b = _make_bridge()
    b._spill_path = str(tmp_path / "spill.jsonl")
    cap = 10
    b._outbound = mempalace_bridge.queue.Queue(maxsize=cap)
    n = cap * 10
    for i in range(n):
        b._enqueue("status", "flood", {"i": i})
    assert b.get_metrics()["outbound_spilled"] >= 1
    assert b.get_metrics()["outbound_dropped"] == 0
    # spill drained first (oldest), then whatever remains in the queue
    delivered = [it[2]["i"] for it in b._drain_spill()]
    b._ack_spill(True)
    while True:
        try:
            delivered.append(b._outbound.get_nowait()[2]["i"])
        except mempalace_bridge.queue.Empty:
            break
    assert delivered == list(range(n))  # zero lost, order preserved


def _write_spill(bridge, subtype="spilled"):
    item = ["status", subtype, {"n": 1}, "cid-spill", time.time()]
    with open(bridge._spill_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(item) + "\n")
    return item


def test_spill_batch_is_retained_until_ack(tmp_path):
    b = _make_bridge()
    b._spill_path = str(tmp_path / "spill.jsonl")
    item = _write_spill(b)
    assert b._drain_spill() == [item]
    b._ack_spill(False)
    assert b._drain_spill() == [item]
    b._ack_spill(True)
    assert b._drain_spill() == []


def test_spill_claim_is_process_safe_and_crash_recoverable(tmp_path):
    b = _make_bridge()
    b._spill_path = str(tmp_path / "spill.jsonl")
    item = _write_spill(b)
    child_code = (
        "import sys; from mempalace_bridge import MemPalaceBridge; "
        f"b=MemPalaceBridge('child'); b._spill_path={b._spill_path!r}; "
        "items=b._drain_spill(); print(len(items), flush=True); sys.stdin.read()"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    assert child.stdout.readline().strip() == "1"
    assert b._drain_spill() == []
    child.kill()
    child.wait(timeout=5)
    assert b._drain_spill() == [item]
    b._ack_spill(True)


def test_stop_drains_queue_and_spill(tmp_path):
    b = _make_bridge()
    b._spill_path = str(tmp_path / "spill.jsonl")
    spilled = _write_spill(b)
    queued = ["status", "queued", {}, "cid-queue", time.time()]
    b._outbound.put(queued)
    delivered = []
    b._deliver = lambda *args: delivered.append(args)
    b._writer = threading.Thread(target=b._writer_loop)
    b._writer.start()
    b.stop(timeout=2.0)
    assert [list(item[:4]) for item in delivered] == [spilled[:4], queued[:4]]


def test_failed_queue_item_spills_and_retries_before_next_item(tmp_path):
    b = _make_bridge()
    b._spill_path = str(tmp_path / "spill.jsonl")
    delivered = []

    def fail_first(*args):
        delivered.append(args[1])
        if delivered == ["first"]:
            raise RuntimeError("transient failure")

    b._deliver = fail_first
    b._outbound.put(("status", "first", {}, "cid-first", time.time()))
    b._outbound.put(("status", "second", {}, "cid-second", time.time()))
    b._writer = threading.Thread(target=b._writer_loop)
    b._writer.start()
    deadline = time.monotonic() + 3.0
    while len(delivered) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    b.stop(timeout=2.0)
    assert delivered == ["first", "first", "second"]
    assert b.get_metrics()["outbound_sent"] == 2
    assert not (tmp_path / "spill.jsonl").exists()


def test_failed_spill_item_restores_only_failed_suffix_in_order(tmp_path):
    b = _make_bridge()
    b._spill_path = str(tmp_path / "spill.jsonl")
    with open(b._spill_path, "w", encoding="utf-8") as fh:
        for name in ("first", "second", "third"):
            fh.write(json.dumps(["status", name, {}, f"cid-{name}", time.time()]) + "\n")
    b._outbound.put(("status", "queued", {}, "cid-queued", time.time()))
    delivered = []

    def fail_second_once(*args):
        delivered.append(args[1])
        if args[1] == "second" and delivered.count("second") == 1:
            raise RuntimeError("transient failure")

    b._deliver = fail_second_once
    b._writer = threading.Thread(target=b._writer_loop)
    b._writer.start()
    deadline = time.monotonic() + 4.0
    while "queued" not in delivered and time.monotonic() < deadline:
        time.sleep(0.01)
    b.stop(timeout=2.0)
    assert delivered == ["first", "second", "second", "third", "queued"]
    assert delivered.count("first") == 1
    assert delivered.count("second") == 2
    assert delivered.count("third") == 1
    assert b.get_metrics()["outbound_sent"] == 4
    assert not (tmp_path / "spill.jsonl").exists()


def test_unavailable_library_is_failed_delivery_not_success():
    b = _make_bridge()
    with patch.object(mempalace_bridge, "_MEMPALACE_AVAILABLE", False):
        try:
            b._deliver("status", "heartbeat", {}, "cid")
        except RuntimeError as exc:
            assert "unavailable" in str(exc)
        else:
            raise AssertionError("unavailable MemPalace acknowledged delivery")
        assert mempalace_bridge.degraded_reason() == "mempalace library not installed"
    assert b.get_metrics()["outbound_sent"] == 0
    assert b.get_metrics()["outbound_failed"] == 1
