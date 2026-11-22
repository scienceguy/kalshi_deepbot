"""B13: top-up cannot resurrect contracts after a partial exit.

After _manage_exits lowers a position's count, a stale positions payload can
make the per-cycle top-up raise it again. The increase is now only accepted
when a known resting entry order explains it, and never within
EXIT_TOPUP_GRACE_SECONDS of a filled exit on that ticker.
"""
import sys
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

sys.argv = ["trader.py"]

import config
import trader
from risk import RiskManager


TICKER = "KXBTC15M-26NOV15-45"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Isolate trader.state. Live mode: B14 made exchange-position adoption
    live-only, so the top-up guard is exercised against the exchange payload."""
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "EXIT_ENABLED", False)
    monkeypatch.setattr(trader.state, "positions", {})
    monkeypatch.setattr(trader.state, "position_risk", {})
    monkeypatch.setattr(trader.state, "resting_value", {})
    monkeypatch.setattr(trader.state, "resting_tickers", set())
    monkeypatch.setattr(trader.state, "last_exit_ts", {}, raising=False)
    monkeypatch.setattr(trader.state, "risk", RiskManager())
    monkeypatch.setattr(trader.state, "window", {
        "id": None, "start_cash": 0.0, "budget": 0.0, "used": 0.0,
        "reserved_cash": 0.0, "entered": set(),
    })
    monkeypatch.setattr(trader.state, "llm_trade_insights", {})
    monkeypatch.setattr(trader.state, "cycles", 0)
    monkeypatch.setattr(trader.state, "orders_today", 0)
    monkeypatch.setattr(trader.state, "trades_placed", 0)
    monkeypatch.setattr(trader.state, "today",
                        datetime.now(timezone.utc).date().isoformat())
    monkeypatch.setattr(trader, "find_active_markets", lambda c: _mkts())
    monkeypatch.setattr(trader, "get_n8n_client", lambda: MagicMock())
    yield


def _mkts():
    from market_finder import MarketInfo
    return [MarketInfo(
        ticker="KXBTC15M-UNRELATED", series="KXBTC15M",
        close_time=datetime.now(timezone.utc), strike=0, strike_type="",
        yes_bid=85, yes_ask=86, no_bid=14, no_ask=15,
        yes_liquidity=100, no_liquidity=100, minutes_left=5.0,
        bracket_role="MAIN",
    )]


def _client(positions, orders=None):
    client = MagicMock()
    client.get_positions.return_value = positions
    client.get_orders.return_value = orders or []
    client.get_market.return_value = {"expiration_value": 1.0}
    client.get_shard_balance.return_value = 100.0  # live mode sizes against the shard
    client.get_balance.return_value = 100.0        # live mode risk update reads the total balance
    return client


def _stale_payload(contracts=10):
    """A pre-exit snapshot: more contracts than the reduced local state."""
    return [{
        "ticker": TICKER, "yes_position": contracts, "no_position": 0,
        "yes_avg_price_dollars": "0.90", "no_avg_price_dollars": "0",
        "market_exposure_dollars": str(contracts * 0.90),
    }]


def _seed_position(contracts=5):
    trader.state.positions[TICKER] = {
        "side": "yes", "entry_price": 90, "contracts": contracts,
        "order_id": "sb_" + "a" * 32,
    }
    trader.state.position_risk[TICKER] = contracts * 0.90


def test_stale_payload_after_partial_exit_keeps_reduced_count(monkeypatch):
    """Acceptance: a partial exit followed by one stale positions response
    keeps the reduced count."""
    _seed_position(5)
    monkeypatch.setattr(trader.state, "last_exit_ts",
                        {TICKER: time.time()}, raising=False)
    client = _client(_stale_payload(10))
    trader.cycle(client)
    assert trader.state.positions[TICKER]["contracts"] == 5


def test_top_up_without_resting_order_is_suppressed():
    """An unexplained increase is not adopted even without a recent exit."""
    _seed_position(5)
    client = _client(_stale_payload(10))
    trader.cycle(client)
    assert trader.state.positions[TICKER]["contracts"] == 5


def test_top_up_with_known_resting_order_still_works(monkeypatch):
    """Acceptance: a real fill from a known resting entry order still tops
    up — with no recent exit."""
    _seed_position(5)
    monkeypatch.setattr(trader.state, "resting_tickers", {TICKER})
    monkeypatch.setattr(trader.state, "last_exit_ts", {}, raising=False)
    client = _client(_stale_payload(10))
    trader.cycle(client)
    assert trader.state.positions[TICKER]["contracts"] == 10


def test_top_up_within_exit_grace_is_suppressed(monkeypatch):
    """The grace period wins over the resting-order explanation right after
    an exit: sold contracts must not come back."""
    _seed_position(5)
    monkeypatch.setattr(trader.state, "resting_tickers", {TICKER})
    monkeypatch.setattr(trader.state, "last_exit_ts",
                        {TICKER: time.time()}, raising=False)
    client = _client(_stale_payload(10))
    trader.cycle(client)
    assert trader.state.positions[TICKER]["contracts"] == 5


def test_manage_exits_partial_fill_stamps_last_exit_ts(monkeypatch):
    """A partially filled exit lowers the stored count and stamps
    last_exit_ts (which the top-up grace reads)."""
    trader.state.positions[TICKER] = {
        "side": "yes", "entry_price": 90, "contracts": 10,
        "order_id": "sb_" + "a" * 32,
    }
    trader.state.position_risk[TICKER] = 9.0
    close = (datetime.now(timezone.utc) + timedelta(minutes=10))
    client = MagicMock()
    client.get_market.return_value = {
        "ticker": TICKER,
        "yes_bid_dollars": "0.95", "yes_ask_dollars": "0.96",
        "no_bid_dollars": "0.05", "no_ask_dollars": "0.06",
        "close_time": close.isoformat(),
    }
    client.sell_position.return_value = {
        # IOC partial fill: unfilled remainder is canceled, fill_count_fp reports
        # what actually left the position.
        "order": {"order_id": "exit-1", "status": "canceled",
                  "fill_count_fp": "4"},
    }
    monkeypatch.setattr(config, "EXIT_ENABLED", True)
    monkeypatch.setattr(config, "NO_ONLY", False)  # exercise the exit machinery
    monkeypatch.setattr(config, "EXIT_TAKE_PROFIT_CENTS", 3)
    monkeypatch.setattr(config, "EXIT_STOP_LOSS_CENTS", 25)
    monkeypatch.setattr(config, "EXIT_MIN_MINUTES_LEFT", 1.0)
    monkeypatch.setattr(trader, "_book_exit", lambda *a, **k: None)
    trader._manage_exits(client)
    pos = trader.state.positions[TICKER]
    assert pos["contracts"] == 6.0
    assert getattr(trader.state, "last_exit_ts", {}).get(TICKER, 0) > 0
