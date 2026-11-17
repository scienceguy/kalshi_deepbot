"""B14: small Kalshi-side items in trader.py.

- Dry-run never adopts or tops up LIVE exchange positions into paper state.
- cycle() resets consecutive_no_trade_cycles when an order is placed (the
  --once path used to re-increment it on unfilled placement, so a placed
  maker order still showed as a no-trade cycle).
- _touch_window branches on equity (cash + exposure), matching
  tradable_balance_limit, not on raw cash.
"""
import sys
import types
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

sys.argv = ["trader.py"]

import config
import trader
from signals import TradeSignal
from market_finder import MarketInfo
from risk import RiskManager


TICKER = "KXBTC15M-26NOV15-45"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(config, "LLM_EVALUATION_ENABLED", False)
    monkeypatch.setattr(trader.paper_mode, "cash", lambda *a, **k: 100.0)
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
    monkeypatch.setattr(trader.state, "last_order_time", 0.0)  # clear the order-gap clock left by prior tests
    monkeypatch.setattr(trader.state, "trades_placed", 0)
    monkeypatch.setattr(trader.state, "consecutive_no_trade_cycles", 0)
    monkeypatch.setattr(trader.state, "today",
                        datetime.now(timezone.utc).date().isoformat())
    monkeypatch.setattr(trader, "find_active_markets", lambda c: [])
    monkeypatch.setattr(trader, "get_n8n_client", lambda: MagicMock())
    yield


def _market(ticker=TICKER):
    return MarketInfo(
        ticker=ticker, series="KXBTC15M",
        close_time=datetime.now(timezone.utc), strike=0, strike_type="",
        yes_bid=92, yes_ask=93, no_bid=7, no_ask=8,
        yes_liquidity=100, no_liquidity=100, minutes_left=5.0,
        bracket_role="MAIN",
    )


def _exchange_position(contracts=10):
    return [{
        "ticker": TICKER, "yes_position": contracts, "no_position": 0,
        "yes_avg_price_dollars": "0.90", "no_avg_price_dollars": "0",
        "market_exposure_dollars": str(contracts * 0.90),
        "order_id": "sb_" + "a" * 32,
    }]


def _client(positions):
    client = MagicMock()
    client.get_positions.return_value = positions
    client.get_orders.return_value = []
    client.get_market.return_value = {"expiration_value": 1.0}
    return client


def test_dry_run_does_not_adopt_exchange_position():
    """A dry run must not adopt LIVE exchange positions into paper state."""
    client = _client(_exchange_position(10))
    trader.cycle(client)
    assert TICKER not in trader.state.positions


def test_dry_run_does_not_top_up_from_exchange_position():
    """... nor top a paper position up from the live exchange count."""
    trader.state.positions[TICKER] = {
        "side": "yes", "entry_price": 90, "contracts": 5,
        "order_id": "sb_" + "a" * 32,
    }
    client = _client(_exchange_position(10))
    trader.cycle(client)
    assert trader.state.positions[TICKER]["contracts"] == 5


def test_cycle_resets_no_trade_counter_on_placement(monkeypatch):
    """An order PLACED this cycle resets the counter even before it fills."""
    mkt = _market()
    sig = TradeSignal("YES", "yes", 93, 1.0, 1.0, "test", mkt, win_prob=0.99)
    monkeypatch.setattr(trader.se, "evaluate",
                        lambda m, cash, max_trade_override=None: [sig])
    monkeypatch.setattr(trader.se, "rank_signals", lambda sigs: list(sigs))
    monkeypatch.setattr(trader.gates, "prefetch", lambda assets: None)
    monkeypatch.setattr(trader.gates, "asset_for", lambda m: "BTC")
    monkeypatch.setattr(trader.gates, "evaluate_entry",
                        lambda *a, **k: types.SimpleNamespace(allowed=True))
    # Hermetic paper cash so _pre_trade_check doesn't block on empty state file.
    monkeypatch.setattr(trader.paper_mode, "cash", lambda open_risk, mtm=None: 100.0)
    monkeypatch.setattr(trader.paper_mode, "start_balance", lambda: 10.0)
    monkeypatch.setattr(trader.paper_mode, "pnl", lambda: 0.0)
    trader.state._place_order = lambda client, sig, coid: {
        "order": {"order_id": "sb_test_order", "status": "resting"},
    }
    monkeypatch.setattr(trader.state, "consecutive_no_trade_cycles", 5)
    monkeypatch.setattr(trader, "find_active_markets", lambda c: [mkt])
    client = _client([])
    trader.cycle(client)
    assert trader.state.consecutive_no_trade_cycles == 0


def test_touch_window_branches_on_equity(monkeypatch):
    """The micro-balance branch uses equity (cash + exposure), matching
    tradable_balance_limit — not raw cash."""
    now = datetime(2026, 1, 1, 12, 0, 0)
    # equity 12 >= 10: normal percentage budget even though cash is 8.
    trader._touch_window(now, 8.0, 4.0)
    assert trader.state.window["budget"] == pytest.approx(8.0 * config.WINDOW_TRADE_PCT)
    # same window, cash grew: refresh keeps the equity branch.
    trader._touch_window(now, 9.0, 4.0)
    assert trader.state.window["budget"] == pytest.approx(9.0 * config.WINDOW_TRADE_PCT)
    # new window, equity 8 < 10: micro path caps at 60% of equity.
    trader._touch_window(now + timedelta(minutes=16), 8.0, 0.0)
    assert trader.state.window["budget"] == pytest.approx(8.0 * 0.60)
