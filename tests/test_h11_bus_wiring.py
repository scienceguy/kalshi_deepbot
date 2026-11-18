"""H11 wiring: agent_bus._log_only_handler persists envelopes to bus_inbox.

The coding agent is not always running; a pushed message must land in the
durable inbox even when nothing drains it yet. These tests call the handler
directly against a throwaway LocalDB.
"""
import json

import agent_bus
import local_db


def _db(tmp_path, monkeypatch):
    db = local_db.LocalDB(db_path=str(tmp_path / "bus_inbox.db"), auto_sync=False)
    monkeypatch.setattr(local_db, "get_local_db", lambda *a, **kw: db)
    return db


def _env(**over):
    env = {
        "message_id": "msg-1",
        "type": "task.request",
        "from_agent": "ubuntu:opencode:kalshi_deepbot",
        "correlation_id": "task_123",
        "body": "hello",
    }
    env.update(over)
    return env


def test_handler_enqueues_pending_row(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    assert agent_bus._log_only_handler(_env()) == {"persisted": True}
    pending = db.get_pending_bus_messages()
    assert len(pending) == 1
    row = pending[0]
    assert row["message_id"] == "msg-1"
    assert row["type"] == "task.request"
    assert row["from_agent"] == "ubuntu:opencode:kalshi_deepbot"
    assert row["correlation_id"] == "task_123"
    assert row["body"] == "hello"


def test_handler_stores_dict_body_as_json(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    agent_bus._log_only_handler(_env(message_id="msg-json", body={"a": 1, "b": ["x"]}))
    pending = db.get_pending_bus_messages()
    assert len(pending) == 1
    assert json.loads(pending[0]["body"]) == {"a": 1, "b": ["x"]}


def test_handler_duplicate_message_id_no_raise_no_second_row(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    agent_bus._log_only_handler(_env())
    agent_bus._log_only_handler(_env())  # same message_id
    pending = db.get_pending_bus_messages()
    assert [r["message_id"] for r in pending] == ["msg-1"]


def test_handler_reports_not_durable_when_enqueue_returns_none(tmp_path, monkeypatch):
    """A failed INSERT is swallowed by enqueue and returns None — the handler
    must not claim durability just because nothing raised."""
    db = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "enqueue_bus_message", lambda *a, **kw: None)
    assert agent_bus._log_only_handler(_env(message_id="msg-none")) == {"persisted": False}


def test_handler_swallows_enqueue_failure(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "enqueue_bus_message",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    # never raises, but reports the message is not durable
    assert agent_bus._log_only_handler(_env(message_id="msg-fail")) == {"persisted": False}
