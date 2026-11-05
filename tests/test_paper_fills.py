"""Dry-run maker fill model: a posted bid fills only when the book crosses it."""
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.argv = ["trader.py"]

import trader


class _Sig:
    def __init__(self, ticker, side, entry_price, contracts=2):
        self.market = SimpleNamespace(ticker=ticker, series=ticker.split("-")[0])
        self.side = side
        self.entry_price = entry_price
        self.contracts = contracts


def _market(ticker, yes_ask=None, no_ask=None):
    return SimpleNamespace(ticker=ticker, yes_ask=yes_ask, no_ask=no_ask)


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.setattr(trader.state, "paper_resting", {})
    monkeypatch.setattr(trader.state, "resting_value", {})
    monkeypatch.setattr(trader.state, "_record_filled_order", lambda *a: None)
    yield
    trader.state.paper_resting.clear()
    trader.state.resting_value.clear()


def _rest(ticker, side, price, age=0.0):
    sig = _Sig(ticker, side, price)
    trader.state.paper_resting["oid-1"] = {"sig": sig, "placed_at": time.time() - age}
    trader.state.resting_value["oid-1"] = price / 100.0 * sig.contracts
    return sig


def test_fills_when_ask_crosses_the_bid(clean):
    _rest("KXBTC15M-1", "yes", 30)
    trader._manage_paper_fills([_market("KXBTC15M-1", yes_ask=30)], MagicMock())
    assert "oid-1" not in trader.state.paper_resting


def test_does_not_fill_while_ask_stays_above(clean):
    _rest("KXBTC15M-1", "yes", 30)
    trader._manage_paper_fills([_market("KXBTC15M-1", yes_ask=31)], MagicMock())
    assert "oid-1" in trader.state.paper_resting


def test_no_side_uses_the_no_ask(clean):
    _rest("KXBTC15M-1", "no", 85)
    # YES ask irrelevant for a NO order
    trader._manage_paper_fills([_market("KXBTC15M-1", yes_ask=1, no_ask=84)], MagicMock())
    assert "oid-1" not in trader.state.paper_resting


def test_unfilled_order_is_cancelled_when_stale(clean, monkeypatch):
    monkeypatch.setattr(trader.config, "STALE_ORDER_MINS", 5)
    _rest("KXBTC15M-1", "yes", 30, age=6 * 60)
    trader._manage_paper_fills([_market("KXBTC15M-1", yes_ask=40)], None)
    assert "oid-1" not in trader.state.paper_resting
    assert "oid-1" not in trader.state.resting_value


def test_order_released_when_market_disappears(clean):
    _rest("KXBTC15M-1", "yes", 30)
    trader._manage_paper_fills([], None)
    assert "oid-1" not in trader.state.paper_resting
    assert "oid-1" not in trader.state.resting_value


class TestClearRestingOrders:
    """Kalshi blocks an early-exit sell while any resting order exists on the market."""

    def test_cancels_only_that_ticker_and_releases_cash(self, clean, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(trader.config, "DRY_RUN", False)
        monkeypatch.setattr(trader.state, "live_resting_orders", [
            {"ticker": "KXBTC15M-1", "order_id": "oid-a"},
            {"ticker": "KXBTC15M-1", "order_id": "oid-b"},
            {"ticker": "KXETH15M-1", "order_id": "oid-c"},
        ])
        trader.state.resting_value.update({"oid-a": 1.0, "oid-b": 1.0, "oid-c": 1.0})

        trader.clear_resting_orders(client, "KXBTC15M-1")

        assert {c[0][0] for c in client.cancel_order.call_args_list} == {"oid-a", "oid-b"}
        assert "oid-a" not in trader.state.resting_value
        assert "oid-b" not in trader.state.resting_value
        assert "oid-c" in trader.state.resting_value

    def test_drops_a_parked_paper_entry_on_that_ticker(self, clean):
        sig = _Sig("KXBTC15M-1", "yes", 30)
        trader.state.paper_resting["oid-1"] = {"sig": sig, "placed_at": 0.0}
        trader.state.resting_value["oid-1"] = 0.6
        trader.clear_resting_orders(MagicMock(), "KXBTC15M-1")
        assert "oid-1" not in trader.state.paper_resting
        assert "oid-1" not in trader.state.resting_value
