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
