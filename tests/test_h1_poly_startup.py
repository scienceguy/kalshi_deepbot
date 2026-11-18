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
