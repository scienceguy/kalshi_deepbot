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
        trader = PolyUSPaperTrader(initial_cash=35.0, reset=True)
        yield trader


def test_initial_state(temp_trader):
    assert temp_trader.state["cash"] == 35.0
    assert temp_trader.state["start_balance"] == 35.0
    assert temp_trader.state["positions"] == []


def test_reset_state(temp_trader):
    temp_trader.state["positions"] = [{"slug": "old", "cost": 10.0}]
    state = temp_trader.reset_state(35.0)
    assert state["start_balance"] == 35.0
    assert state["cash"] == 35.0
    assert state["positions"] == []
    assert state["milestones_reached"] == [35.0]


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
    assert temp_trader.state["cash"] < 35.0
    assert temp_trader.state["trades_count"] == 1


def test_compute_tier_size():
    from poly_us_trader import PolyUSPaperTrader
    assert PolyUSPaperTrader.compute_tier_size(20.0) == 0.60
    assert PolyUSPaperTrader.compute_tier_size(100.0) == 3.0
    assert PolyUSPaperTrader.compute_tier_size(500.0) == 15.0
    assert PolyUSPaperTrader.compute_tier_size(2000.0) == 60.0
    assert PolyUSPaperTrader.compute_tier_size(20000.0) == 600.0
    assert PolyUSPaperTrader.compute_tier_size(200000.0) == 4000.0


def test_opp_sorting_prioritizes_soonest_active():
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    future_10m = (now + timedelta(minutes=10)).isoformat()
    future_60m = (now + timedelta(minutes=60)).isoformat()
    expired_5m = (now - timedelta(minutes=5)).isoformat()

    opps = [
        {"slug": "future-60m", "end_date": future_60m},
        {"slug": "expired-5m", "end_date": expired_5m},
        {"slug": "future-10m", "end_date": future_10m},
    ]

    now_ts = now.timestamp()

    def _key(opp):
        ed = opp.get("end_date") or ""
        if not ed:
            return float("inf")
        try:
            dt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
            ts = dt.timestamp()
            if ts <= now_ts:
                return float("inf")
            return ts
        except Exception:
            return float("inf")

    sorted_opps = sorted(opps, key=_key)
    assert sorted_opps[0]["slug"] == "future-10m"
    assert sorted_opps[1]["slug"] == "future-60m"
    assert sorted_opps[2]["slug"] == "expired-5m"


def test_cash_reserve_floor_blocks_overtrade(temp_trader):
    # Total equity = $7 cash + $28 positions = $35
    temp_trader.state["positions"] = [{"cost": 28.0, "slug": "p1"}]
    temp_trader.state["cash"] = 7.0  # Cash is 20% of equity, below 30% floor
    opp = {
        "slug": "test-market-low-cash",
        "question": "Sample",
        "side": "yes",
        "price": 0.95,
    }
    temp_trader.preview_order = MagicMock(return_value=True)
    temp_trader.execute_paper_trade(opp)
    assert len(temp_trader.state["positions"]) == 1  # No new position added


def test_check_settlements_yes_win(temp_trader):
    temp_trader.state["positions"] = [{
        "slug": "test-settle-yes",
        "side": "yes",
        "price": 0.95,
        "qty": 5,
        "cost": 4.75,
    }]
    temp_trader.state["cash"] = 95.25
    temp_trader.state["realized_pnl"] = 0.0

    with patch("poly_us_trader.polymarket_us_client.get_market_settlement", return_value={"slug": "test-settle-yes", "settlement": 1.0}):
        temp_trader.check_settlements({"test-settle-yes": {"closed": True}})

    assert len(temp_trader.state["positions"]) == 0
    assert temp_trader.state["cash"] == 95.25 + 5.00  # Payout is $5.00
    assert temp_trader.state["realized_pnl"] == round(5.00 - 4.75, 4)  # +$0.25


def test_check_settlements_no_win(temp_trader):
    temp_trader.state["positions"] = [{
        "slug": "test-settle-no",
        "side": "no",
        "price": 0.98,
        "qty": 10,
        "cost": 9.80,
    }]
    temp_trader.state["cash"] = 90.20
    temp_trader.state["realized_pnl"] = 0.0

    # Settlement 0.0 means YES lost, so NO won: payout = (1.0 - 0.0) * 10 = $10.00
    with patch("poly_us_trader.polymarket_us_client.get_market_settlement", return_value={"slug": "test-settle-no", "settlement": 0.0}):
        temp_trader.check_settlements({"test-settle-no": {"closed": True}})

    assert len(temp_trader.state["positions"]) == 0
    assert temp_trader.state["cash"] == 90.20 + 10.00  # Payout is $10.00
    assert temp_trader.state["realized_pnl"] == round(10.00 - 9.80, 4)  # +$0.20


def test_polymarket_us_client_module_attributes():
    import polymarket_us_client
    expected_attrs = [
        "get_market_by_slug",
        "get_market_book",
        "get_market_settlement",
        "get_events",
        "get_event_by_slug",
        "search_events",
        "get_portfolio_positions",
        "get_account_balances",
        "usd_cash_balance",
        "preview_order",
        "place_maker_order",
        "get_open_orders",
        "cancel_order",
        "close_position",
    ]
    for attr in expected_attrs:
        assert hasattr(polymarket_us_client, attr), f"Missing attribute {attr} on polymarket_us_client"


def test_evaluate_opportunity_filters_long_dated(temp_trader):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    # Market expiring in 48 hours should be rejected
    market_48h = {
        "slug": "mkt-48h",
        "question": "Will BTC reach strike?",
        "outcomePrices": ["0.95", "0.05"],
        "endDate": (now + timedelta(hours=48)).isoformat(),
    }
    assert temp_trader.evaluate_opportunity(market_48h) is None

    # Market expiring in 2 hours should be accepted
    market_2h = {
        "slug": "mkt-2h",
        "question": "Will BTC reach strike?",
        "outcomePrices": ["0.95", "0.05"],
        "endDate": (now + timedelta(hours=2)).isoformat(),
    }
    opp = temp_trader.evaluate_opportunity(market_2h)
    assert opp is not None
    assert opp["slug"] == "mkt-2h"
    assert opp["hours_left"] is not None
    assert 1.9 <= opp["hours_left"] <= 2.1


def test_cycle_rotates_stale_long_dated_positions(temp_trader):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    # Open position with endDate 30 days in future
    future_30d = (now + timedelta(days=30)).isoformat()
    temp_trader.state["positions"] = [{
        "slug": "stale-dec-pos",
        "side": "no",
        "price": 0.99,
        "qty": 5,
        "cost": 4.95,
        "current_price": 0.99,
    }]
    temp_trader.state["cash"] = 50.0

    market_map = {
        "stale-dec-pos": {
            "slug": "stale-dec-pos",
            "endDate": future_30d,
            "outcomePrices": ["0.01", "0.99"],
        }
    }
    with patch.object(temp_trader, "scan_markets", return_value=list(market_map.values())):
        temp_trader.cycle()

    # Stale position rotated out, cash credited back
    assert len(temp_trader.state["positions"]) == 0
    assert temp_trader.state["cash"] == 50.0 + 4.95

