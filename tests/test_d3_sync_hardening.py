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
    # Kept locally with synced = -1 (never DELETEd — this is the audit row).
    kept = conn.execute(
        "SELECT synced FROM orders WHERE order_id = 'poison'").fetchone()
    assert kept is not None and kept["synced"] == -1
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


class _RecordingClient:
    """Records the payload each insert receives."""

    def __init__(self, ok=True):
        self.payloads = []
        self.ok = ok

    def insert_signal(self, data):
        self.payloads.append(("signals", dict(data)))
        return self.ok

    def insert_pnl_ledger(self, data):
        self.payloads.append(("pnl_ledger", dict(data)))
        return self.ok


def test_sync_posts_only_remote_columns_and_keeps_a_poison_row(tmp_path, monkeypatch):
    """A signal row carrying the local-only D1/D3 columns must sync, not be
    quarantined-and-deleted: posting those columns made every upsert fail."""
    monkeypatch.setattr(local_db, "supabase_is_enabled", lambda: True)
    db = _db(tmp_path)
    conn = db._get_conn()  # noqa: SLF001
    conn.execute(
        "INSERT INTO signals (ts, ticker, side, price_cents, contracts, venue, mode, "
        "origin_host, schema_version) VALUES (datetime('now'), 'KXBTC15M-X', 'yes', "
        "92, 3, 'kalshi', 'dry', 'host', '1')")
    conn.commit()

    fake = _RecordingClient(ok=True)
    assert db._sync_table("signals", fake) == 1  # noqa: SLF001
    (table, payload), = fake.payloads
    assert table == "signals"
    local_only = {"id", "synced", "created_at", "updated_at", "venue", "mode",
                  "origin_host", "schema_version", "sync_attempts",
                  "last_error", "next_attempt_at"}
    assert not (set(payload) & local_only), sorted(set(payload) & local_only)
    assert {"ts", "ticker", "side", "price_cents", "contracts"} <= set(payload)
    assert conn.execute("SELECT synced FROM signals").fetchone()[0] == 1

    # A permanently failing row stays in the table (synced = -1), never deleted.
    monkeypatch.setattr(local_db, "SYNC_MAX_ROW_ATTEMPTS", 2)
    conn.execute("INSERT INTO signals (ts, ticker, side, price_cents, contracts) "
                 "VALUES (datetime('now'), 'KXBTC15M-Y', 'no', 8, 2)")
    conn.commit()
    bad = _RecordingClient(ok=False)
    for _ in range(3):
        db._sync_table("signals", bad)  # noqa: SLF001
        conn.execute("UPDATE signals SET next_attempt_at = NULL "
                     "WHERE ticker = 'KXBTC15M-Y'")
        conn.commit()
    row = conn.execute(
        "SELECT synced FROM signals WHERE ticker = 'KXBTC15M-Y'").fetchone()
    assert row is not None, "a poison signal row must not be deleted"
    assert row["synced"] == -1


def test_pnl_ledger_sync_posts_only_remote_columns(tmp_path, monkeypatch):
    """pnl_ledger is the realized-money audit; its remote payload must be the
    remote schema's columns only."""
    monkeypatch.setattr(local_db, "supabase_is_enabled", lambda: True)
    db = _db(tmp_path)
    db.apply_settlement_pnl("k1", "KXBTC15M-X", 1.25, venue="", mode="dry")
    fake = _RecordingClient(ok=True)
    assert db._sync_table("pnl_ledger", fake) == 1  # noqa: SLF001
    (table, payload), = fake.payloads
    assert table == "pnl_ledger"
    assert "venue" not in payload and "mode" not in payload
    assert "idempotency_key" in payload
    assert "realized_pnl_dollars" in payload
