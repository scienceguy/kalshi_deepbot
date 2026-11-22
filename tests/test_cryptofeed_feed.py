"""Tests for T15-11: cryptofeed_feed.py normalized feed -> tick_bus ingestion."""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

import cryptofeed_feed
from cryptofeed_feed import (
    _on_ticker,
    _on_trade,
    _symbol_to_base,
    get_status,
    is_running,
)


class TestCryptofeedFeedBasics:
    """Basic cryptofeed_feed module behavior."""

    def test_is_running_false_initially(self):
        """Feed must not be running before start."""
        assert is_running() is False

    def test_get_status_keys(self):
        """get_status returns expected keys."""
        # Reset module state so this test is hermetic regardless of test order
        # (callback tests mutate the shared ingest counters).
        cryptofeed_feed.stop_cryptofeed()
        cryptofeed_feed._ticks_ingested = 0
        cryptofeed_feed._trades_ingested = 0
        status = get_status()
        assert set(status.keys()) == {
            "running", "venues", "symbols", "ticks_ingested", "trades_ingested"
        }
        assert status["running"] is False
        assert status["venues"] == ["COINBASE", "BINANCE"]
        assert status["symbols"] == ["BTC-USD", "ETH-USD"]
        assert status["ticks_ingested"] == 0
        assert status["trades_ingested"] == 0

    def test_symbol_to_base(self):
        """BTC-USD -> BTC, ETH-USD -> ETH."""
        assert _symbol_to_base("BTC-USD") == "BTC"
        assert _symbol_to_base("ETH-USD") == "ETH"
        assert _symbol_to_base("btc-usd") == "BTC"


def test_start_stop_idempotent():
    """stop then start cycle works; second start while running is a no-op."""
    cryptofeed_feed.stop_cryptofeed()
    # Enable test mode to avoid hitting API rate limits and enable feed addition
    from cryptofeed_feed import set_test_mode
    set_test_mode(True)
    try:
        cryptofeed_feed.start_cryptofeed()
        assert is_running() is True, "start_cryptofeed() should leave feed running"
        # Second start while running must be a no-op (no exception, same state)
        cryptofeed_feed.start_cryptofeed()
        assert is_running() is True, "second start_cryptofeed() should not change state"
    finally:
        cryptofeed_feed.stop_cryptofeed()
        set_test_mode(False)
    assert is_running() is False, "stop_cryptofeed() should leave feed not running"


class TestCryptofeedCallbacks:
    """Ticker/trade callbacks route through tick_bus ingestion API."""

    def _make_ticker(self, symbol="BTC-USD", bid=50000.0, ask=50002.0,
                     exchange="COINBASE", timestamp=123.0):
        return type("T", (), {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "exchange": exchange,
            "timestamp": timestamp,
            "raw": {},
        })()

    def _make_trade(self, symbol="BTC-USD", price=50000.0, amount=1.5,
                    side="buy", exchange="COINBASE"):
        return type("T", (), {
            "symbol": symbol,
            "price": price,
            "amount": amount,
            "side": side,
            "exchange": exchange,
            "timestamp": 123.0,
            "raw": {},
            "id": "x",
            "type": "buy",
        })()

    def test_ticker_routes_to_ingest_tick(self):
        """ticker coroutine calls tick_bus.ingest_tick with mid price + bid/ask."""
        fake_bus = MagicMock()
        fake_bus.ingest_tick.return_value = None
        with patch("tick_bus.get_tick_bus", return_value=fake_bus):
            asyncio.run(_on_ticker(self._make_ticker(), receipt_timestamp=999.0))
        fake_bus.ingest_tick.assert_called_once()
        kwargs = fake_bus.ingest_tick.call_args.kwargs
        assert kwargs["symbol"] == "BTC"
        assert kwargs["venue"] == "coinbase"
        assert kwargs["price"] == 50001.0
        assert kwargs["bid"] == 50000.0
        assert kwargs["ask"] == 50002.0
        assert kwargs["venue_timestamp"] == 999.0

    def test_ticker_mid_price_from_bid_ask(self):
        """Mid price is the average of bid and ask."""
        fake_bus = MagicMock()
        with patch("tick_bus.get_tick_bus", return_value=fake_bus):
            asyncio.run(_on_ticker(self._make_ticker(bid=49900.0, ask=50000.0), receipt_timestamp=1.0))
        assert fake_bus.ingest_tick.call_args.kwargs["price"] == 49950.0

    def test_trade_routes_to_ingest_trade(self):
        """trade coroutine calls tick_bus.ingest_trade with normalized fields."""
        fake_bus = MagicMock()
        with patch("tick_bus.get_tick_bus", return_value=fake_bus):
            asyncio.run(_on_trade(self._make_trade(), receipt_timestamp=555.0))
        fake_bus.ingest_trade.assert_called_once()
        kwargs = fake_bus.ingest_trade.call_args.kwargs
        assert kwargs["symbol"] == "BTC"
        assert kwargs["venue"] == "coinbase"
        assert kwargs["price"] == 50000.0
        assert kwargs["size"] == 1.5
        assert kwargs["side"] == "buy"

    def test_start_cryptofeed_safe_with_broken_venue_list(self):
        """Unknown venue -> warning, skipped, start does not raise."""
        cryptofeed_feed.stop_cryptofeed()
        # Temporarily set broken venue list
        cryptofeed_feed.CRYPTOFEED_VENUES = ["NOTAVENUE"]
        try:
            cryptofeed_feed.start_cryptofeed()
        finally:
            cryptofeed_feed.CRYPTOFEED_VENUES = ["COINBASE", "BINANCE"]
        # Should not raise


class TestCryptofeedImportGuard:
    """Import-guard path: module stubs when cryptofeed unavailable."""

    def test_public_functions_exist_when_unavailable(self, monkeypatch):
        """When availability flag is False, public functions exist and no-op safely."""
        monkeypatch.setattr(cryptofeed_feed, "_cryptofeed_available", False)
        # Public functions must exist (no AttributeError)
        assert callable(cryptofeed_feed.start_cryptofeed)
        assert callable(cryptofeed_feed.stop_cryptofeed)
        assert callable(cryptofeed_feed.is_running)
        assert callable(cryptofeed_feed.get_status)
        # start must no-op safely when unavailable
        cryptofeed_feed.stop_cryptofeed()
        cryptofeed_feed.start_cryptofeed()  # should log warning, not raise
        assert is_running() is False
        cryptofeed_feed.stop_cryptofeed()

    def test_cryptofeed_imports_ok(self):
        """The cryptofeed library itself must import in this env."""
        import importlib
        mod = importlib.import_module("cryptofeed")
        assert mod.FeedHandler is not None


if __name__ == "__main__":
    # Run the tests manually
    pytest.main([__file__, "-v"])