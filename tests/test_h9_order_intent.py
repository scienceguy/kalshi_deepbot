"""H8/H9: write-ahead order intent and latency measurement in the local DB.

H9: an intent row is written BEFORE the venue call so a crash between the
REST call and fill recording leaves a pending_send row to reconcile.
H8: per-stage latency is recorded and summarised so an event-driven wake
can be judged on measurements, not guesses.
"""
import local_db


def _db(tmp_path):
    return local_db.LocalDB(db_path=str(tmp_path / "t.db"), auto_sync=False)


def test_intent_roundtrip_then_confirm_by_ticker(tmp_path):
    db = _db(tmp_path)
    assert db.record_order_intent(
        "coid-1", "KXBTC15M-1", "yes", 90, 5.0, 4.5, "kalshi", "dry") is True
    pending = db.get_pending_sends()
    assert len(pending) == 1
    assert pending[0]["order_id"] == "coid-1"
    assert pending[0]["ticker"] == "KXBTC15M-1"
    assert pending[0]["side"] == "yes"
    assert db.confirm_order_intent_by_ticker(
        "KXBTC15M-1", "yes", venue_order_id="oid-1", venue_status="executed") is True
    assert db.get_pending_sends() == []


def test_duplicate_order_id_rejected(tmp_path):
    db = _db(tmp_path)
    assert db.record_order_intent(
        "coid-1", "KXBTC15M-1", "yes", 90, 5.0, 4.5, "kalshi", "dry") is True
    assert db.record_order_intent(
        "coid-1", "KXBTC15M-1", "yes", 90, 5.0, 4.5, "kalshi", "dry") is False
    assert len(db.get_pending_sends()) == 1


def test_cancel_intent_clears_pending(tmp_path):
    db = _db(tmp_path)
    db.record_order_intent("coid-2", "KXETH15M-1", "no", 88, 3.0, 2.64, "kalshi", "live")
    assert db.cancel_order_intent("coid-2", venue_status="expired") is True
    assert db.get_pending_sends() == []


def test_confirm_by_ticker_ignores_other_side(tmp_path):
    db = _db(tmp_path)
    db.record_order_intent("coid-3", "KXBTC15M-2", "no", 91, 2.0, 1.82, "kalshi", "dry")
    # wrong side must not resolve the intent — and must not report success
    assert db.confirm_order_intent_by_ticker("KXBTC15M-2", "yes", "oid-x") is False
    assert len(db.get_pending_sends()) == 1
    assert db.confirm_order_intent_by_ticker("KXBTC15M-2", "no", "oid-3") is True
    assert db.get_pending_sends() == []


def test_latency_record_and_summary(tmp_path):
    db = _db(tmp_path)
    for ms in (10.0, 20.0, 30.0):
        assert db.record_latency("market_find", ms, venue="kalshi", mode="dry") is True
    db.record_latency("order_place", 5.0, venue="kalshi", mode="dry")
    stats = {s["stage"]: s for s in db.get_latency_stats()}
    assert stats["market_find"]["count"] == 3
    assert stats["market_find"]["avg_ms"] == 20.0
    assert stats["market_find"]["max_ms"] == 30.0
    assert stats["order_place"]["count"] == 1
    only = db.get_latency_stats(stage="order_place")
    assert len(only) == 1 and only[0]["stage"] == "order_place"