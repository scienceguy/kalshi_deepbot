"""Tests for LocalDB → Supabase sync mapping.

The deployed Supabase schema is not identical to the local SQLite schema, so
the sync has to translate: orders need `action` (NOT NULL remotely) and
pnl_ledger has no remote table at all.
"""
import local_db


class _FakeSupabase:
    def __init__(self):
        self.orders = []
        self.pnl = []

    def insert_order(self, data):
        self.orders.append(data)
        return True

    def insert_pnl_ledger(self, data):
        self.pnl.append(data)
        return True

    def insert_signal(self, data):
        return True

    def set_bot_state(self, key, value):
        return True


def _db(tmp_path):
    db = local_db.LocalDB(db_path=str(tmp_path / "t.db"), auto_sync=False)
    return db


def _insert_order(db):
    conn = db._get_conn()  # noqa: SLF001 - test reaches the same handle
    conn.execute(
        "INSERT INTO orders (ts, ticker, side, contracts, price_cents,"
        " order_id, status, fees_dollars, cycles) VALUES"
        " ('2026-11-12T00:00:00Z','KXBTC15M','yes',1,90,'oid1','resting',0.0,0)"
    )
    conn.commit()


def test_orders_sync_sets_action(tmp_path):
    db = _db(tmp_path)
    _insert_order(db)
    fake = _FakeSupabase()
    assert db._sync_table("orders", fake) == 1  # noqa: SLF001
    assert fake.orders and fake.orders[0]["action"] == "buy"


def test_pnl_ledger_sync_is_skipped(tmp_path):
    db = _db(tmp_path)
    fake = _FakeSupabase()
    # no pnl_ledger table exists remotely, so nothing is sent
    assert db._sync_table("pnl_ledger", fake) == 0  # noqa: SLF001
    assert fake.pnl == []
