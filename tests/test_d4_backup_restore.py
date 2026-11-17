"""D4: online backups + integrity checks + restore-from-backup.

Acceptance (AGENT_TASKS_v7.md §D4):
- a backup taken during concurrent writes opens cleanly and passes
  PRAGMA integrity_check;
- a deliberately corrupted DB triggers restore-from-backup plus a critical
  event;
- the restore drill is documented (docs/vps-runbook.md) and exercised
  here against temp dirs.
"""
import os
import sqlite3
import threading
import time
from pathlib import Path

import local_db

REPO_ROOT = Path(__file__).resolve().parent.parent


def _orders_conn(db):
    return db._get_conn()  # noqa: SLF001 - test reaches the same handle


def _insert_order(db, order_id):
    conn = _orders_conn(db)
    if hasattr(db, "mode"):  # PolyLocalDB: its own table set
        conn.execute(
            "INSERT INTO poly_orders (id, ts, slug, side, action, qty, price, "
            "status) VALUES (?, datetime('now'), 'slug-a', 'yes', 'buy', 1, 0.9, "
            "'filled')", (order_id,))
        conn.commit()
        return
    conn.execute(
        "INSERT INTO orders (ts, ticker, side, contracts, price_cents, "
        "order_id, status) VALUES (datetime('now'), 'KXBTC15M', 'yes', 1, "
        "90, ?, 'filled')", (order_id,))
    conn.commit()


def test_backup_during_concurrent_writes_passes_integrity(tmp_path):
    db = local_db.LocalDB(db_path=str(tmp_path / "t.db"), auto_sync=False,
                          backup_dir=str(tmp_path / "backups"))
    _insert_order(db, "seed")
    stop = threading.Event()

    def writer():
        conn = sqlite3.connect(str(tmp_path / "t.db"), timeout=30)
        i = 0
        while not stop.is_set():
            try:
                conn.execute(
                    "INSERT INTO orders (ts, ticker, side, contracts, "
                    "price_cents, order_id, status) VALUES (datetime('now'), "
                    "'KXBTC15M', 'yes', 1, 90, ?, 'filled')", (f"live-{i}",))
                conn.commit()
                i += 1
                time.sleep(0.002)
            except Exception:
                pass
        conn.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        time.sleep(0.3)  # let the writer get going
        path = db.backup_to()
    finally:
        stop.set()
        t.join(timeout=5)
    assert path is not None, "backup failed during concurrent writes"
    probe = sqlite3.connect(path)
    try:
        result = probe.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        probe.close()
    assert str(result).lower() == "ok"


def _corrupt_and_restore(tmp_path, monkeypatch, db_class=local_db.LocalDB, **kwargs):
    backup_dir = tmp_path / "backups"
    db_path = kwargs.pop("db_path", str(tmp_path / "t.db"))
    db = db_class(db_path=db_path, auto_sync=False,
                  backup_dir=str(backup_dir), **kwargs)
    # Force WAL content into the main file, then back up.
    conn = db._get_conn()  # noqa: SLF001
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _insert_order(db, "oid-keep")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    backup = db.backup_to()
    assert backup is not None
    if getattr(db._local, "conn", None) is not None:  # noqa: SLF001
        db._local.conn.close()  # noqa: SLF001
        db._local.conn = None  # noqa: SLF001
    # Deliberately corrupt the main file; drop sidecars so SQLite can't
    # replay a good WAL over the damage.
    raw = bytearray(Path(db_path).read_bytes())
    for i in range(min(4096, len(raw))):
        raw[i] = 0xAB
    Path(db_path).write_bytes(bytes(raw))
    for ext in ("-wal", "-shm"):
        side = db_path + ext
        if os.path.exists(side):
            os.remove(side)

    events = []
    monkeypatch.setattr(
        "emit.emit_event",
        lambda source, event_type, payload=None, **kw: events.append(
            {"source": source, "event_type": event_type, "payload": payload,
             "severity": kw.get("severity")}))

    db2 = db_class(db_path=db_path, auto_sync=False,
                   backup_dir=str(backup_dir), **kwargs)
    return db2, events


def test_corrupted_db_restore_emits_critical_and_recovers_rows(tmp_path, monkeypatch):
    db2, events = _corrupt_and_restore(tmp_path, monkeypatch)
    rows = [r[0] for r in db2._get_conn().execute(  # noqa: SLF001
        "SELECT order_id FROM orders").fetchall()]
    assert rows == ["oid-keep"], f"restore lost rows: {rows}"
    critical = [e for e in events if e["severity"] == "critical"]
    assert critical, f"no critical event emitted: {events}"
    assert any(e["event_type"] == "db.integrity_failed"
               and e["payload"]["status"] == "restored" for e in critical), critical


