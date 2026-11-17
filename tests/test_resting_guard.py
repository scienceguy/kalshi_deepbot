"""L5 acceptance: sub-second protection of resting orders.

Spec AGENT_TASKS_v7.md L5. Tests: a spot move beyond the threshold cancels a
resting order within one fast-loop tick; a feed outage past X seconds
triggers cancel-all; a pause_entries command cancels resting orders;
cancels are not repeated for already-cancelled orders.
"""
import threading
from unittest.mock import patch

import pytest

import control
import resting_guard
from resting_guard import RestingGuard, TRIGGER_BBO_AWAY, TRIGGER_FEED_STALE, TRIGGER_HALT, TRIGGER_SPOT_MOVE


# ── adverse_move_fraction ────────────────────────────────────────────

@pytest.mark.parametrize("side,above,entry,strike,now,expected", [
    # above-market YES buy: cushion = entry - strike (entry above strike)
    ("yes", True, 100.0, 95.0, 97.0, 0.6),      # spot fell toward strike
    ("yes", True, 100.0, 95.0, 101.0, -0.2),    # in favour
    # below-market YES buy: cushion = strike - entry (entry below strike)
    ("yes", False, 95.0, 100.0, 98.0, 0.6),     # spot rose toward strike
    # above-market NO buy: cushion = strike - entry (entry below strike)
    ("no", True, 92.0, 100.0, 96.0, 0.5),       # spot rose toward strike
    # below-market NO buy: cushion = entry - strike (entry above strike)
    ("no", False, 105.0, 100.0, 102.0, 0.6),    # spot fell toward strike
])
def test_adverse_move_fraction(side, above, entry, strike, now, expected):
    frac = resting_guard.adverse_move_fraction(side, above, entry, now, strike)
    assert frac == pytest.approx(expected)


@pytest.mark.parametrize("side,above,entry,strike,now", [
    ("yes", True, 95.0, 95.0, 96.0),    # zero cushion -> no opinion
    ("yes", True, 100.0, 0.0, 99.0),    # non-positive strike
    ("yes", True, -100.0, 95.0, 99.0),  # non-positive entry
    ("yes", False, 95.0, 100.0, 0.0),   # non-positive now
])
def test_adverse_move_fraction_no_opinion(side, above, entry, strike, now):
    assert resting_guard.adverse_move_fraction(side, above, entry, now, strike) is None


def test_adverse_move_fraction_missing_inputs():
    assert resting_guard.adverse_move_fraction("yes", True, None, 99.0, 95.0) is None
    assert resting_guard.adverse_move_fraction("yes", True, "junk", 99.0, 95.0) is None


# ── guard with fakes ─────────────────────────────────────────────────

def _order(order_id="o1", side="yes", price=0.80, strike=95.0, spot=100.0,
           above_market=True, symbol="BTC", cost=4.0, state="resting"):
    return {"order_id": order_id, "slug": f"slug-{order_id}", "side": side,
            "price": price, "qty": int(cost / price) or 1, "cost": cost,
            "fee": 0.0, "symbol": symbol, "strike": strike, "spot": spot,
            "above_market": above_market, "state": state}


class _Recorder:
    def __init__(self):
        self.samples = []

    def record(self, stage, ms):
        self.samples.append((stage, ms))


def _make_guard(orders, *, spot=None, bbo=None, feed_age=None, halt=False,
                cancel_ok=True):
    cancel_log = []

    def list_orders():
        return orders

    def cancel_one(o, reason):
        ok = cancel_ok(o) if callable(cancel_ok) else cancel_ok
        cancel_log.append((o["order_id"], reason, bool(ok)))
        if ok:
            o["state"] = "cancelled"
        return bool(ok)

    guard = RestingGuard(
        lock=threading.RLock(),
        list_orders=list_orders,
        cancel_one=cancel_one,
        spot_for=lambda symbol: spot,
        bbo_for=lambda slug: bbo.get(slug) if isinstance(bbo, dict) else bbo,
        feed_age=lambda: feed_age,
        halt_active=lambda: halt,
        recorder=_Recorder(),
    )
    return guard, cancel_log


def test_spot_move_beyond_threshold_cancels_within_one_tick():
    orders = [_order(cost=5.0)]  # above-market YES, spot 100 -> strike 95
    guard, log = _make_guard(orders, spot=97.0)  # cushion 5, move 3 -> 0.6
    result = guard.evaluate()
    assert log == [("o1", TRIGGER_SPOT_MOVE, True)]
    assert result["cancelled"][0]["trigger"] == TRIGGER_SPOT_MOVE
    assert result["cancelled"][0]["cancel_latency_ms"] >= 0.0


