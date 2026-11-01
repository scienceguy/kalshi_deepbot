"""Tests for trader.py core trading logic.

Tests _pre_trade_check caps, settlement PnL booking, and resting_value behavior.
Mocks KalshiClient and n8n_client to avoid real API calls.
"""
import sys
import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone

# Set sys.argv before importing trader to avoid argparse SystemExit
sys.argv = ["trader.py"]

import config
import trader
from trader import BotState
from fees import buy_cash_out, sell_cash_in


@pytest.fixture
def fresh_state():
    """Create a fresh BotState for each test."""
    return BotState()


@pytest.fixture
def mock_client():
    """Mock KalshiClient."""
    client = MagicMock()
    client.get_balance.return_value = 100.0
    client.get_positions.return_value = []
    client.get_market.return_value = {
        "expiration_value": 1.0,
    }
    return client


def test_pre_trade_check_blocks_over_cap(fresh_state, mock_client):
    """_pre_trade_check blocks trades that exceed per-trade cap."""
    sig = MagicMock()
    sig.dollar_risk = 50.0
    sig.entry_price = 50
    sig.market = MagicMock()
    sig.market.ticker = "TEST"

    # Set up window budget
    fresh_state.window["budget"] = 100.0
    fresh_state.window["used"] = 0.0

    allowed = fresh_state._pre_trade_check(
        sig, sig.market, cash=100.0, exposure=0.0, total_value=100.0, client=mock_client
    )
    assert not allowed


def test_pre_trade_check_blocks_cash_floor(fresh_state, mock_client):
    """_pre_trade_check blocks trades that breach cash floor."""
    sig = MagicMock()
    sig.dollar_risk = 99.0
    sig.entry_price = 50
    sig.market = MagicMock()
    sig.market.ticker = "TEST"

    # Set up window budget
    fresh_state.window["budget"] = 100.0
    fresh_state.window["used"] = 0.0

    allowed = fresh_state._pre_trade_check(
        sig, sig.market, cash=100.0, exposure=0.0, total_value=100.0, client=mock_client
    )
    assert not allowed


def test_pre_trade_check_allows_small_trade(fresh_state, mock_client):
    """_pre_trade_check allows small trades within limits."""
    sig = MagicMock()
    sig.dollar_risk = 5.0
    sig.entry_price = 50
    sig.market = MagicMock()
    sig.market.ticker = "TEST"

    # Set up window budget
    fresh_state.window["budget"] = 100.0
    fresh_state.window["used"] = 0.0

    allowed = fresh_state._pre_trade_check(
        sig, sig.market, cash=100.0, exposure=0.0, total_value=100.0, client=mock_client
    )
    assert allowed


def test_resting_value_tracked_on_order(fresh_state, mock_client):
    """resting_value is populated when an order is placed."""
    sig = MagicMock()
    sig.dollar_risk = 10.0
    sig.market = MagicMock()
    sig.market.ticker = "TEST"

    resp = {"order": {"order_id": "test_oid_123"}}
    fresh_state.resting_value["test_oid_123"] = sig.dollar_risk
    assert fresh_state.resting_value["test_oid_123"] == 10.0


def test_settlement_pnl_booking(fresh_state, mock_client):
    """Settlement PnL is correctly booked when YES wins."""
    contracts = 10
    entry_price = 50 / 100.0  # $0.50
    exit_price = 1.0  # YES wins

    buy_cost = buy_cash_out(contracts, entry_price)
    sell_revenue = sell_cash_in(contracts, exit_price)
    realized_pnl = sell_revenue - buy_cost
    won = sell_revenue > buy_cost

    assert won  # YES won, should be profitable
    assert realized_pnl > 0


def test_settlement_pnl_loss(fresh_state, mock_client):
    """Settlement PnL correctly records losses."""
    contracts = 10
    entry_price = 80 / 100.0  # $0.80
    exit_price = 0.0  # YES won, NO loses

    buy_cost = buy_cash_out(contracts, entry_price)
    sell_revenue = sell_cash_in(contracts, exit_price)
    realized_pnl = sell_revenue - buy_cost
    won = sell_revenue > buy_cost

    assert not won  # NO lost
    assert realized_pnl < 0  # Negative PnL


def test_resting_value_cleared_on_fill(fresh_state, mock_client):
    """resting_value entry is cleared when order is recorded as filled."""
    sig = MagicMock()
    sig.dollar_risk = 10.0
    sig.market = MagicMock()
    sig.market.ticker = "TEST"

    # First add to resting_value
    fresh_state.resting_value["test_oid"] = 10.0
    assert "test_oid" in fresh_state.resting_value

    # Then record as filled (which pops resting_value)
    fresh_state._record_filled_order(
        mock_client, sig, "test_oid", fill_count=1.0, status="executed",
        fees_dollars=0.5, cycles=1
    )
    assert "test_oid" not in fresh_state.resting_value


