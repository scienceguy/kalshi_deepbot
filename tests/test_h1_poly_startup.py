"""H1 — Polymarket US trader resumes a persisted pause_entries at startup.

The Kalshi trader reloads the persisted pause into control.STATE at startup;
the Polymarket trader did not, so after a restart the in-memory flag read by
execute_paper_trade() was False and new entries were placed despite the
signed pause persisted in local_db. These tests cover the startup reload and
prove the entry path refuses after it.
"""

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
