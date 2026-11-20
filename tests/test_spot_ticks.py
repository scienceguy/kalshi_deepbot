"""T13-12 — spot consensus tick store: spot_ticks table, throttled recorder,
and retention prune."""

import time

import local_db
import spot_consensus
from spot_consensus import SpotConsensusProvider, VenuePrice


def _db(tmp_path):
    return local_db.LocalDB(db_path=str(tmp_path / "t.db"), auto_sync=False)


def test_record_spot_tick_persists_row(tmp_path):
    db = _db(tmp_path)
    assert db.record_spot_tick("BTC", "binance", 62000.5, bid=62000.0, ask=62001.0, ts=1700000000)
    conn = db._get_conn()
    row = conn.execute(
        "SELECT ts, symbol, venue, price, bid, ask FROM spot_ticks").fetchone()
    assert tuple(row) == (1700000000, "BTC", "binance", 62000.5, 62000.0, 62001.0)


def test_record_spot_tick_defaults_ts(tmp_path):
    db = _db(tmp_path)
    before = int(time.time())
    assert db.record_spot_tick("BTC", "coinbase", 62000.0)
    row = db._get_conn().execute("SELECT ts FROM spot_ticks").fetchone()
    assert before <= tuple(row)[0] <= int(time.time()) + 1


def test_prune_deletes_old_ticks_only(tmp_path):
    db = _db(tmp_path)
    now = int(time.time())
    db.record_spot_tick("BTC", "binance", 1.0, ts=now - 10 * 86400)   # 10d old
    db.record_spot_tick("BTC", "binance", 2.0, ts=now - 3600)         # 1h old
    res = db.prune_control_tables(tick_keep_days=3)
    assert res["tick_deleted"] == 1
    rows = db._get_conn().execute("SELECT price FROM spot_ticks").fetchall()
    assert [tuple(r) for r in rows] == [(2.0,)]


def _consensus():
    return spot_consensus.compute_spot_consensus(
        {"binance": VenuePrice("binance", 62000.0, time.monotonic()),
         "coinbase": VenuePrice("coinbase", 62001.0, time.monotonic())})


def test_recorder_throttled_and_best_effort(monkeypatch):
    calls = []

    class FakeDB:
        def record_spot_tick(self, symbol, venue, price, bid, ask):
            calls.append((symbol, venue, price))
            return True

    monkeypatch.setattr(local_db, "get_local_db", lambda: FakeDB())
    monkeypatch.setattr(spot_consensus, "_TICK_MIN_INTERVAL_S", 1.0)
    monkeypatch.setattr(spot_consensus, "_last_tick_recorded", 0.0)

    provider = SpotConsensusProvider()
    provider.register_venue("binance", lambda s: (62000.0, time.monotonic()))
    provider.register_venue("coinbase", lambda s: (62001.0, time.monotonic()))

    c1 = provider.get_consensus("BTC")
    assert c1 is not None
    assert len(calls) == 2  # one row per venue, first pass
    provider.get_consensus("BTC")  # within the interval -> no new writes
    assert len(calls) == 2
    # after the interval elapses, ticks record again
    monkeypatch.setattr(spot_consensus, "_last_tick_recorded", time.monotonic() - 2.0)
    provider.get_consensus("BTC")
    assert len(calls) == 4

    # a failing DB must never raise into the hot path
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(local_db, "get_local_db", boom)
    monkeypatch.setattr(spot_consensus, "_last_tick_recorded", 0.0)
    assert provider.get_consensus("BTC") is not None
