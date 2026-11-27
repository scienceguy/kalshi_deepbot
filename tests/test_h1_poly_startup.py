"""H1 — Polymarket US trader resumes a persisted pause_entries at startup.

The Kalshi trader reloads the persisted pause into control.STATE at startup;
the Polymarket trader did not, so after a restart the in-memory flag read by
execute_paper_trade() was False and new entries were placed despite the
signed pause persisted in local_db. These tests cover the startup reload and
prove the entry path refuses after it.
"""

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import control  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _reset_control_state():
    saved = control.STATE.pause_entries
    control.STATE.pause_entries = False
    yield
    control.STATE.pause_entries = saved


@pytest.fixture
def control_db(tmp_path):
    from local_db import LocalDB
    return LocalDB(db_path=str(tmp_path / "h1_poly_pause.db"), auto_sync=False)


def test_load_persisted_pause_sets_memory(control_db, monkeypatch):
    pytest.importorskip("polymarket_us")
    import local_db
    import poly_us_trader

    control.STATE.pause_entries = False  # what a fresh process looks like
    control_db.set_control_state("pause_entries",
                                 {"paused": True, "paused_by": "operator",
                                  "reason": "restart drill"})
    monkeypatch.setattr(local_db, "get_local_db", lambda *a, **k: control_db)

    poly_us_trader._load_persisted_pause()
    assert control.STATE.pause_entries is True

    # a persisted resume clears the flag for the next startup
    control_db.set_control_state("pause_entries", {"paused": False})
    control.STATE.pause_entries = False
    poly_us_trader._load_persisted_pause()
    assert control.STATE.pause_entries is False


def test_entry_refuses_after_restart_with_persisted_pause(control_db,
                                                          tmp_path,
                                                          monkeypatch):
    pytest.importorskip("polymarket_us")
    import local_db
    import poly_us_trader

    # Fresh restart: in-memory flag is False; only the DB row is paused.
    control.STATE.pause_entries = False
    control_db.set_control_state("pause_entries",
                                 {"paused": True, "paused_by": "operator",
                                  "reason": "restart drill"})
    monkeypatch.setattr(local_db, "get_local_db", lambda *a, **k: control_db)

    # Startup step now runs before the first cycle in main().
    poly_us_trader._load_persisted_pause()

    state_file = tmp_path / "state.json"
    ledger_file = tmp_path / "ledger.jsonl"
    with patch("poly_us_trader.STATE_FILE", state_file), \
         patch("poly_us_trader.LEDGER_FILE", ledger_file):
        trader = poly_us_trader.PolyUSPaperTrader(initial_cash=35.0, reset=True)

    opp = {"slug": "test-crypto-favorite", "question": "Q",
           "side": "yes", "price": 0.95}
    assert trader.execute_paper_trade(opp) is False


def test_main_applies_persisted_pause_during_startup(control_db, tmp_path,
                                                     monkeypatch):
    """Regression guard for the H1 call site in main().

    Every other test in this file calls _load_persisted_pause() directly, so
    deleting the call from main() (the actual regression) broke nothing.
    This test drives main() itself with everything downstream of the H1 step
    stubbed, and asserts the pause is already in memory when startup stops.
    """
    pytest.importorskip("polymarket_us")
    import sys

    import binance_feed
    import local_db
    import poly_us_trader

    control.STATE.pause_entries = False  # what a fresh process looks like
    control_db.set_control_state("pause_entries",
                                 {"paused": True, "paused_by": "operator",
                                  "reason": "restart drill"})
    monkeypatch.setattr(local_db, "get_local_db", lambda *a, **k: control_db)

    # Stub every filesystem / process / network touch in main() up to the
    # H1 step so entering it costs nothing and stays offline:
    #   DATA_DIR.mkdir + POLY_US_LOG_FILE -> tmp_path (logging.basicConfig is
    #   already a no-op under pytest, but keep the path harmless anyway),
    #   acquire_instance_lock -> no real flock/fd,
    #   PolyUSPaperTrader -> fake (the real ctor reads/writes state files and
    #   hits the venue gateway health check).
    monkeypatch.setattr(poly_us_trader, "DATA_DIR", tmp_path / "poly_us_data")
    monkeypatch.setattr(poly_us_trader, "POLY_US_LOG_FILE",
                        tmp_path / "poly_us_data" / "poly_us_dry.log")
    monkeypatch.setattr(poly_us_trader, "LOCK_FILE",
                        tmp_path / "poly_us_data" / ".lock_poly")
    monkeypatch.setattr(poly_us_trader, "acquire_instance_lock",
                        lambda *a, **k: None)

    class _FakeTrader:
        def __init__(self, *a, **k):
            self.state = {"start_balance": 35.0}

    monkeypatch.setattr(poly_us_trader, "PolyUSPaperTrader", _FakeTrader)
    monkeypatch.setattr(sys, "argv", ["poly_us_trader.py"])

    # The first statement after _load_persisted_pause() in main() is the
    # spot-stream block, wrapped in `except Exception`. A BaseException
    # sentinel therefore escapes that block untouched and stops main() right
    # there — before the prewarm, MemPalace bridge/bus and the cycle loop,
    # none of which may run in this test. (An Exception subclass would be
    # swallowed by that except and main() would march on into real startup.)
    class _StartupSentinel(BaseException):
        pass

    def _boom(*a, **k):
        raise _StartupSentinel()

    monkeypatch.setattr(binance_feed, "start_ws_streams", _boom)

    with pytest.raises(_StartupSentinel):
        poly_us_trader.main()

    assert control.STATE.pause_entries is True


