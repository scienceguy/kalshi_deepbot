"""Tests for T15-11: brti_tracker.py BRTI proxy tracker."""

import time
from unittest.mock import MagicMock

import pytest

from brti_tracker import (
    BRTITracker,
    BRTIProxy,
    ProxyErrorRecord,
    get_brti_tracker,
    start_brti_tracker,
    stop_brti_tracker,
    get_brti_proxy,
    get_brti_spot,
)


class TestBRTITrackerBasics:
    """Basic BRTI tracker functionality tests."""

    def setup_method(self):
        """Set up tracker with mock venue getters."""
        self.venue_getters = {
            "binance": lambda s: (50000.0, 0.1),
            "coinbase": lambda s: (50010.0, 0.1),
        }
        self.tracker = BRTITracker(symbols=["BTC"], venue_getters=self.venue_getters)

    def teardown_method(self):
        """Clean up."""
        self.tracker.stop()

    def test_tracker_creation(self):
        """Test tracker can be created."""
        assert self.tracker.symbols == ["BTC"]
        assert "binance" in self.tracker.venue_getters
        assert "coinbase" in self.tracker.venue_getters

    def test_tracker_start_stop(self):
        """Test tracker starts and stops."""
        self.tracker.start()
        assert self.tracker._running
        time.sleep(0.2)
        self.tracker.stop()
        assert not self.tracker._running

    def test_get_proxy_empty(self):
        """Test get_proxy returns None before any data (without starting)."""
        proxy = self.tracker.get_proxy("BTC")
        assert proxy is None

    def test_get_index_ref_empty(self):
        """Test get_index_ref returns None before any data (without starting)."""
        ref = self.tracker.get_index_ref("BTC")
        assert ref is None

    def test_get_spot_empty(self):
        """Test get_spot returns None before any data (without starting)."""
        spot = self.tracker.get_spot("BTC")
        assert spot is None


class TestBRTITrackerProxy:
    """Test BRTI proxy computation."""

    def setup_method(self):
        """Set up tracker with mock venue getters."""
        self.venue_getters = {
            "binance": lambda s: (50000.0, 0.1),
            "coinbase": lambda s: (50010.0, 0.1),
        }
        self.tracker = BRTITracker(symbols=["BTC"], venue_getters=self.venue_getters)

    def teardown_method(self):
        """Clean up."""
        self.tracker.stop()

    def test_median_spot(self):
        """Test median-of-mids spot computation."""
        self.tracker.start()
        time.sleep(0.2)  # Let it poll once
        spot = self.tracker.get_spot("BTC")
        # Median of 50000 and 50010 = 50005
        assert spot == 50005.0
        self.tracker.stop()

    def test_running_average(self):
        """Test 60-second running average computation."""
        self.tracker.start()
        time.sleep(0.2)  # Let it poll
        ref = self.tracker.get_index_ref("BTC")
        # With only 1-2 samples, running average ≈ spot
        assert ref is not None
        assert abs(ref - 50005.0) < 10.0  # Close to spot
        self.tracker.stop()

    def test_cross_venue_deviation(self):
        """Test cross-venue deviation tracking."""
        self.tracker.start()
        time.sleep(0.2)
        proxy = self.tracker.get_proxy("BTC")
        assert proxy is not None
        # 10 cents deviation on 50005 = 2 bps
        assert proxy.cross_venue_deviation_bps > 0
        self.tracker.stop()

    def test_staleness_flag(self):
        """Test staleness flag when venue data is old."""
        # Use stale venue getters
        old_getters = {
            "binance": lambda s: (50000.0, 10.0),  # 10 seconds old
            "coinbase": lambda s: (50010.0, 0.1),
        }
        tracker = BRTITracker(symbols=["BTC"], venue_getters=old_getters)
        tracker.start()
        time.sleep(0.2)
        proxy = tracker.get_proxy("BTC")
        assert proxy is not None
        # One venue is stale (>5s default), so staleness_flag should be True
        assert proxy.staleness_flag is True
        tracker.stop()


