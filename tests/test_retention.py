"""H9-H11: retention policies for unbounded control tables.

Covers prune_control_tables (per-table retention windows, configurable),
the nightly db-prune job wiring in scripts/scheduled_jobs.py, and the
config retention constants.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import config  # noqa: E402
import local_db  # noqa: E402
import scheduled_jobs  # noqa: E402


def _db(tmp_path):
    return local_db.LocalDB(db_path=str(tmp_path / "retention.db"), auto_sync=False)


def _seed_old_rows(db):
    conn = db._get_conn()
    with db._lock:
        conn.execute(
            "INSERT INTO event_outbox (event_id, type, payload_json, status, created_at, delivered_at) "
            "VALUES ('old-sent', 't', '{}', 'sent', datetime('now', '-10 days'), datetime('now', '-10 days'))")
        conn.execute(
            "INSERT INTO event_outbox (event_id, type, payload_json, status, created_at, delivered_at) "
            "VALUES ('old-dead', 't', '{}', 'dead', datetime('now', '-10 days'), '')")
        conn.execute(
            "INSERT INTO event_outbox (event_id, type, payload_json, status, created_at, delivered_at) "
            "VALUES ('new-sent', 't', '{}', 'sent', datetime('now', '-1 days'), datetime('now', '-1 days'))")
        conn.execute(
            "INSERT INTO event_outbox (event_id, type, payload_json, status, created_at, delivered_at) "
            "VALUES ('old-pending', 't', '{}', 'pending', datetime('now', '-10 days'), '')")
        conn.execute(
            "INSERT INTO alert_history (job_name, alert_key, alert_type, payload_json, sent_at) "
            "VALUES ('j', 'old', 't', '{}', datetime('now', '-40 days'))")
        conn.execute(
            "INSERT INTO alert_history (job_name, alert_key, alert_type, payload_json, sent_at) "
            "VALUES ('j', 'new', 't', '{}', datetime('now', '-1 days'))")
        conn.execute(
            "INSERT INTO bus_inbox (message_id, type, status, created_at, delivered_at) "
            "VALUES ('old-deliv', 't', 'delivered', datetime('now', '-10 days'), datetime('now', '-10 days'))")
        conn.execute(
            "INSERT INTO bus_inbox (message_id, type, status, created_at, delivered_at) "
            "VALUES ('new-deliv', 't', 'delivered', datetime('now', '-1 days'), datetime('now', '-1 days'))")
        conn.execute(
            "INSERT INTO bus_inbox (message_id, type, status, created_at, delivered_at) "
            "VALUES ('old-pend', 't', 'pending', datetime('now', '-10 days'), '')")
        for status in ("confirmed", "cancelled", "failed"):
            conn.execute(
                "INSERT INTO pending_send (order_id, ticker, side, price_cents, contracts, "
                "dollar_risk, venue, mode, created_at, status) "
                "VALUES (?, 'T', 'yes', 95, 1, 1.0, 'kalshi', 'dry', datetime('now', '-10 days'), ?)",
                (f"old-{status}", status))
        conn.execute(
            "INSERT INTO pending_send (order_id, ticker, side, price_cents, contracts, "
            "dollar_risk, venue, mode, created_at, status) "
            "VALUES ('new-confirmed', 'T', 'yes', 95, 1, 1.0, 'kalshi', 'dry', datetime('now', '-1 days'), 'confirmed')")
        conn.execute(
            "INSERT INTO pending_send (order_id, ticker, side, price_cents, contracts, "
            "dollar_risk, venue, mode, created_at, status) "
            "VALUES ('old-pending2', 'T', 'yes', 95, 1, 1.0, 'kalshi', 'dry', datetime('now', '-10 days'), 'pending')")
        conn.execute(
            "INSERT INTO latency_measurements (stage, duration_ms, created_at) "
            "VALUES ('poll', 1.0, datetime('now', '-10 days'))")
        conn.execute(
            "INSERT INTO latency_measurements (stage, duration_ms, created_at) "
            "VALUES ('poll', 1.0, datetime('now', '-1 days'))")
        conn.execute(
            "INSERT INTO sync_quarantine (table_name, pk, row_json, quarantined_at) "
            "VALUES ('signals', 'old', '{}', datetime('now', '-40 days'))")
        conn.execute(
            "INSERT INTO sync_quarantine (table_name, pk, row_json, quarantined_at) "
            "VALUES ('signals', 'new', '{}', datetime('now', '-1 days'))")
        conn.commit()


def _count(db, table):
    conn = db._get_conn()
    with db._lock:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_prune_deletes_only_expired_rows(tmp_path):
    db = _db(tmp_path)
    _seed_old_rows(db)
    result = db.prune_control_tables()
    assert result["outbox_deleted"] == 2
    assert result["alert_deleted"] == 1
    assert result["inbox_deleted"] == 1
    assert result["pending_deleted"] == 3
    assert result["latency_deleted"] == 1
    assert result["quarantine_deleted"] == 1
    # survivors
    assert _count(db, "event_outbox") == 2  # new-sent + old-pending
    assert _count(db, "alert_history") == 1
    assert _count(db, "bus_inbox") == 2     # new-deliv + old-pend
    assert _count(db, "pending_send") == 2  # new-confirmed + old-pending2 (abandoned, not deleted)
    assert _count(db, "latency_measurements") == 1
    assert _count(db, "sync_quarantine") == 1


def test_prune_respects_explicit_retention_args(tmp_path):
    db = _db(tmp_path)
    _seed_old_rows(db)
    result = db.prune_control_tables(
        latency_keep_days=365, pending_keep_days=365, inbox_keep_days=365,
        outbox_keep_days=365, alert_keep_days=365, quarantine_keep_days=365)
    assert result["latency_deleted"] == 0
    assert result["pending_deleted"] == 0
    assert result["inbox_deleted"] == 0
    assert result["outbox_deleted"] == 0
    assert result["alert_deleted"] == 0
    assert result["quarantine_deleted"] == 0


def test_prune_never_deletes_pending_intents(tmp_path):
    db = _db(tmp_path)
    _seed_old_rows(db)
    db.prune_control_tables()
    conn = db._get_conn()
    with db._lock:
        row = conn.execute(
            "SELECT status, venue_status FROM pending_send WHERE order_id = 'old-pending2'").fetchone()
    # stale pending intent is abandoned (marked failed), never deleted
    assert row[0] == "failed"
    assert row[1] == "abandoned_stale"


def test_config_retention_constants_exist():
    assert config.RETENTION_EVENT_OUTBOX_DAYS == 7
    assert config.RETENTION_ALERT_HISTORY_DAYS == 28
    assert config.RETENTION_BUS_INBOX_DAYS == 7
    assert config.RETENTION_PENDING_SEND_DAYS == 7
    assert config.RETENTION_LATENCY_MEASUREMENTS_DAYS == 7
    assert config.RETENTION_SYNC_QUARANTINE_DAYS == 30


def test_maybe_prune_defaults_match_config(tmp_path):
    db = _db(tmp_path)
    _seed_old_rows(db)
    db.maybe_prune()
    assert _count(db, "latency_measurements") == 1
    assert _count(db, "sync_quarantine") == 1


def test_nightly_prune_job_registered():
    prune_jobs = [j for j in scheduled_jobs.JOBS if j[0] == "db-prune"]
    assert len(prune_jobs) == 1
    job = prune_jobs[0]
    assert job[1] == ["scripts/prune_control_tables.py"]
    assert job[2] == 86400  # nightly


def test_nightly_prune_job_due_once_per_day():
    job = next(j for j in scheduled_jobs.JOBS if j[0] == "db-prune")
    _, _, every_s, offset_s = job[:4]
    hits = [t for t in range(0, 2 * 86400, 60) if scheduled_jobs.due(t, every_s, offset_s)]
    assert len(hits) == 2  # once per day


def test_prune_script_runs_and_reports(tmp_path, monkeypatch):
    import prune_control_tables
    db = _db(tmp_path)
    _seed_old_rows(db)
    result = prune_control_tables.check(db=db)
    assert result["alert"] is False
    assert result["outbox_deleted"] == 2
    assert result["quarantine_deleted"] == 1
