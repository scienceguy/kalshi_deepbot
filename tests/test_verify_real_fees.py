import json
import sqlite3

import fees
from local_db import LocalDB
from scripts import verify_real_fees


def _legacy_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE fills (
            id TEXT PRIMARY KEY, ts TEXT NOT NULL, venue TEXT NOT NULL,
            ticker TEXT NOT NULL, side TEXT NOT NULL,
            price_cents INTEGER NOT NULL, qty INTEGER NOT NULL,
            fee_dollars REAL NOT NULL DEFAULT 0, order_id TEXT NOT NULL,
            maker_taker TEXT NOT NULL DEFAULT 'taker', settled INTEGER DEFAULT 0,
            settlement_price_cents INTEGER, settlement_pnl_dollars REAL,
            synced INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()
    return LocalDB(db_path=str(path))


def test_verify_real_fees_legacy_schema_is_idempotent_over_two_runs(
        tmp_path, monkeypatch):
    db = _legacy_db(tmp_path / "legacy.db")
    price = 0.95
    quantity = 1.5
    actual = fees.taker_fee_dollars(quantity, price, series="KXBTC15M")
    fill = {
        "created_time": "2026-11-27T00:00:00Z", "order_id": "venue-order",
        "ticker": "KXBTC15M-X", "side": "no", "price": price,
        "count_fp": quantity, "is_taker": True, "fee_dollars": actual,
    }

    class FakeClient:
        def _request(self, method, path, params=None):
            return {"fills": [fill]}

    monkeypatch.setattr(verify_real_fees, "KalshiClient", FakeClient)
    monkeypatch.setattr(verify_real_fees, "get_local_db", lambda: db)
    monkeypatch.setattr(verify_real_fees, "Path", lambda value: tmp_path / value)

    assert verify_real_fees.main() == 0
    assert verify_real_fees.main() == 0

    conn = db._get_conn()
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    row = conn.execute(
        "SELECT qty, maker_taker, raw_json FROM fills WHERE order_id = 'venue-order'"
    ).fetchone()
    assert row["qty"] == 1.5
    assert row["maker_taker"] == "taker"
    raw = json.loads(row["raw_json"])
    assert raw["order_id"] == "venue-order"
    assert raw["model_fee_dollars"] == actual
    assert raw["actual_fee_dollars"] == actual