# ── Persisted-pause reload: no cycles until an explicit resume ─────────────


def test_wait_for_resume_blocks_until_resume(control_db, monkeypatch, caplog):
    pytest.importorskip("polymarket_us")
    import logging

    import local_db
    import poly_us_trader

    control.STATE.pause_entries = True  # what a fresh paused process looks like
    control_db.set_control_state("pause_entries",
                                 {"paused": True, "paused_by": "operator"})
    monkeypatch.setattr(local_db, "get_local_db", lambda *a, **k: control_db)

    calls = {"drain": 0}

    def _fake_drain(state):
        calls["drain"] += 1
        if calls["drain"] >= 2:
            # second drain applies the resume, as control.drain_and_apply would
            control_db.set_control_state("pause_entries", {"paused": False})
            control.STATE.pause_entries = False

    monkeypatch.setattr(poly_us_trader, "_drain_control_commands", _fake_drain)
    monkeypatch.setattr(poly_us_trader, "_sleep_with_watchdog",
                        lambda s, chunk=30.0: None)

    class _FakeTrader:
        state = {"ruin_halted": False, "positions": []}

    with caplog.at_level(logging.WARNING):
        assert poly_us_trader._wait_for_resume(_FakeTrader(), interval=0.01) is True

    assert calls["drain"] >= 2
    assert control.STATE.pause_entries is False
    assert "Paused at startup — waiting for resume" in caplog.text


def test_wait_for_resume_stop_signal_never_resumes(control_db, monkeypatch,
                                                   caplog):
    pytest.importorskip("polymarket_us")
    import logging

    import local_db
    import poly_us_trader

    control.STATE.pause_entries = True
    control_db.set_control_state("pause_entries",
                                 {"paused": True, "paused_by": "operator"})
    monkeypatch.setattr(local_db, "get_local_db", lambda *a, **k: control_db)
    monkeypatch.setattr(poly_us_trader, "_stop_requested", True)
    monkeypatch.setattr(poly_us_trader, "_drain_control_commands",
                        lambda state: pytest.fail("must not drain once stopped"))

    class _FakeTrader:
        state = {"ruin_halted": False, "positions": []}

    with caplog.at_level(logging.INFO):
        assert poly_us_trader._wait_for_resume(_FakeTrader(), interval=0.01) is False

    assert control.STATE.pause_entries is True  # pause never lifted
    assert "not starting trading cycles" in caplog.text


def test_pause_entries_active_prefers_persisted_resume(control_db,
                                                       monkeypatch):
    pytest.importorskip("polymarket_us")
    import local_db
    import poly_us_trader

    control.STATE.pause_entries = True  # stale in-memory flag
    control_db.set_control_state("pause_entries", {"paused": False})
    monkeypatch.setattr(local_db, "get_local_db", lambda *a, **k: control_db)

    assert poly_us_trader._pause_entries_active() is False
    assert control.STATE.pause_entries is False


def test_pause_entries_active_persisted_pause_beats_stale_memory(control_db,
                                                                 monkeypatch):
    pytest.importorskip("polymarket_us")
    import local_db
    import poly_us_trader

    control.STATE.pause_entries = False
    control_db.set_control_state("pause_entries",
                                 {"paused": True, "paused_by": "operator"})
    monkeypatch.setattr(local_db, "get_local_db", lambda *a, **k: control_db)

    assert poly_us_trader._pause_entries_active() is True
    assert control.STATE.pause_entries is True


def test_pause_entries_active_read_failure_fails_closed(monkeypatch):
    pytest.importorskip("polymarket_us")
    import local_db
    import poly_us_trader

    control.STATE.pause_entries = True

    def _boom(*a, **k):
        raise OSError("db locked")

    monkeypatch.setattr(local_db, "get_local_db", _boom)
    assert poly_us_trader._pause_entries_active() is True
    assert control.STATE.pause_entries is True


