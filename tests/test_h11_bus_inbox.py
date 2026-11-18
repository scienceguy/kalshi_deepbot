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
