"""H11: durable inbox for the coding agent.

The coding agent (an OpenCode session) is not always running; bus messages
addressed to it must not be lost. bus_inbox is the durable fallback: senders
enqueue, the agent drains and acks.
"""
import local_db


def _db(tmp_path):
    return local_db.LocalDB(db_path=str(tmp_path / "bus_inbox.db"), auto_sync=False)


def test_enqueue_list_ack_roundtrip(tmp_path):
    db = _db(tmp_path)
    row_id = db.enqueue_bus_message(
        "m-1", "task.request", "ubuntu:opencode:kalshi_deepbot",
        "task_123", '{"body": "hello"}')
    assert isinstance(row_id, int)
    pending = db.get_pending_bus_messages()
    assert [r["message_id"] for r in pending] == ["m-1"]
    assert pending[0]["type"] == "task.request"
    assert pending[0]["from_agent"] == "ubuntu:opencode:kalshi_deepbot"
    assert pending[0]["correlation_id"] == "task_123"
    assert pending[0]["body"] == '{"body": "hello"}'
    assert db.mark_bus_message_delivered(row_id) is True
    assert db.get_pending_bus_messages() == []


def test_duplicate_message_id_rejected(tmp_path):
    db = _db(tmp_path)
    first = db.enqueue_bus_message("dup-1", "task.request", "a", "c", "{}")
    second = db.enqueue_bus_message("dup-1", "task.request", "a", "c", "{}")
    assert first is not None
    # a duplicate is still durable: report the existing row, not a failure
    assert second == first
    assert [r["message_id"] for r in db.get_pending_bus_messages()] == ["dup-1"]


def test_ordering_oldest_first(tmp_path):
    db = _db(tmp_path)
    for mid in ("m-1", "m-2", "m-3"):
        assert db.enqueue_bus_message(mid, "task.request", "from", "corr", "body") is not None
    assert [r["message_id"] for r in db.get_pending_bus_messages()] == ["m-1", "m-2", "m-3"]


def test_empty_inbox_no_op(tmp_path):
    db = _db(tmp_path)
    assert db.get_pending_bus_messages() == []
    # acking a nonexistent row must report failure, not a false "ok"
    assert db.mark_bus_message_delivered(999) is False
    assert db.get_pending_bus_messages() == []


# ── H3–H5: startup wiring (bus_startup.py) ─────────────────────────────

import bus_startup
import agent_identity


class _FakeBridge:
    def __init__(self):
        self.started = 0
        self.statuses = []

    def start(self):
        self.started += 1

    def status(self, status, extra=None):
        self.statuses.append((status, extra or {}))


def _enqueue(db, mid, type_="task.request", corr="task_1"):
    return db.enqueue_bus_message(mid, type_, "ubuntu:service:kalshi_trader",
                                  corr, '{"body": "hi"}')


def test_process_pending_marks_delivered_and_returns_rows(tmp_path):
    db = _db(tmp_path)
    _enqueue(db, "s-1")
    _enqueue(db, "s-2", type_="patch.ready", corr="task_2")
    seen = []
    delivered = bus_startup.process_pending_bus_inbox(
        db=db, handler=lambda m: seen.append(m["message_id"]))
    assert seen == ["s-1", "s-2"]               # oldest first
    assert [r["message_id"] for r in delivered] == ["s-1", "s-2"]
    assert db.get_pending_bus_messages() == []


def test_handler_failure_leaves_row_pending(tmp_path):
    db = _db(tmp_path)
    _enqueue(db, "ok-1")
    _enqueue(db, "bad-1")

    def handler(m):
        if m["message_id"] == "bad-1":
            raise RuntimeError("boom")

    delivered = bus_startup.process_pending_bus_inbox(db=db, handler=handler)
    assert [r["message_id"] for r in delivered] == ["ok-1"]
    remaining = db.get_pending_bus_messages()
    assert [r["message_id"] for r in remaining] == ["bad-1"]


def test_watcher_command_shape_and_cursor():
    cmd = bus_startup.watcher_command()
    assert cmd[:4] == ["mempalace", "logstream", "watch", "--agent"]
    assert agent_identity.CODING_AGENT in cmd
    for t in ("task.request", "task.reply", "patch.ready"):
        assert cmd.count(t) == 1
    assert cmd[-1] == "--json"
    assert "--since-event-id" not in cmd
    cmd2 = bus_startup.watcher_command(cursor="evt_123")
    assert cmd2[cmd2.index("--since-event-id") + 1] == "evt_123"


def test_arm_watcher_announces_filter_and_cursor_without_spawn():
    bridge = _FakeBridge()
    result = bus_startup.arm_watcher(cursor="evt_abc", bridge=bridge,
                                     spawn=False)
    assert bridge.started == 1
    assert bridge.statuses == [("watcher-armed",
                                {"filter": list(bus_startup.WATCHER_TYPES),
                                 "cursor": "evt_abc"})]
    assert result["pid"] is None
    assert result["cursor"] == "evt_abc"


def test_sweep_degrades_when_mempalace_unavailable(monkeypatch):
    monkeypatch.setattr(bus_startup, "_MEMPALACE_AVAILABLE", False)
    out = bus_startup.sweep_logstream_inbox()
    assert out["events"] == [] and out["cursor"] is None
    assert "skipped" in out


def test_startup_drains_and_arms_with_fakes(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _enqueue(db, "boot-1")
    bridge = _FakeBridge()
    monkeypatch.setattr(bus_startup, "sweep_logstream_inbox",
                        lambda **kw: {"events": [{"id": "evt_x"}],
                                      "cursor": "evt_x"})
    summary = bus_startup.startup(db=db, bridge=bridge, spawn_watcher=False)
    assert [r["message_id"] for r in summary["delivered"]] == ["boot-1"]
    assert db.get_pending_bus_messages() == []
    assert summary["cursor"] == "evt_x"
    assert summary["watcher"]["filter"] == list(bus_startup.WATCHER_TYPES)
    assert bridge.statuses[0][1]["cursor"] == "evt_x"
    assert summary["logstream_events"] == [{"id": "evt_x"}]