def test_corrupted_poly_db_restore(tmp_path, monkeypatch):
    db2, events = _corrupt_and_restore(
        tmp_path, monkeypatch, db_class=local_db.PolyLocalDB,
        db_path=str(tmp_path / "poly.db"), mode="dry")
    assert any(e["event_type"] == "db.integrity_failed" for e in events), events
    rows = [r[0] for r in db2._get_conn().execute(  # noqa: SLF001
        "SELECT id FROM poly_orders").fetchall()]
    assert rows == ["oid-keep"], f"restore lost rows: {rows}"
    ok, _ = db2.integrity_check()
    assert ok


def test_subtly_corrupt_db_restored_via_startup_integrity_gate(tmp_path, monkeypatch):
    """DB opens fine but fails PRAGMA integrity_check -> restore at startup."""
    backup_dir = tmp_path / "backups"
    db = local_db.LocalDB(db_path=str(tmp_path / "t.db"), auto_sync=False,
                          backup_dir=str(backup_dir))
    _insert_order(db, "oid-keep")
    db.backup_to()
    db._local.conn.close()  # noqa: SLF001 - release for the swap
    db._local.conn = None  # noqa: SLF001

    events = []
    monkeypatch.setattr(
        "emit.emit_event",
        lambda source, event_type, payload=None, **kw: events.append(
            {"event_type": event_type, "payload": payload,
             "severity": kw.get("severity")}))
    # Force the integrity gate (not the open/create path) to detect failure.
    monkeypatch.setattr(local_db.LocalDB, "integrity_check",
                        lambda self, read_only=True: (False, "page 3 is corrupt"))
    db2 = local_db.LocalDB(db_path=str(tmp_path / "t.db"), auto_sync=False,
                           backup_dir=str(backup_dir))
    assert any(e["event_type"] == "db.integrity_failed"
               and e["payload"]["status"] == "restored" for e in events), events
    rows = [r[0] for r in db2._get_conn().execute(  # noqa: SLF001
        "SELECT order_id FROM orders").fetchall()]
    assert rows == ["oid-keep"]


def test_unrecoverable_corrupt_db_emits_critical_and_recreates(tmp_path, monkeypatch):
    (tmp_path / "t.db").write_bytes(b"\xab" * 8192)
    events = []
    monkeypatch.setattr(
        "emit.emit_event",
        lambda source, event_type, payload=None, **kw: events.append(
            {"event_type": event_type, "payload": payload,
             "severity": kw.get("severity")}))
    db = local_db.LocalDB(db_path=str(tmp_path / "t.db"), auto_sync=False,
                          backup_dir=str(tmp_path / "backups"))
    assert db is not None  # recreated empty, trader must start
    assert any(e["severity"] == "critical" for e in events), events


def test_prune_keeps_newest_backups(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    db_path = str(tmp_path / "t.db")
    for stamp in ("20261114000000000000", "20261115000000000000",
                  "20261116000000000000", "20261117000000000000"):
        (backup_dir / f"t.db.{stamp}.bak").write_bytes(b"x")
    db = local_db.LocalDB(db_path=db_path, auto_sync=False,
                          backup_dir=str(backup_dir))
    db.prune_backups(keep=2)
    remaining = sorted(p.name for p in backup_dir.glob("t.db.*.bak"))
    assert remaining == ["t.db.20261116000000000000.bak",
                         "t.db.20261117000000000000.bak"]


def test_nightly_maintenance_time_gates_and_backs_up(tmp_path):
    backup_dir = tmp_path / "backups"
    db = local_db.LocalDB(db_path=str(tmp_path / "t.db"), auto_sync=False,
                          backup_dir=str(backup_dir))
    _insert_order(db, "oid-1")
    first = db.maybe_nightly_maintenance()
    assert first["checked"] is True
    assert first.get("backup"), first
    second = db.maybe_nightly_maintenance()
    assert second == {"skipped": True}


def test_restore_drill_documented_in_runbook():
    text = (REPO_ROOT / "docs" / "vps-runbook.md").read_text()
    assert "## Restore drill (SQLite DBs)" in text
    assert "integrity_check" in text
    assert "test_d4_backup_restore.py" in text


def test_integrity_check_opens_read_only(tmp_path, monkeypatch):
    """integrity_check opens read-only (mode=ro) to avoid lock contention
    with active writers in WAL mode. This fixes the false corruption issue
    where nightly maintenance would fail integrity_check due to lock
    contention with the main trading thread, creating 436+ .corrupt.* files
    (2026-11-17). Read-only connections don't block or get blocked by
    writers in WAL mode."""
    db = local_db.LocalDB(db_path=str(tmp_path / "t.db"), auto_sync=False)
    ok, msg = db.integrity_check()
    assert ok, msg

    calls = []
    real_connect = local_db.sqlite3.connect

    def spy(database, *args, **kwargs):
        calls.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(local_db.sqlite3, "connect", spy)
    ok, msg = db.integrity_check()
    assert ok, msg
    assert calls
    assert any("mode=ro" in c for c in calls)