def test_resting_value_kept_for_resting_order(fresh_state, mock_client):
    """resting_value entry is kept for resting (unfilled) orders."""
    sig = MagicMock()
    sig.dollar_risk = 10.0
    sig.market = MagicMock()
    sig.market.ticker = "TEST"

    # Add to resting_value without recording as filled
    fresh_state.resting_value["test_oid"] = 10.0
    assert "test_oid" in fresh_state.resting_value
    assert fresh_state.resting_value["test_oid"] == 10.0


# ── cycle() regression tests (money path) ───────────────────────────────────

def _reset_cycle_state(monkeypatch):
    """Give the module-level trader.state a clean, isolated snapshot."""
    from risk import RiskManager
    monkeypatch.setattr(trader.state, "positions", {})
    monkeypatch.setattr(trader.state, "resting_value", {})
    monkeypatch.setattr(trader.state, "position_risk", {})
    monkeypatch.setattr(trader.state, "window", {
        "id": None, "start_cash": 0.0, "budget": 0.0, "used": 0.0,
        "reserved_cash": 0.0, "entered": set(),
    })
    monkeypatch.setattr(trader.state, "risk", RiskManager())
    monkeypatch.setattr(trader.state, "cycles", 0)
    monkeypatch.setattr(trader.state, "orders_today", 0)
    monkeypatch.setattr(trader.state, "today", "")


def _mkts():
    from market_finder import MarketInfo
    return [MarketInfo(
        ticker="KXBTC15M-TEST", series="KXBTC15M",
        close_time=datetime.now(timezone.utc), strike=0, strike_type="",
        yes_bid=85, yes_ask=86, no_bid=14, no_ask=15,
        yes_liquidity=100, no_liquidity=100, minutes_left=5.0,
        bracket_role="MAIN",
    )]


def test_cycle_does_not_crash_on_unknown_balance(monkeypatch):
    """cycle() must not crash when the balance fetch fails (cash=None).

    Regression: update_balance(None, ...) and _touch_window(..., None) raised
    TypeError, crashing the whole cycle on a transient balance fetch failure.
    """
    client = MagicMock()
    client.get_balance.side_effect = RuntimeError("network blip")
    client.get_positions.return_value = []
    client.invalidate_markets.return_value = None

    monkeypatch.setattr(trader, "find_active_markets", lambda c: _mkts())
    monkeypatch.setattr(trader, "get_supabase_client", lambda: MagicMock())
    _reset_cycle_state(monkeypatch)

    trader.cycle(client)  # must not raise


def test_hedge_contracts_are_integer():
    """_evaluate_hedge floors fractional size to an integer contract count."""
    from market_finder import MarketInfo
    from signals import _evaluate_hedge
    m = MarketInfo(
        ticker="T", series="S", close_time=None, strike=0, strike_type="",
        yes_bid=14, yes_ask=15, no_bid=84, no_ask=85,
        yes_liquidity=100, no_liquidity=100, minutes_left=7.0,
    )
    res = _evaluate_hedge(m, 1.0)  # $1 budget at 84c -> 1.19 -> floored to 1
    sig = res[0]
    assert sig.is_trade
    assert sig.contracts == float(int(sig.contracts))
    assert sig.dollar_risk == sig.contracts * sig.entry_price / 100


def test_cycle_prunes_stale_resting_value(monkeypatch):
    """resting_value entries for orders no longer resting are pruned."""
    client = MagicMock()
    client.get_balance.return_value = 100.0
    client.get_positions.return_value = []
    client.invalidate_markets.return_value = None
    client.get_orders.return_value = []          # nothing is resting now
    client.place_order.return_value = None

    monkeypatch.setattr(trader, "find_active_markets", lambda c: _mkts())
    monkeypatch.setattr(trader, "get_supabase_client", lambda: MagicMock())
    _reset_cycle_state(monkeypatch)
    trader.state.resting_value["stale_oid"] = 5.0

    trader.cycle(client)

    assert "stale_oid" not in trader.state.resting_value


def test_cycle_syncs_new_position_from_exchange(monkeypatch):
    """A position that appeared on the exchange is recorded in local state."""
    client = MagicMock()
    client.get_balance.return_value = 100.0
    client.get_positions.return_value = [{
        "ticker": "KXBTC15M-NEW", "yes_position": 2, "no_position": 0,
        "yes_avg_price_dollars": "0.85", "no_avg_price_dollars": "0",
    }]
    client.invalidate_markets.return_value = None
    client.get_orders.return_value = []
    client.get_market.return_value = {"expiration_value": 1.0}
    client.place_order.return_value = None

    monkeypatch.setattr(trader, "find_active_markets", lambda c: _mkts())
    monkeypatch.setattr(trader, "get_supabase_client", lambda: MagicMock())
    monkeypatch.setattr(trader, "get_n8n_client", lambda: MagicMock())
    _reset_cycle_state(monkeypatch)

    trader.cycle(client)

    assert "KXBTC15M-NEW" in trader.state.positions
    assert trader.state.positions["KXBTC15M-NEW"]["contracts"] == 2
    assert trader.state.positions["KXBTC15M-NEW"]["side"] == "yes"


