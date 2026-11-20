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


def test_update_order_settlement_by_order_id(tmp_path):
    db = _db(tmp_path)
    _insert_order(db)
    assert db.update_order_settlement(
        "oid1",
        fill_price_cents=100,
        outcome_won=True,
        pnl_dollars=0.10,
        settled_at="2026-11-12T01:00:00Z",
        ticker="KXBTC15M",
    )
    conn = db._get_conn()  # noqa: SLF001
    row = conn.execute(
        "SELECT status, fill_price_cents, outcome_won, pnl_dollars, settled_at"
        " FROM orders WHERE order_id = 'oid1'"
    ).fetchone()
    assert tuple(row) == ("settled", 100, 1, 0.10, "2026-11-12T01:00:00Z")


def test_update_order_settlement_falls_back_to_ticker(tmp_path):
    db = _db(tmp_path)
    _insert_order(db)
    assert db.update_order_settlement(
        "unknown-id",
        fill_price_cents=0,
        outcome_won=False,
        pnl_dollars=-0.90,
        settled_at="2026-11-12T01:05:00Z",
        ticker="KXBTC15M",
    )
    conn = db._get_conn()  # noqa: SLF001
    row = conn.execute(
        "SELECT status, outcome_won, pnl_dollars FROM orders WHERE ticker = 'KXBTC15M'"
    ).fetchone()
    assert tuple(row) == ("settled", 0, -0.90)


def test_update_order_settlement_unknown_row_is_false(tmp_path):
    db = _db(tmp_path)
    assert not db.update_order_settlement(
        "nope",
        fill_price_cents=100,
        outcome_won=True,
        pnl_dollars=0.1,
        settled_at="2026-11-12T01:00:00Z",
        ticker="NOSUCH",
    )


def test_day0_balance_recorded_once_and_readable(tmp_path):
    """T13-32: the day-0 snapshot is recorded once; a second record with a
    different balance does not overwrite it (first writer wins)."""
    db = _db(tmp_path)
    assert db.get_day0_balance("kalshi") is None
    assert db.record_day0_balance("kalshi", 12.55, "kalshi /portfolio/balance (live)") is True
    # second write is ignored — the day-0 snapshot must never drift
    assert db.record_day0_balance("kalshi", 999.0, "bogus") is False
    row = db.get_day0_balance("kalshi")
    assert row["balance"] == 12.55
    assert row["source"] == "kalshi /portfolio/balance (live)"
    assert row["venue"] == "kalshi"
    assert db.get_day0_balance("polymarket") is None
