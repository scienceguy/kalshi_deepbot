"""Unified-fill schema and idempotent legacy migration tests."""
import sqlite3

from local_db import LocalDB


def _columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def test_required_unified_fill_schema(tmp_path):
    path = tmp_path / "migration.db"
    LocalDB(db_path=str(path))
    conn = sqlite3.connect(path)
    columns = _columns(conn, "fills")
    assert "raw_json" in columns
    assert "mode" in columns
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


def test_legacy_unified_fills_gain_raw_json_and_mode_without_data_loss(tmp_path):
    path = tmp_path / "legacy-fills.db"
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
    conn.execute("INSERT INTO fills (id, ts, venue, ticker, side, price_cents, qty, order_id) "
                 "VALUES ('old', '2026-01-01', 'kalshi', 'KXBTC15M-X', 'no', 95, 1.5, 'old-order')")
    conn.commit()
    conn.close()

    LocalDB(db_path=str(path))
    LocalDB(db_path=str(path))

    conn = sqlite3.connect(path)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(fills)")]
    assert columns.count("raw_json") == 1
    assert columns.count("mode") == 1
    assert conn.execute("SELECT qty FROM fills WHERE id = 'old'").fetchone()[0] == 1.5
    assert conn.execute("SELECT raw_json FROM fills WHERE id = 'old'").fetchone()[0] == "{}"
    assert conn.execute("SELECT mode FROM fills WHERE id = 'old'").fetchone()[0] == ""
    conn.close()