def test_resolve_pending_settlement_yes_win():
    """_resolve_pending_settlement computes PnL for a winning YES."""
    client = MagicMock()
    client.get_market.return_value = {"expiration_value": 1.0}
    payload = {"ticker": "T", "side": "yes", "contracts": 10,
               "entry_price": 60, "pnl_dollars": None}
    out = trader._resolve_pending_settlement(client, payload)
    assert out is not None
    assert out["status"] == "settled"
    assert out["outcome_won"] is True
    assert out["pnl_dollars"] > 0


def test_resolve_pending_settlement_unavailable_returns_none():
    """Pending settlement with no price yet returns None (stay queued)."""
    client = MagicMock()
    client.get_market.return_value = {"expiration_value": None}
    payload = {"ticker": "T", "side": "yes", "contracts": 10,
               "entry_price": 60, "pnl_dollars": None}
    assert trader._resolve_pending_settlement(client, payload) is None


def test_replay_resolves_pending_and_credits(monkeypatch, tmp_path):
    """A queued pending settlement is resolved, credited, and drained."""
    import json
    qfile = tmp_path / "q.json"
    qfile.write_text(json.dumps([{"order_id": "oid1", "payload": {
        "action": "settlement", "order_id": "oid1", "status": "pending",
        "fill_price_cents": None, "outcome_won": None, "pnl_dollars": None,
        "ticker": "KXBTC15M-TEST", "side": "yes", "contracts": 10, "entry_price": 60,
    }}]))
    monkeypatch.setattr(trader, "SETTLEMENT_QUEUE_FILE", qfile)
    monkeypatch.setattr(trader, "get_n8n_client", lambda: MagicMock())
    monkeypatch.setattr(trader.state, "session_pnl", 0.0)
    client = MagicMock()
    client.get_market.return_value = {"expiration_value": 1.0}

    trader._replay_settlement_queue(client)

    assert trader.state.session_pnl > 0
    assert json.loads(qfile.read_text()) == []  # drained


def test_replay_keeps_unresolvable_pending_queued(monkeypatch, tmp_path):
    """A pending settlement that can't be resolved is kept, not dropped."""
    import json
    qfile = tmp_path / "q.json"
    entry = {"order_id": "oid1", "payload": {
        "action": "settlement", "order_id": "oid1", "status": "pending",
        "fill_price_cents": None, "outcome_won": None, "pnl_dollars": None,
        "ticker": "KXBTC15M-TEST", "side": "yes", "contracts": 10, "entry_price": 60,
    }}
    qfile.write_text(json.dumps([entry]))
    monkeypatch.setattr(trader, "SETTLEMENT_QUEUE_FILE", qfile)
    monkeypatch.setattr(trader.state, "session_pnl", 0.0)
    client = MagicMock()
    client.get_market.return_value = {"expiration_value": None}

    trader._replay_settlement_queue(client)

    assert trader.state.session_pnl == 0.0
    assert len(json.loads(qfile.read_text())) == 1  # still queued


def test_settlement_records_market_loss_to_correct_ticker(monkeypatch):
    """Settlement of a losing position books the loss to the settled ticker.

    Regression: the loss was recorded against `ticker` (only defined by an
    unrelated positions loop) instead of the loop variable `t`, causing a
    NameError (empty positions) or mis-attribution to the wrong ticker.
    """
    ticker = "KXBTC15M-TEST"
    client = MagicMock()
    client.get_balance.return_value = 100.0
    client.get_positions.return_value = []          # position has settled (gone)
    client.invalidate_markets.return_value = None
    client.get_market.return_value = {"expiration_value": 0.0}  # YES loses
    client.get_orders.return_value = []

    monkeypatch.setattr(trader, "find_active_markets", lambda c: _mkts())
    monkeypatch.setattr(trader, "get_supabase_client", lambda: MagicMock())
    monkeypatch.setattr(trader, "get_n8n_client", lambda: MagicMock())
    _reset_cycle_state(monkeypatch)

    # A previously-recorded YES position that has now settled as a loss.
    trader.state.positions[ticker] = {
        "side": "yes", "entry_price": 85, "contracts": 1, "order_id": "oid1",
    }
    trader.state.position_risk[ticker] = 0.85

    trader.cycle(client)

    # The loss must be recorded against the settled ticker, not a stray one.
    assert ticker in trader.state.risk.state.market_daily_losses
    assert ticker not in trader.state.positions
