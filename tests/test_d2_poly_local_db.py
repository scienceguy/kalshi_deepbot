"""D2: Polymarket trader local DB parity.

Acceptance (AGENT_TASKS_v7.md §D2):
- a full simulated cycle writes orders, fills, settlement and ledger rows in
  one transaction;
- killing the process mid-cycle (simulated) never leaves a position without
  its order row;
- the legacy importer run twice yields identical row counts;
- sync marks rows synced only after a successful upsert.
"""
import json

import pytest

import config
import local_db


def _db(tmp_path):
    return local_db.PolyLocalDB(
        db_path=str(tmp_path / "poly.db"), mode="dry", auto_sync=False)


def _cycle_rows():
    return {
        "poly_orders": {
            "id": "o-1", "ts": "2026-11-17T00:00:00+00:00", "slug": "slug-a",
            "side": "yes", "action": "buy", "qty": 5, "price": 0.60,
            "cost": 3.0, "fee": 0.0, "status": "resting"},
        "poly_fills": {
            "id": "f-1", "ts": "2026-11-17T00:00:00+00:00", "slug": "slug-a",
            "side": "yes", "action": "buy", "qty": 5, "price": 0.60,
            "fee": 0.0, "order_id": "o-1"},
        "poly_settlements": {
            "id": "s-1", "ts": "2026-11-17T00:01:00+00:00", "slug": "slug-a",
            "side": "yes", "qty": 5, "payout": 5.0, "pnl": 2.0, "outcome": 1},
        "poly_ledger": {
            "id": "l-1", "ts": "2026-11-17T00:00:00+00:00", "event": "open",
            "slug": "slug-a", "payload_json": "{}", "mode": "dry"},
        "poly_positions": {
            "id": "slug-a|yes", "slug": "slug-a", "side": "yes", "qty": 5,
            "cost": 3.0, "price": 0.60, "mode": "dry"},
    }


def test_full_cycle_writes_orders_fills_settlement_ledger_in_one_transaction(tmp_path):
    db = _db(tmp_path)
    counts = db.apply_poly_cycle(_cycle_rows())
    assert counts == {t: 1 for t in _cycle_rows()}, counts
    for table in ("poly_orders", "poly_fills", "poly_settlements",
                  "poly_ledger", "poly_positions"):
        assert db.table_count(table) == 1, table


def test_midcycle_kill_rolls_back_every_table(tmp_path):
    """Simulated process death: a bad column aborts the cycle and no table
    keeps a partial write — a position can never be left without its order."""
    db = _db(tmp_path)
    rows = _cycle_rows()
    rows["poly_settlements"]["bogus_col"] = 1  # kills the transaction
    try:
        db.apply_poly_cycle(rows)
        raise AssertionError("apply_poly_cycle should have raised")
    except Exception:
        pass
    for table in db.SYNC_TABLES:
        assert db.table_count(table) == 0, f"{table} kept rows after rollback"


def _legacy_fixtures(tmp_path):
    state = {
        "cash": 35.0, "start_balance": 35.0,
        "epoch_started_at": "2026-11-16T00:00:00+00:00",
        "positions": [{"slug": "slug-a", "side": "yes", "qty": 5,
                       "cost": 3.0, "price": 0.60}],
    }
    (tmp_path / "state.json").write_text(json.dumps(state))
    ledger = [
        {"event": "open", "timestamp": "t1", "slug": "slug-a", "side": "yes",
         "qty": 5, "price": 0.60, "cost": 3.0, "fee": 0.0},
        {"event": "rotate_exit", "timestamp": "t2", "slug": "slug-a",
         "side": "yes", "qty": 5, "exit_price": 0.70, "payout": 3.5,
         "fee": 0.0},
        {"event": "milestone", "timestamp": "t3", "slug": "",
         "cash": 36.2},
    ]
    (tmp_path / "ledger.jsonl").write_text(
        "\n".join(json.dumps(r) for r in ledger) + "\n")
    calib = [{"ts": "c1", "slug": "slug-a", "outcome": 1},
             {"ts": "c2", "slug": "slug-b", "outcome": 0}]
    (tmp_path / "calib.jsonl").write_text(
        "\n".join(json.dumps(r) for r in calib) + "\n")
    return (str(tmp_path / "state.json"), str(tmp_path / "ledger.jsonl"),
            str(tmp_path / "calib.jsonl"))


def test_importer_run_twice_yields_identical_counts(tmp_path):
    db = _db(tmp_path)
    state_f, ledger_f, calib_f = _legacy_fixtures(tmp_path)

    first = db.import_legacy(state_f, ledger_f, calib_f)
    second = db.import_legacy(state_f, ledger_f, calib_f)
    assert first == second, (first, second)
    assert first["poly_positions"] == 1
    assert first["poly_epochs"] == 1
    assert first["poly_ledger"] == 3
    assert first["poly_orders"] == 2  # open + rotate_exit
    assert first["poly_fills"] == 2
    assert first["poly_calibration"] == 2