def test_spot_move_in_favour_keeps_order():
    orders = [_order()]
    guard, log = _make_guard(orders, spot=110.0)  # cushion grows
    assert guard.evaluate()["cancelled"] == []
    assert orders[0]["state"] == "resting"


def test_spot_move_zero_cushion_order_never_cancelled_by_spot():
    orders = [_order(spot=95.0)]  # spot == strike at entry: no cushion to score
    guard, log = _make_guard(orders, spot=90.0)
    assert guard.evaluate()["cancelled"] == []
    assert orders[0]["state"] == "resting"


def test_feed_outage_cancels_all():
    orders = [_order("o1"), _order("o2", side="no")]
    guard, log = _make_guard(orders, feed_age=31.0)
    result = guard.evaluate()
    assert [c[0] for c in log] == ["o1", "o2"]
    assert all(c[1] == TRIGGER_FEED_STALE for c in log)
    assert len(result["cancelled"]) == 2


def test_feed_absent_stays_quiet():
    orders = [_order("o1")]
    guard, log = _make_guard(orders, feed_age=None)  # no stream expected
    assert guard.evaluate()["cancelled"] == []
    assert orders[0]["state"] == "resting"


def test_pause_entries_cancels_resting_orders():
    orders = [_order("o1"), _order("o2")]
    guard, log = _make_guard(orders, halt=True)
    guard.evaluate()
    assert all(c[1] == TRIGGER_HALT for c in log)
    assert all(o["state"] == "cancelled" for o in orders)


def test_bbo_crossed_yes_order_cancelled():
    # Our YES bid 0.82 vs market ask 0.75: we'd cross 7 ticks -> adverse fill.
    orders = [_order(price=0.82)]
    guard, log = _make_guard(orders, bbo=(0.70, 0.75))
    guard.evaluate()
    assert log == [("o1", TRIGGER_BBO_AWAY, True)]


def test_bbo_normal_book_keeps_yes_order():
    orders = [_order(price=0.70)]
    guard, log = _make_guard(orders, bbo=(0.70, 0.75))
    assert guard.evaluate()["cancelled"] == []


def test_bbo_crossed_no_order_cancelled():
    # Our NO sell at 0.18 (NO price); YES ask 0.75 -> NO bid 0.25 crosses it.
    orders = [_order(side="no", price=0.18)]
    guard, log = _make_guard(orders, bbo=(0.80, 0.75))
    guard.evaluate()
    assert log == [("o1", TRIGGER_BBO_AWAY, True)]


def test_bbo_normal_book_keeps_no_order():
    # Our NO sell at 0.25; NO bid = 1 - 0.76 = 0.24 < 0.25 -> no cross.
    orders = [_order(side="no", price=0.25)]
    guard, log = _make_guard(orders, bbo=(0.70, 0.76))
    assert guard.evaluate()["cancelled"] == []


def test_cancels_not_repeated_for_cancelled_orders():
    orders = [_order("o1")]
    guard, log = _make_guard(orders, feed_age=999.0)
    first = guard.evaluate()
    second = guard.evaluate()
    assert len(first["cancelled"]) == 1
    assert second["checked"] == 0
    assert second["cancelled"] == []
    assert len(log) == 1  # venue hit exactly once


def test_failed_cancel_retries_next_tick():
    state = {"calls": 0}
    orders = [_order("o1")]

    def cancel_ok(o):
        state["calls"] += 1
        return state["calls"] > 1  # first attempt refused, second accepted

    guard, log = _make_guard(orders, halt=True, cancel_ok=cancel_ok)
    first = guard.evaluate()
    assert first["cancelled"] == []  # refused: still resting
    assert orders[0]["state"] == "resting"
    second = guard.evaluate()
    assert [c["order_id"] for c in second["cancelled"]] == ["o1"]


def test_recorder_and_emit_capture_trigger_and_latency(monkeypatch):
    emitted = []
    monkeypatch.setattr("emit.emit_event",
                        lambda source, kind, payload, **kw:
                        emitted.append((source, kind, payload, kw)))
    orders = [_order("o1")]
    guard, log = _make_guard(orders, spot=97.0)
    guard.evaluate()
    stages = [s for s, _ in guard._recorder.samples]
    assert "cancel" in stages
    assert "cancel.spot_move" in stages
    assert emitted and emitted[0][1] == "order.cancelled"
    assert emitted[0][2]["reason"] == TRIGGER_SPOT_MOVE
    assert emitted[0][2]["cancel_latency_ms"] >= 0.0
    assert emitted[0][3]["key"] == "order.cancelled:o1:spot_move"