class TestBRTITrackerSettlement:
    """Test settlement recording and error tracking."""

    def setup_method(self):
        """Set up tracker with mock venue getters."""
        self.venue_getters = {
            "binance": lambda s: (50000.0, 0.1),
            "coinbase": lambda s: (50010.0, 0.1),
        }
        self.tracker = BRTITracker(symbols=["BTC"], venue_getters=self.venue_getters)

    def teardown_method(self):
        """Clean up."""
        self.tracker.stop()

    def test_record_settlement(self):
        """Test recording settlement and computing error."""
        self.tracker.start()
        time.sleep(0.2)  # Let it build proxy
        # Record a settlement
        self.tracker.record_settlement("BTC", 50002.0, minutes_to_expiry=0.0)
        stats = self.tracker.get_error_stats("BTC")
        assert stats["count"] == 1
        # Error = (50005 - 50002) / 50002 * 10000 ≈ 6 bps
        assert stats["mae_bps"] > 0
        self.tracker.stop()

    def test_error_stats_by_hour(self):
        """Test error stats grouped by hour."""
        self.tracker.start()
        time.sleep(0.2)
        self.tracker.record_settlement("BTC", 50000.0, minutes_to_expiry=0.0)
        stats = self.tracker.get_error_stats("BTC")
        assert "by_hour" in stats
        self.tracker.stop()


class TestBRTITrackerModuleSingleton:
    """Test module-level singleton functions."""

    def setup_method(self):
        """Clean up any existing tracker."""
        stop_brti_tracker()

    def teardown_method(self):
        """Clean up."""
        stop_brti_tracker()

    def test_get_brti_tracker_singleton(self):
        """Test get_brti_tracker returns same instance."""
        venue_getters = {"binance": lambda s: (50000.0, 0.1)}
        t1 = get_brti_tracker(["BTC"], venue_getters)
        t2 = get_brti_tracker(["BTC"], venue_getters)
        assert t1 is t2
        stop_brti_tracker()

    def test_start_stop_brti_tracker(self):
        """Test module-level start/stop."""
        venue_getters = {"binance": lambda s: (50000.0, 0.1)}
        start_brti_tracker(["BTC"], venue_getters)
        tracker = get_brti_tracker()
        assert tracker._running
        stop_brti_tracker()
        assert not tracker._running

    def test_get_brti_proxy_spot(self):
        """Test module-level convenience functions."""
        venue_getters = {
            "binance": lambda s: (50000.0, 0.1),
            "coinbase": lambda s: (50010.0, 0.1),
        }
        start_brti_tracker(["BTC"], venue_getters)
        time.sleep(0.2)
        proxy = get_brti_proxy("BTC")
        spot = get_brti_spot("BTC")
        assert proxy is not None
        assert spot is not None
        assert abs(proxy - 50005.0) < 10.0
        assert spot == 50005.0
        stop_brti_tracker()


class TestBRTITrackerMultipleSymbols:
    """Test tracker with multiple symbols."""

    def setup_method(self):
        """Set up tracker with multiple symbols."""
        self.venue_getters = {
            "binance": lambda s: (50000.0 if s == "BTC" else 3000.0, 0.1),
            "coinbase": lambda s: (50010.0 if s == "BTC" else 3010.0, 0.1),
        }
        self.tracker = BRTITracker(symbols=["BTC", "ETH"], venue_getters=self.venue_getters)

    def teardown_method(self):
        """Clean up."""
        self.tracker.stop()

    def test_multiple_symbols(self):
        """Test tracking multiple symbols independently."""
        self.tracker.start()
        time.sleep(0.2)
        btc_spot = self.tracker.get_spot("BTC")
        eth_spot = self.tracker.get_spot("ETH")
        assert btc_spot == 50005.0
        assert eth_spot == 3005.0
        self.tracker.stop()


class TestBRTIProxyDataclass:
    """Test BRTIProxy dataclass."""

    def test_brti_proxy_creation(self):
        """Test BRTIProxy can be created with all fields."""
        proxy = BRTIProxy(
            symbol="BTC",
            current_average=50005.0,
            current_spot=50005.0,
            venues_used=[],
            window_start=time.time() - 60,
            last_update=time.time(),
            samples_in_window=60,
        )
        assert proxy.symbol == "BTC"
        assert proxy.current_average == 50005.0
        assert proxy.current_spot == 50005.0
        assert proxy.samples_in_window == 60


if __name__ == "__main__":
    pytest.main([__file__, "-v"])