def _stub_main(monkeypatch, tmp_path, control_db, drain_behavior):
    """Stub every filesystem / network / process touch in main() so driving
    it stays offline. drain_behavior(state) replaces _drain_control_commands."""
    import sys

    import agent_bus
    import binance_feed
    import local_db
    import poly_us_stream
    import polymarket_us_client
    import poly_us_trader

    monkeypatch.setattr(local_db, "get_local_db", lambda *a, **k: control_db)
    monkeypatch.setattr(poly_us_trader, "DATA_DIR", tmp_path / "poly_us_data")
    monkeypatch.setattr(poly_us_trader, "POLY_US_LOG_FILE",
                        tmp_path / "poly_us_data" / "poly_us_dry.log")
    monkeypatch.setattr(poly_us_trader, "LOCK_FILE",
                        tmp_path / "poly_us_data" / ".lock_poly")
    monkeypatch.setattr(poly_us_trader, "acquire_instance_lock",
                        lambda *a, **k: None)
    monkeypatch.setattr(poly_us_trader, "_stop_requested", False)
    monkeypatch.setattr(poly_us_trader, "_sleep_with_watchdog",
                        lambda s, chunk=30.0: None)
    monkeypatch.setattr(poly_us_trader, "_mp_bridge", lambda: None)
    monkeypatch.setattr(binance_feed, "start_ws_streams", lambda *a, **k: None)
    monkeypatch.setattr(poly_us_stream, "get_poly_stream", lambda *a, **k: None)
    monkeypatch.setattr(polymarket_us_client, "prewarm", lambda *a, **k: None)

    # The bus would bind a loopback port — replace with no-op fakes.
    class _FakeBus:
        def __init__(self, *a, **k):
            self.port = 0

        def start(self):
            pass

    monkeypatch.setattr(agent_bus, "BusServer", _FakeBus)
    monkeypatch.setattr(agent_bus, "BusReceiver", lambda *a, **k: object())

    holder = {}

    class _FakeTrader:
        def __init__(self, *a, **k):
            self.state = {"start_balance": 35.0, "ruin_halted": False,
                          "positions": []}
            self.cycle_calls = 0
            # J5: pause cache refresher attributes
            self._pause_cache_stop = threading.Event()
            self._pause_cache_thread = None

        def cycle(self):
            self.cycle_calls += 1
            # stop after the first cycle so the daemon loop exits under test
            poly_us_trader._stop_requested = True

        def cancel_all_resting_orders(self, reason=""):
            return True

        def flush_ledger(self, timeout=0):
            return True

    def _ctor(*a, **k):
        trader = _FakeTrader()
        holder["trader"] = trader
        return trader

    monkeypatch.setattr(poly_us_trader, "PolyUSPaperTrader", _ctor)
    monkeypatch.setattr(poly_us_trader, "_drain_control_commands",
                        drain_behavior)
    monkeypatch.setattr(sys, "argv", ["poly_us_trader.py"])
    return holder


def test_main_logging_does_not_construct_queue(control_db, tmp_path, monkeypatch):
    pytest.importorskip("polymarket_us")
    import logging

    import poly_us_trader

    holder = _stub_main(monkeypatch, tmp_path, control_db, lambda state: None)
    constructions = []

    def _record_queue(*args, **kwargs):
        constructions.append((args, kwargs))
        return object()

    def _keep_logging_inline(*args, **kwargs):
        raise RuntimeError("avoid replacing test root handlers")

    monkeypatch.setattr(poly_us_trader.queue, "Queue", _record_queue)
    monkeypatch.setattr(logging.handlers, "QueueHandler", _keep_logging_inline)
    poly_us_trader.main()

    assert constructions == []
    assert holder["trader"].cycle_calls == 1


def test_main_persisted_pause_holds_cycle_loop_until_resume(control_db,
                                                            tmp_path,
                                                            monkeypatch,
                                                            caplog):
    """Fresh boot with a persisted pause: main() must not enter the cycle
    loop; once the control inbox applies resume_entries, cycles resume."""
    pytest.importorskip("polymarket_us")
    import logging

    import poly_us_trader

    control.STATE.pause_entries = False  # fresh process; the DB carries the pause
    control_db.set_control_state("pause_entries",
                                 {"paused": True, "paused_by": "operator",
                                  "reason": "restart drill"})

    drained = {"n": 0}

    def _drain_resumes(state):
        drained["n"] += 1
        # the operator's resume_entries command lands here
        control_db.set_control_state("pause_entries", {"paused": False})
        control.STATE.pause_entries = False

    holder = _stub_main(monkeypatch, tmp_path, control_db, _drain_resumes)
    with caplog.at_level(logging.WARNING):
        poly_us_trader.main()

    assert "Paused at startup — waiting for resume" in caplog.text
    assert drained["n"] >= 1
    assert holder["trader"].cycle_calls == 1  # exactly one post-resume cycle
    assert control.STATE.pause_entries is False


def test_main_persisted_pause_with_stop_runs_no_cycle(control_db, tmp_path,
                                                      monkeypatch, caplog):
    """Fresh boot with a persisted pause and a stop signal arriving while
    paused: main() exits without running a single trading cycle."""
    pytest.importorskip("polymarket_us")
    import logging

    import poly_us_trader

    control.STATE.pause_entries = False  # fresh process; the DB carries the pause
    control_db.set_control_state("pause_entries",
                                 {"paused": True, "paused_by": "operator",
                                  "reason": "restart drill"})

    def _drain_never_resumes(state):
        # SIGTERM arrives while paused; no resume is ever applied
        poly_us_trader._stop_requested = True

    holder = _stub_main(monkeypatch, tmp_path, control_db, _drain_never_resumes)
    with caplog.at_level(logging.WARNING):
        poly_us_trader.main()

    assert "Paused at startup — waiting for resume" in caplog.text
    assert holder["trader"].cycle_calls == 0
    assert control.STATE.pause_entries is True  # pause never lifted
