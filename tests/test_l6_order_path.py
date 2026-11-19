"""
L6 acceptance: order-path efficiency.

Covers the four L6 mechanisms against the spec's acceptance tests:
1. preview_order validates a (slug, side) once per process (shadow mode and
   failures never cached; reset_state clears the cache).
2. A slow ledger write never delays the order hot path (`order_sent` recorded
   before any disk I/O; execute_paper_trade returns while the writer drains).
3. L1 stage vocabulary unchanged; order_sent actually lands in the recorder.
4. Fixed 0.05s sleeps gone from settlement/missing-slug fetch loops.
5. Prewarm + per-slug static memo + bounded ledger queue drop-not-block.
"""

import queue
import time
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("polymarket_us")

import config
import latency
import polymarket_us_client
from poly_us_trader import PolyUSPaperTrader


@pytest.fixture
def temp_trader(tmp_path):
    state_file = tmp_path / "state.json"
    ledger_file = tmp_path / "ledger.jsonl"
    with patch("poly_us_trader.STATE_FILE", state_file), \
         patch("poly_us_trader.LEDGER_FILE", ledger_file):
        trader = PolyUSPaperTrader(initial_cash=35.0, reset=True)
        yield trader


def _venue_preview_ok():
    return patch("polymarket_us_client.preview_order",
                 return_value={"ok": True, "error": None})


def _book_ok():
    book = {"bbo": {"yes_bid": "0.94", "yes_ask": "0.96"}}
    return patch("polymarket_us_client.get_market_book", return_value=book)


def _opp(slug="l6-market"):
    return {"slug": slug, "question": "Q", "side": "yes", "price": 0.95,
            "ref_prob": 0.98}


def _run_paper_buy(trader, slug="l6-market"):
    """One full paper-taker order flow with the venue mocked at the HTTP edge."""
    with _venue_preview_ok(), _book_ok():
        return trader.execute_paper_trade(_opp(slug))


# ── 1. preview-once per (slug, side) ────────────────────────────────

def test_preview_called_once_per_slug_side(temp_trader):
    with _venue_preview_ok() as m:
        assert temp_trader.preview_order("s1", "yes", 0.95, 1) is True
        assert temp_trader.preview_order("s1", "yes", 0.95, 1) is True
        assert m.call_count == 1  # second order on same (slug, side): no venue call


def test_preview_cache_keyed_by_slug_and_side(temp_trader):
    with _venue_preview_ok() as m:
        temp_trader.preview_order("s1", "yes", 0.95, 1)
        temp_trader.preview_order("s1", "no", 0.95, 1)   # other side -> call
        temp_trader.preview_order("s2", "yes", 0.95, 1)  # other slug -> call
        assert m.call_count == 3


def test_preview_shadow_mode_never_caches(temp_trader):
    with patch.object(config, "GATES_SHADOW_MODE", True), _venue_preview_ok() as m:
        temp_trader.preview_order("s1", "yes", 0.95, 1)
        temp_trader.preview_order("s1", "yes", 0.95, 1)
        assert m.call_count == 2  # shadow mode keeps every preview


def test_preview_failure_not_cached(temp_trader):
    with patch("polymarket_us_client.preview_order", return_value=None) as m:
        assert temp_trader.preview_order("s1", "yes", 0.95, 1) is False
        assert temp_trader.preview_order("s1", "yes", 0.95, 1) is False
        assert m.call_count == 2  # failure retries next time


def test_preview_exception_not_cached(temp_trader):
    with patch("polymarket_us_client.preview_order",
               side_effect=RuntimeError("boom")) as m:
        assert temp_trader.preview_order("s1", "yes", 0.95, 1) is False
        assert temp_trader.preview_order("s1", "yes", 0.95, 1) is False
        assert m.call_count == 2


def test_reset_state_clears_preview_cache(temp_trader):
    with _venue_preview_ok() as m:
        temp_trader.preview_order("s1", "yes", 0.95, 1)
        temp_trader.reset_state(35.0)
        temp_trader.preview_order("s1", "yes", 0.95, 1)
        assert m.call_count == 2  # new session re-validates


# ── 2. slow disk cannot delay order_sent ────────────────────────────

def test_slow_ledger_write_does_not_delay_order(temp_trader):
    orig_write = temp_trader._write_ledger_record

    def slow_write(record):
        time.sleep(0.5)  # a disk far slower than the order path tolerates
        orig_write(record)

    temp_trader._write_ledger_record = slow_write
    t0 = time.monotonic()
    ok = _run_paper_buy(temp_trader)
    elapsed = time.monotonic() - t0
    assert ok is True
    assert elapsed < 0.4  # order hot path never waits on the ledger write

    assert temp_trader.flush_ledger(timeout=5.0) is True
    lines = temp_trader.ledger_file.read_text().splitlines()
    assert any('"event": "open"' in line for line in lines)  # nothing lost


def test_order_sent_stage_recorded(temp_trader):
    before = latency.default_recorder.stage_stats().get("order_sent", {}).get("n", 0)
    assert _run_paper_buy(temp_trader) is True
    after = latency.default_recorder.stage_stats().get("order_sent", {}).get("n", 0)
    assert after > before


