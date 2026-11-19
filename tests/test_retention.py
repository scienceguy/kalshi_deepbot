"""Retention pruning for unbounded control tables (latency_measurements,
pending_send). Verifies age-based deletion, the pending-status guard, and the
hourly throttle on the write path.
"""
import types

import local_db


def _db(tmp_path):
    return local_db.LocalDB(db_path=str(tmp_path / "t.db"), auto_sync=False)


def _insert_latency(db, stage, created_at):
    conn = db._get_conn()
    with db._lock:
        if created_at is None:
            conn.execute(
                "INSERT INTO latency_measurements (stage, duration_ms) "
                "VALUES (?, ?)", (stage, 1.0))
        else:
            conn.execute(
                "INSERT INTO latency_measurements (stage, duration_ms, created_at) "
                "VALUES (?, ?, ?)", (stage, 1.0, created_at))
        conn.commit()


def _insert_pending(db, order_id, status, created_at):
    conn = db._get_conn()
    with db._lock:
        if created_at is None:
            conn.execute(
                "INSERT INTO pending_send "
                "(order_id, ticker, side, price_cents, contracts, dollar_risk, "
                " venue, mode, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, "KXBTC15M-1", "yes", 90, 1.0, 0.9,
                 "kalshi", "dry", status))
        else:
            conn.execute(
                "INSERT INTO pending_send "
                "(order_id, ticker, side, price_cents, contracts, dollar_risk, "
                " venue, mode, created_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, "KXBTC15M-1", "yes", 90, 1.0, 0.9,
                 "kalshi", "dry", created_at, status))
        conn.commit()


def _latency_count(db):
    conn = db._get_conn()
    with db._lock:
        return conn.execute(
            "SELECT COUNT(*) FROM latency_measurements").fetchone()[0]


def test_prune_latency_deletes_only_old(tmp_path):
    db = _db(tmp_path)
    _insert_latency(db, "old", "2020-01-01 00:00:00")
    _insert_latency(db, "fresh", None)
    out = db.prune_control_tables(latency_keep_days=7)
    assert out["latency_deleted"] == 1
    assert _latency_count(db) == 1
    conn = db._get_conn()
    with db._lock:
        row = conn.execute(
            "SELECT stage FROM latency_measurements").fetchone()
    assert row[0] == "fresh"


def test_prune_pending_respects_status(tmp_path):
    db = _db(tmp_path)
    _insert_pending(db, "old-pending", "pending", "2020-01-01 00:00:00")
    _insert_pending(db, "old-confirmed", "confirmed", "2020-01-01 00:00:00")
    _insert_pending(db, "old-failed", "failed", "2020-01-01 00:00:00")
    _insert_pending(db, "fresh-cancelled", "cancelled", None)
    out = db.prune_control_tables(pending_keep_days=7)
    assert out["pending_deleted"] == 2
    conn = db._get_conn()
    with db._lock:
        remaining = {r[0] for r in conn.execute(
            "SELECT order_id FROM pending_send").fetchall()}
    assert remaining == {"old-pending", "fresh-cancelled"}


def test_prune_marks_stale_pending_and_drops_delivered_inbox(tmp_path):
    db = _db(tmp_path)
    _insert_pending(db, "stale", "pending", "2020-01-01 00:00:00")
    conn = db._get_conn()
    with db._lock:
        conn.execute(
            "INSERT INTO bus_inbox (message_id, type, from_agent, "
            "correlation_id, body, status, created_at, delivered_at) "
            "VALUES ('m-delivered', 'task.request', 'a', 'c', '{}', "
            "'delivered', '2020-01-01 00:00:00', '2020-01-01 00:00:00')")
        conn.execute(
            "INSERT INTO bus_inbox (message_id, type, from_agent, "
            "correlation_id, body, status, created_at) "
            "VALUES ('m-pending', 'task.request', 'a', 'c', '{}', "
            "'pending', '2020-01-01 00:00:00')")
        conn.commit()
    out = db.prune_control_tables()
    assert out["inbox_deleted"] == 1
    assert out["pending_abandoned"] == 1
    assert out["pending_deleted"] == 0
    with db._lock:
        status = conn.execute(
            "SELECT status FROM pending_send WHERE order_id = 'stale'"
        ).fetchone()[0]
        ids = {r[0] for r in conn.execute(
            "SELECT message_id FROM bus_inbox").fetchall()}
    assert status == "failed"      # marked abandoned, never deleted
    assert ids == {"m-pending"}    # un-acked inbox rows are kept


def test_prune_empty_db_returns_zero_counts(tmp_path):
    db = _db(tmp_path)
    out = db.prune_control_tables()
    assert out == {"latency_deleted": 0, "pending_deleted": 0,
                   "inbox_deleted": 0, "pending_abandoned": 0,
                   "control_deleted": 0, "outbox_deleted": 0,
                   "alert_deleted": 0}
    assert all(isinstance(v, int) for v in out.values())


def test_record_latency_prunes_at_most_hourly(tmp_path, monkeypatch):
    db = _db(tmp_path)
    now = [1_000_000.0]
    monkeypatch.setattr(local_db, "time",
                        types.SimpleNamespace(time=lambda: now[0]))
    calls = []
    real_prune = db.prune_control_tables

    def counting_prune(*args, **kwargs):
        calls.append(1)
        return real_prune(*args, **kwargs)

    monkeypatch.setattr(db, "prune_control_tables", counting_prune)
    assert db.record_latency("poll", 1.0) is True
    assert db.record_latency("poll", 2.0) is True
    assert len(calls) == 1
    now[0] += 3601.0
    assert db.record_latency("poll", 3.0) is True
    assert len(calls) == 2