def test_sync_marks_rows_synced_only_after_successful_upsert(tmp_path, monkeypatch):
    monkeypatch.setattr(local_db, "supabase_is_enabled", lambda: True)
    db = _db(tmp_path)
    db.apply_poly_cycle({"poly_orders": _cycle_rows()["poly_orders"]})

    class FakeClient:
        def __init__(self, ok):
            self.ok = ok
            self.calls = 0

        def upsert_table(self, table, row):
            self.calls += 1
            return self.ok

    ok_client = FakeClient(True)
    res = db.sync_to_supabase(supabase_client=ok_client)
    assert res["poly_orders"] == 1
    assert db.unsynced_count("poly_orders") == 0, "successful upsert must mark synced"

    bad_db = _db(tmp_path)
    bad_db.apply_poly_cycle({"poly_orders": _cycle_rows()["poly_orders"]})
    fail_client = FakeClient(False)
    res = bad_db.sync_to_supabase(supabase_client=fail_client)
    assert res["poly_orders"] == 0
    assert bad_db.unsynced_count("poly_orders") == 1, "failed upsert must stay unsynced"


def test_mirror_ledger_open_maps_to_order_and_fill(tmp_path):
    db = _db(tmp_path)
    ok = db.mirror_ledger({
        "event": "open", "timestamp": "t9", "slug": "slug-b", "side": "no",
        "qty": 10, "price": 0.40, "cost": 4.0, "fee": 0.0})
    assert ok is True
    assert db.table_count("poly_ledger") == 1
    assert db.table_count("poly_orders") == 1
    assert db.table_count("poly_fills") == 1
    conn = db._get_conn()  # noqa: SLF001 - test reaches the same handle
    order = conn.execute("SELECT action, status, qty FROM poly_orders").fetchone()
    assert order["action"] == "buy" and order["status"] == "filled"
    assert order["qty"] == 10


def test_record_fill_marks_order_filled_idempotently(tmp_path):
    db = _db(tmp_path)
    db.record_order({"id": "o-7", "ts": "t", "slug": "slug-c", "side": "yes",
                     "action": "buy", "qty": 2, "price": 0.5, "status": "resting"})
    assert db.record_fill({"order_id": "o-7", "slug": "slug-c", "side": "yes",
                           "action": "buy", "qty": 2, "price": 0.5, "fee": 0.0,
                           "ts": "t2"}) is True
    db.record_fill({"order_id": "o-7", "slug": "slug-c", "side": "yes",
                    "action": "buy", "qty": 2, "price": 0.5, "fee": 0.0,
                    "ts": "t2"})  # replay: same deterministic fill id
    conn = db._get_conn()  # noqa: SLF001
    assert conn.execute("SELECT COUNT(*) FROM poly_fills").fetchone()[0] == 1
    status = conn.execute("SELECT status FROM poly_orders WHERE id = 'o-7'").fetchone()
    assert status["status"] == "filled"


def test_record_fill_unified_distinguishes_dry_and_live_modes(tmp_path, monkeypatch):
    db = local_db.LocalDB(db_path=str(tmp_path / "fill.db"))
    base = {"ticker": "KXBTC15M-X", "side": "no", "price_cents": 95, "qty": 1.5}
    monkeypatch.setattr(config, "DRY_RUN", True)
    assert db.record_fill_unified({**base, "order_id": "dry-order"}, venue="kalshi")
    monkeypatch.setattr(config, "DRY_RUN", False)
    assert db.record_fill_unified({**base, "order_id": "live-order"}, venue="kalshi")
    rows = db._get_conn().execute("SELECT order_id, mode, qty FROM fills ORDER BY order_id").fetchall()
    assert [tuple(row) for row in rows] == [("dry-order", "dry", 1.5), ("live-order", "live", 1.5)]


@pytest.mark.parametrize("mode", ["dry", "live"])
def test_poly_local_db_unified_fill_uses_shared_schema_and_mode(tmp_path, mode):
    db = local_db.PolyLocalDB(db_path=str(tmp_path / "poly-unified.db"), mode=mode, auto_sync=False)
    assert db.record_fill_unified({
        "ticker": "poly-market", "side": "no", "price_cents": 95,
        "qty": 1.5, "order_id": f"{mode}-order", "raw": {"source": mode},
    }, venue="polymarket_us")
    row = db._get_conn().execute(
        "SELECT mode, qty, raw_json FROM fills WHERE order_id = ?", (f"{mode}-order",)).fetchone()
    assert row["mode"] == mode
    assert row["qty"] == 1.5
    assert json.loads(row["raw_json"]) == {"source": mode}
    assert db._get_conn().execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'orders'").fetchone()[0] == 0


@pytest.mark.parametrize("qty", [0.5, 1.5])
def test_record_fill_unified_preserves_fractional_quantity(tmp_path, qty):
    db = local_db.LocalDB(db_path=str(tmp_path / "fill.db"))
    assert db.record_fill_unified({
        "ticker": "KXBTC15M-X", "side": "no", "price_cents": 95,
        "qty": qty, "order_id": "fractional-order",
    }, venue="kalshi")
    row = db._get_conn().execute("SELECT qty, typeof(qty) AS storage_type FROM fills").fetchone()
    assert row["qty"] == qty
    assert row["storage_type"] == "real"


def test_record_fill_unified_idempotent(tmp_path):
    db = local_db.LocalDB(db_path=str(tmp_path / "fill.db"))
    fill = {"ticker": "KXBTC15M-X", "side": "yes", "price_cents": 50,
            "qty": 1.5, "order_id": "same-order", "raw": {"source": "test"}}
    assert db.record_fill_unified(fill, venue="kalshi")
    assert db.record_fill_unified(fill, venue="kalshi")
    assert db._get_conn().execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