# ── 3. L1 vocabulary + histograms unchanged ─────────────────────────

def test_l1_stage_vocabulary_unchanged(tmp_path):
    assert set(latency.STAGES) == {
        "tick_recv", "book_update", "decision", "gate_eval", "risk_check",
        "order_build", "order_submit", "order_ack", "fill_confirm",
    }
    # Fresh recorder (tmp log file, never the live latency.jsonl): the
    # histogram API works and order_sent samples land like any other stage.
    rec = latency.LatencyRecorder(log_file=tmp_path / "l.jsonl")
    rec.record("decision", 5.0)
    p = rec.percentiles()
    assert p["n"] == 1 and p["p50_ms"] == 5.0
    rec.record("order_sent", 1.5)
    assert "order_sent" in rec.stage_stats()


# ── 4. fixed sleeps gone from the hot loops ─────────────────────────

def test_check_settlements_has_no_fixed_sleep(temp_trader, tmp_path):
    temp_trader.state["positions"] = [{
        "slug": "l6-settle", "side": "yes", "price": 0.95, "qty": 5, "cost": 4.75,
    }]
    temp_trader.state["cash"] = 95.25
    temp_trader.state["realized_pnl"] = 0.0
    # Hermetic: tmp settle DB — the shared default SQLite is held by the live
    # bot processes on this host ("database is locked"), which would strand the
    # position open and fail the cash assertion for environmental reasons.
    import local_db
    settle_db = local_db.get_local_db(db_path=str(tmp_path / "settle.db"))
    with patch("time.sleep") as mock_sleep, \
         patch("poly_us_trader.get_local_db", return_value=settle_db), \
         patch("polymarket_us_client.get_market_settlement",
               return_value={"slug": "l6-settle", "settlement": 1.0}):
        temp_trader.check_settlements({"l6-settle": {"closed": True}})
    assert temp_trader.state["positions"] == []           # path really ran
    assert temp_trader.state["cash"] == 95.25 + 5.00
    for call in mock_sleep.call_args_list:                # no fixed 0.05s pace
        for arg in call.args:
            assert not (isinstance(arg, (int, float)) and abs(arg - 0.05) < 1e-9)


# ── 5. bounded ledger queue: drop, never block ──────────────────────

def test_ledger_queue_full_drops_loudly(temp_trader):
    temp_trader._ledger_q = queue.Queue(maxsize=1)
    temp_trader._ensure_ledger_thread = lambda: None  # no drain: pure queue test
    temp_trader._ledger_pending = 0
    temp_trader._ledger_dropped = 0
    temp_trader._enqueue_ledger(("ledger", {"event": "open"}))
    temp_trader._enqueue_ledger(("ledger", {"event": "open"}))  # queue is full
    assert temp_trader._ledger_dropped == 1
    assert temp_trader._ledger_pending == 1  # dropped op never counted pending


def test_flush_ledger_drains_writer(temp_trader):
    assert temp_trader.flush_ledger(timeout=1.0) is True
    temp_trader._append_ledger({"event": "open", "slug": "x"})
    assert temp_trader.flush_ledger(timeout=5.0) is True
    assert '"event": "open"' in temp_trader.ledger_file.read_text()


# ── 6. per-slug static memo ─────────────────────────────────────────

def _market(slug, symbol, strike):
    return {
        "slug": slug,
        "assetPriceTerms": {"strike": {"value": strike}, "asset": {"symbol": symbol}},
        "endDate": "2026-11-20T00:00:00Z",
    }


def test_slug_static_memoizes_and_reset_clears(temp_trader):
    e1 = temp_trader._slug_static("s1", _market("s1", "btc", "100"))
    e2 = temp_trader._slug_static("s1", _market("s1", "eth", "999"))
    assert e1 is e2                      # memo hit: same object
    assert e1["symbol"] == "BTC"         # first call's facts win
    e3 = temp_trader._slug_static("s2", _market("s2", "eth", "50"))
    assert e3 is not e1
    temp_trader.reset_state(35.0)
    e4 = temp_trader._slug_static("s1", _market("s1", "eth", "999"))
    assert e4["symbol"] == "ETH"         # reset dropped the memo


def test_slug_static_cache_bounded(temp_trader):
    for i in range(520):
        temp_trader._slug_static(f"s{i}", _market(f"s{i}", "btc", "100"))
    assert len(temp_trader._static_cache) <= 512


# ── 7. HTTP prewarm ─────────────────────────────────────────────────

def test_prewarm_false_without_client():
    with patch("polymarket_us_client._get_client", return_value=None):
        assert polymarket_us_client.prewarm() is False


def test_prewarm_warms_authenticated_read():
    fake = MagicMock()
    with patch("polymarket_us_client._get_client", return_value=fake):
        assert polymarket_us_client.prewarm() is True
    fake.get.assert_called_once_with("/v1/account/balances", authenticated=True)


def test_prewarm_swallows_venue_errors():
    fake = MagicMock()
    fake.get.side_effect = RuntimeError("no network")
    with patch("polymarket_us_client._get_client", return_value=fake):
        assert polymarket_us_client.prewarm() is False