def test_evaluate_never_raises():
    def boom_list():
        raise RuntimeError("boom")

    guard = RestingGuard(lock=threading.RLock(), list_orders=boom_list,
                         cancel_one=lambda o, r: True,
                         spot_for=lambda s: None, bbo_for=lambda s: None,
                         feed_age=lambda: None, halt_active=lambda: False)
    result = guard.evaluate()
    assert result["error"]


def test_bad_config_rejected():
    with pytest.raises(ValueError):
        RestingGuard(lock=threading.RLock(), list_orders=list, cancel_one=None,
                     spot_for=None, bbo_for=None, feed_age=None,
                     halt_active=None, spot_move_fraction=1.0)
    with pytest.raises(ValueError):
        RestingGuard(lock=threading.RLock(), list_orders=list, cancel_one=None,
                     spot_for=None, bbo_for=None, feed_age=None,
                     halt_active=None, bbo_ticks=0)


def test_zero_resting_orders_is_noop():
    guard, log = _make_guard([], halt=True)
    result = guard.evaluate()
    assert result == {"checked": 0, "cancelled": []}
    assert log == []


# ── trader-level wiring (hermetic) ───────────────────────────────────

@pytest.fixture
def temp_trader(tmp_path):
    state_file = tmp_path / "state.json"
    ledger_file = tmp_path / "ledger.jsonl"
    with patch("poly_us_trader.STATE_FILE", state_file), \
            patch("poly_us_trader.LEDGER_FILE", ledger_file):
        import poly_us_trader
        trader = poly_us_trader.PolyUSPaperTrader(initial_cash=35.0, reset=True)
        trader.resting_guard._recorder = _Recorder()
        yield trader


def _resting_order(trader, oid="o1", cost=5.0, strike=95.0, spot=100.0):
    order = {"order_id": oid, "slug": f"slug-{oid}", "side": "yes",
             "price": 0.80, "qty": 6, "cost": cost, "fee": 0.0,
             "symbol": "BTC", "strike": strike, "spot": spot,
             "above_market": True, "state": "resting",
             "placed_at": "2026-11-17T00:00:00+00:00",
             "window_ts": trader.state.get("current_window_ts")}
    trader.state["cash"] = round(trader.state["cash"] - cost, 4)
    trader.state["resting_orders"] = [order]
    trader._save_state(trader.state)
    return order


def test_trader_feed_stale_cancels_resting_order(temp_trader):
    temp_trader.resting_guard._feed_age = lambda: 999.0  # feed dead
    order = _resting_order(temp_trader, cost=5.0)
    cash_before = temp_trader.state["cash"]
    result = temp_trader.resting_guard.evaluate()
    assert [c["order_id"] for c in result["cancelled"]] == ["o1"]
    assert order["state"] == "cancelled"
    assert temp_trader.state["cash"] == round(cash_before + 5.0, 4)
    assert temp_trader.state["resting_orders"] == []
    # cancelled order was persisted to the state file
    assert temp_trader.state_file.exists()


def test_trader_pause_entries_cancels_resting_order(temp_trader):
    _resting_order(temp_trader, oid="o-pause", cost=3.0)
    control.STATE.pause_entries = True
    try:
        result = temp_trader.resting_guard.evaluate()
        assert [c["order_id"] for c in result["cancelled"]] == ["o-pause"]
        assert temp_trader.state["resting_orders"] == []
    finally:
        control.STATE.pause_entries = False


def test_trader_spot_move_cancels_within_one_tick(temp_trader):
    temp_trader.resting_guard._spot_for = lambda symbol: 97.0
    _resting_order(temp_trader, oid="o-spot", cost=4.0)
    result = temp_trader.resting_guard.evaluate()
    assert [c["order_id"] for c in result["cancelled"]] == ["o-spot"]


def test_trader_no_repeat_cancel(temp_trader):
    temp_trader.resting_guard._feed_age = lambda: 999.0
    _resting_order(temp_trader, oid="o-once", cost=5.0)
    first = temp_trader.resting_guard.evaluate()
    second = temp_trader.resting_guard.evaluate()
    assert len(first["cancelled"]) == 1
    assert second["cancelled"] == []
    assert second["checked"] == 0


def test_guard_config_defaults():
    import config
    assert config.POLY_RESTING_GUARD_ENABLED is True
    assert config.POLY_GUARD_INTERVAL_S == 1.0
    assert config.POLY_GUARD_SPOT_MOVE_FRACTION == 0.5
    assert config.POLY_GUARD_BBO_TICKS == 5
    assert config.POLY_GUARD_FEED_STALE_SECONDS == 30.0
    assert config.PM_TICK_DOLLARS == 0.01
