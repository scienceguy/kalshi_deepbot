"""H8/H9 trader wiring: cycle() records order intents and latency rows.

H9: before the order REST call, cycle() records a write-ahead intent in
    pending_send; on a confirmed fill the intent is marked confirmed
    (confirm_order_intent_by_ticker).
H8: cycle() records a 'market_find' latency row after discovery and an
    'order_place' latency row after a placement attempt.

These tests patch trader.get_local_db to a real LocalDB in a tmp dir, so the
rows are asserted against actual SQLite, not a mock. No network, no venue.
"""
import sqlite3
import sys
import types
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

sys.argv = ["trader.py"]

import config
import local_db
import trader
from signals import TradeSignal
from market_finder import MarketInfo
from risk import RiskManager

TICKER = "KXBTC15M-26NOV15-45"


def _market(ticker=TICKER):
    return MarketInfo(
        ticker=ticker, series="KXBTC15M",
        close_time=datetime.now(timezone.utc), strike=0, strike_type="",
        yes_bid=92, yes_ask=93, no_bid=7, no_ask=8,
        yes_liquidity=100, no_liquidity=100, minutes_left=5.0,
        bracket_role="MAIN",
    )


def _client(positions=None):
    client = MagicMock()
    client.get_balance.return_value = 100.0
    client.get_positions.return_value = positions if positions is not None else []
    client.get_orders.return_value = []
    client.get_market.return_value = {"expiration_value": 1.0}
    client.invalidate_markets.return_value = None
    return client


def _rows(db_path, sql, params=()):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path):
    return local_db.LocalDB(db_path=str(tmp_path / "h8h9.db"), auto_sync=False)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path, db):
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(config, "LLM_EVALUATION_ENABLED", False)
    # Route every get_local_db() call in trader.py at the tmp DB.
    monkeypatch.setattr(trader, "get_local_db", lambda: db)
    # Hermetic side effects: no repo files, no webhooks, no reporter.
    monkeypatch.setattr(trader, "PAPER_POSITIONS_FILE", tmp_path / "paper_positions.json")
    monkeypatch.setattr(trader, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(trader, "emit_sync_lag", lambda *a, **k: None)
    monkeypatch.setattr(trader, "_report_cycle_state", lambda *a, **k: None)
    # Paper bankroll so _pre_trade_check doesn't block on empty state file.
    monkeypatch.setattr(trader.paper_mode, "cash", lambda *a, **k: 100.0)
    monkeypatch.setattr(trader.paper_mode, "start_balance", lambda: 10.0)
    monkeypatch.setattr(trader.paper_mode, "pnl", lambda: 0.0)
    monkeypatch.setattr(trader.state, "positions", {})
    monkeypatch.setattr(trader.state, "position_risk", {})
    monkeypatch.setattr(trader.state, "resting_value", {})
    monkeypatch.setattr(trader.state, "resting_tickers", set())
    monkeypatch.setattr(trader.state, "risk", RiskManager())
    monkeypatch.setattr(trader.state, "window", {
        "id": None, "start_cash": 0.0, "budget": 0.0, "used": 0.0,
        "reserved_cash": 0.0, "entered": set(),
    })
    monkeypatch.setattr(trader.state, "cycles", 0)
    monkeypatch.setattr(trader.state, "orders_today", 0)
    monkeypatch.setattr(trader.state, "last_order_time", 0.0)
    monkeypatch.setattr(trader.state, "trades_placed", 0)
    monkeypatch.setattr(trader.state, "consecutive_no_trade_cycles", 0)
    monkeypatch.setattr(trader.state, "today",
                        datetime.now(timezone.utc).date().isoformat())
    monkeypatch.setattr(trader, "find_active_markets", lambda c: [])
    monkeypatch.setattr(trader, "get_n8n_client", lambda: MagicMock())
    yield


def _placement_cycle(monkeypatch):
    """Drive one cycle that places (and in dry-run, fills) one YES order."""
    mkt = _market()
    sig = TradeSignal("YES", "yes", 93, 1.0, 1.0, "test", mkt, win_prob=0.99)
    monkeypatch.setattr(trader.se, "evaluate",
                        lambda m, cash, max_trade_override=None: [sig])
    monkeypatch.setattr(trader.se, "rank_signals", lambda sigs: list(sigs))
    monkeypatch.setattr(trader.gates, "prefetch", lambda assets: None)
    monkeypatch.setattr(trader.gates, "asset_for", lambda m: "BTC")
    monkeypatch.setattr(trader.gates, "evaluate_entry",
                        lambda *a, **k: types.SimpleNamespace(allowed=True))
    monkeypatch.setattr(trader, "find_active_markets", lambda c: [mkt])
    monkeypatch.setattr(trader.state, "_place_order",
                        lambda client, sig, coid: {
                            "order": {"order_id": "sb_test_order",
                                      "status": "resting"},
                        })
    trader.cycle(_client())


def test_cycle_placing_order_records_and_confirms_intent(monkeypatch, db):
    """One pending_send row for the signal; confirmed (not pending) after fill."""
    _placement_cycle(monkeypatch)

    rows = _rows(db.db_path,
                 "SELECT ticker, side, status, venue, mode FROM pending_send")
    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == TICKER
    assert row["side"] == "yes"
    assert row["status"] in ("pending", "confirmed")
    # dry-run book had yes_ask == entry_price with depth, so the fill
    # recorded in-cycle must have flipped the intent out of 'pending'.
    assert row["status"] == "confirmed"


def test_cycle_records_market_find_and_order_place_latency(monkeypatch, db):
    """A placing cycle leaves market_find and order_place latency rows."""
    _placement_cycle(monkeypatch)

    stages = [r["stage"] for r in _rows(
        db.db_path, "SELECT stage FROM latency_measurements")]
    assert "market_find" in stages
    assert "order_place" in stages


def test_cycle_without_signals_leaves_no_intent_and_records_market_find(
        monkeypatch, db):
    """No signals -> no pending_send rows; market_find latency still recorded."""
    trader.cycle(_client())  # fixture default: find_active_markets -> []

    rows = _rows(db.db_path, "SELECT COUNT(*) AS n FROM pending_send")
    assert rows[0]["n"] == 0
    stages = [r["stage"] for r in _rows(
        db.db_path, "SELECT stage FROM latency_measurements")]
    assert "market_find" in stages
    assert "order_place" not in stages
