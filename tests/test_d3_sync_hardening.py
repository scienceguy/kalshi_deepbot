"""D3: sync hardening — persisted retry/backoff/quarantine + lag metrics.

Acceptance (AGENT_TASKS_v7.md §D3):
- one permanently failing row does not stop other rows syncing and ends in
  quarantine after N attempts;
- after a simulated 10-minute outage the backlog drains in order;
- --sync-status prints the lag fields.
"""
import inspect

import local_db


def _db(tmp_path):
    return local_db.LocalDB(db_path=str(tmp_path / "sync.db"), auto_sync=False)


def _insert_order(db, order_id, age_seconds):
    conn = db._get_conn()  # noqa: SLF001
    conn.execute(
        "INSERT INTO orders (ts, ticker, side, contracts, price_cents, order_id, "
        "status, created_at) VALUES (datetime('now', ?), ?, 'yes', 1, 90, ?, 'filled', "
        "datetime('now', ?))",
        (f"-{age_seconds} seconds", f"T-{order_id}", order_id, f"-{age_seconds} seconds"))
    conn.commit()


class FakeClient:
    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.synced = []

    def insert_order(self, row):
        oid = row.get("order_id")
        if oid in self.fail_ids:
            return False
        self.synced.append(oid)
        return True

    def set_bot_state(self, key, value):
        return True


def test_poison_row_quarantined_others_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(local_db, "SYNC_MAX_ROW_ATTEMPTS", 3)
    monkeypatch.setattr(local_db, "supabase_is_enabled", lambda: True)
    db = _db(tmp_path)
    _insert_order(db, "good-1", 300)
    _insert_order(db, "poison", 200)
    _insert_order(db, "good-2", 100)

    fake = FakeClient(fail_ids={"poison"})
    conn = db._get_conn()  # noqa: SLF001
    for _ in range(3):
        db.sync_to_supabase(supabase_client=fake)
        # production spaces retries by wall clock; tests rewind instead
        conn.execute("UPDATE orders SET next_attempt_at = NULL "
                     "WHERE order_id = 'poison'")
        conn.commit()

    assert sorted(fake.synced) == ["good-1", "good-2"], fake.synced
    row = conn.execute("SELECT pk, attempts, last_error, row_json FROM sync_quarantine").fetchone()
    assert row is not None
    assert int(row["pk"]) == 2  # the integer orders.id of the poison row
    assert "poison" in row["row_json"]
    assert row["attempts"] == 3
    assert row["last_error"]
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE order_id = 'poison'").fetchone()[0] == 0
    assert db.quarantine_count() == 1


def test_failing_row_skipped_while_in_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(local_db, "SYNC_MAX_ROW_ATTEMPTS", 5)
    monkeypatch.setattr(local_db, "supabase_is_enabled", lambda: True)
    db = _db(tmp_path)
    _insert_order(db, "poison", 100)

    fake = FakeClient(fail_ids={"poison"})
    db.sync_to_supabase(supabase_client=fake)
    # next_attempt_at is in the future: not retried inside the backoff window
    db.sync_to_supabase(supabase_client=fake)
    assert fake.synced == []
    conn = db._get_conn()  # noqa: SLF001
    assert conn.execute(
        "SELECT sync_attempts FROM orders WHERE order_id = 'poison'").fetchone()["sync_attempts"] == 1


def test_backlog_drains_in_created_order_after_outage(tmp_path, monkeypatch):
    monkeypatch.setattr(local_db, "supabase_is_enabled", lambda: True)
    monkeypatch.setattr(local_db, "SYNC_BREAKER_MAX_FAILURES", 1000)
    db = _db(tmp_path)
    for i, age in enumerate([600, 480, 360, 240, 120]):  # 10-minute-ago backlog
        _insert_order(db, f"o-{i}", age)

    out = FakeClient(fail_ids={"o-0", "o-1", "o-2", "o-3", "o-4"})  # outage
    db.sync_to_supabase(supabase_client=out)
    conn = db._get_conn()  # noqa: SLF001
    conn.execute("UPDATE orders SET next_attempt_at = NULL")  # outage ends
    conn.commit()

    up = FakeClient()  # healthy remote again
    db.sync_to_supabase(supabase_client=up)
    assert up.synced == ["o-0", "o-1", "o-2", "o-3", "o-4"], up.synced
    assert db.get_sync_lag_stats()["unsynced_count"] == 0


def test_get_sync_lag_stats_exposes_lag_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(local_db, "supabase_is_enabled", lambda: True)
    db = _db(tmp_path)
    _insert_order(db, "o-1", 600)
    stats = db.get_sync_lag_stats()
    assert set(stats) >= {"unsynced_count", "oldest_unsynced_age_s",
                          "last_success_ts", "quarantined"}
    assert stats["unsynced_count"] == 1
    assert stats["oldest_unsynced_age_s"] > 0
    assert stats["quarantined"] == 0

    db.sync_to_supabase(supabase_client=FakeClient())
    stats = db.get_sync_lag_stats()
    assert stats["unsynced_count"] == 0
    assert stats["last_success_ts"] > 0


def test_kalshi_trader_sync_status_flag_parses():
    import trader
    args = trader.parse_args(["--sync-status"])
    assert args.sync_status is True


def test_poly_trader_exposes_sync_status_flag():
    import poly_us_trader
    src = inspect.getsource(poly_us_trader)
    assert '"--sync-status"' in src
