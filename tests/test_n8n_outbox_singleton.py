"""Regression: the n8n emit path must reuse the LocalDB singleton.

Raw `LocalDB()` per emitted event spawns a never-stopped daemon sync thread
plus a per-thread SQLite connection; on the live dry trader (2026-11-17)
that filled the 1024-fd ceiling alongside 257 threads. get_local_db() is
the process-wide singleton every other call site reuses.
"""
import inspect
from unittest.mock import MagicMock

import local_db
import n8n_client


def test_n8n_client_has_no_raw_localdb_construction():
    src = inspect.getsource(n8n_client)
    assert "LocalDB()" not in src, (
        "n8n_client must not construct LocalDB() directly; use get_local_db()")


def test_outbox_metrics_reads_via_singleton(monkeypatch):
    sentinel = MagicMock()
    sentinel.get_outbox_depth_stats.return_value = {
        "outbox_depth": 3,
        "oldest_undelivered_age_s": 1.0,
    }
    monkeypatch.setattr(local_db, "get_local_db", lambda: sentinel)
    stats = n8n_client.outbox_metrics()
    assert stats["outbox_depth"] == 3
    sentinel.get_outbox_depth_stats.assert_called_once_with()


def test_fire_and_forget_enqueues_via_singleton(monkeypatch):
    sentinel = MagicMock()
    monkeypatch.setattr(local_db, "get_local_db", lambda: sentinel)
    monkeypatch.setattr(n8n_client, "_start_outbox_sender", lambda: None)
    client = n8n_client.N8nClient(api_key="test_key")
    client.execute_webhook(
        "superbot-trade",
        {"venue": "kalshi", "mode": "dry", "type": "risk.halt",
         "payload": {"market": "X"}},
        fire_and_forget=True,
    )
    sentinel.insert_outbox_event.assert_called_once()
    kwargs = sentinel.insert_outbox_event.call_args.kwargs
    assert kwargs["delivery_class"] == "critical"
