"""
Unit tests for poly_us_trader.py.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from poly_us_trader import PolyUSPaperTrader


@pytest.fixture
def temp_trader(tmp_path):
    state_file = tmp_path / "state.json"
    ledger_file = tmp_path / "ledger.jsonl"
    with patch("poly_us_trader.STATE_FILE", state_file), patch("poly_us_trader.LEDGER_FILE", ledger_file):
        trader = PolyUSPaperTrader(initial_cash=100.0)
        yield trader


def test_initial_state(temp_trader):
    assert temp_trader.state["cash"] == 100.0
    assert temp_trader.state["start_balance"] == 100.0
    assert temp_trader.state["positions"] == []


def test_evaluate_opportunity_finds_favorites(temp_trader):
    market = {
        "slug": "test-crypto-favorite",
        "question": "Will BTC reach strike?",
        "outcomePrices": ["0.95", "0.05"],
    }
    opp = temp_trader.evaluate_opportunity(market)
    assert opp is not None
    assert opp["side"] == "yes"
    assert opp["price"] == 0.95


def test_evaluate_opportunity_ignores_mid_band(temp_trader):
    market = {
        "slug": "test-crypto-mid",
        "question": "Will ETH reach strike?",
        "outcomePrices": ["0.50", "0.50"],
    }
    opp = temp_trader.evaluate_opportunity(market)
    assert opp is None


def test_evaluate_opportunity_supports_single_price_complement(temp_trader):
    market = {
        "slug": "cpc-btc-150k-11-30-2026",
        "question": "When will Bitcoin hit $150k?",
        "outcomePrices": ["0.0100"],
    }
    opp = temp_trader.evaluate_opportunity(market)
    assert opp is not None
    assert opp["side"] == "no"
    assert opp["price"] == 0.99


def test_execute_paper_trade_updates_bankroll(temp_trader):
    opp = {
        "slug": "test-market-1",
        "question": "Sample Question",
        "side": "yes",
        "price": 0.95,
    }
    temp_trader.preview_order = MagicMock(return_value=True)
    temp_trader.execute_paper_trade(opp)
    assert len(temp_trader.state["positions"]) == 1
    assert temp_trader.state["cash"] < 100.0
    assert temp_trader.state["trades_count"] == 1


def test_compute_tier_size():
    from poly_us_trader import PolyUSPaperTrader
    assert PolyUSPaperTrader.compute_tier_size(100.0) == 5.0
    assert PolyUSPaperTrader.compute_tier_size(500.0) == 25.0
    assert PolyUSPaperTrader.compute_tier_size(2000.0) == 80.0
    assert PolyUSPaperTrader.compute_tier_size(20000.0) == 600.0
    assert PolyUSPaperTrader.compute_tier_size(200000.0) == 4000.0


def test_cash_reserve_floor_blocks_overtrade(temp_trader):
    # Total equity = $20 cash + $80 positions = $100
    temp_trader.state["positions"] = [{"cost": 80.0, "slug": "p1"}]
    temp_trader.state["cash"] = 20.0  # Cash is 20% of equity, below 30% floor
    opp = {
        "slug": "test-market-low-cash",
        "question": "Sample",
        "side": "yes",
        "price": 0.95,
    }
    temp_trader.preview_order = MagicMock(return_value=True)
    temp_trader.execute_paper_trade(opp)
    assert len(temp_trader.state["positions"]) == 1  # No new position added
