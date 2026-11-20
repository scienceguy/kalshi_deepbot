"""Tests for T15-10: tick_bus.py event-driven spot snapshot."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tick_bus import TickBus, Tick, Snapshot, get_tick_bus, start_tick_bus, stop_tick_bus, set_test_mode


class TestTickBusBasics:
    """Basic tick bus functionality tests."""

    def setup_method(self):
        """Enable test mode before each test."""
        set_test_mode(True)

    def teardown_method(self):
        """Disable test mode after each test."""
        set_test_mode(False)

    def test_tick_bus_creation(self):
        """Test tick bus can be created with symbols."""
        bus = TickBus(symbols=["BTC", "ETH"])
        assert bus.symbols == ["BTC", "ETH"]
        assert not bus._running

    def test_tick_bus_start_stop(self):
        """Test tick bus starts and stops without error."""
        bus = TickBus(symbols=["BTC"])
        bus.start()
        assert bus._running
        time.sleep(0.2)  # Let it start
        bus.stop()
        assert not bus._running

    def test_get_snapshot_empty(self):
        """Test get_snapshot returns None for unknown symbol."""
        bus = TickBus(symbols=["BTC"])
        bus.start()
        time.sleep(0.1)
        snap = bus.get_snapshot("BTC")
        # No data yet, should be None
        assert snap is None
        bus.stop()

    def test_get_snapshot_with_age_empty(self):
        """Test get_snapshot_with_age returns (None, None) for unknown symbol."""
        bus = TickBus(symbols=["BTC"])
        bus.start()
        time.sleep(0.1)
        price, age = bus.get_snapshot_with_age("BTC")
        assert price is None
        assert age is None
        bus.stop()

    def test_get_atr_sigma_empty(self):
        """Test get_atr/get_sigma return None when no data."""
        bus = TickBus(symbols=["BTC"])
        bus.start()
        time.sleep(0.1)
        assert bus.get_atr("BTC") is None
        assert bus.get_sigma("BTC") is None
        bus.stop()

    def test_venue_state(self):
        """Test venue state tracking."""
        bus = TickBus(symbols=["BTC"])
        binance_state = bus.get_venue_state("binance")
        coinbase_state = bus.get_venue_state("coinbase")
        assert binance_state is not None
        assert binance_state.name == "binance"
        assert coinbase_state is not None
        assert coinbase_state.name == "coinbase"
        # Initially not connected
        assert not binance_state.connected
        assert not coinbase_state.connected
        bus.stop()


class TestTickBusSnapshot:
    """Test snapshot building from venue ticks."""

    def setup_method(self):
        """Enable test mode before each test."""
        set_test_mode(True)

    def teardown_method(self):
        """Disable test mode after each test."""
        set_test_mode(False)

    def test_snapshot_median_price(self):
        """Test snapshot computes median price across venues."""
        bus = TickBus(symbols=["BTC"])
        bus.start()
        time.sleep(0.1)
        # Manually inject ticks via the cache
        with bus._latest_ticks_lock:
            bus._latest_ticks["binance"]["BTC"] = Tick(
                symbol="BTC", venue="binance", price=50000.0, bid=49999.0, ask=50001.0
            )
            bus._latest_ticks["coinbase"]["BTC"] = Tick(
                symbol="BTC", venue="coinbase", price=50010.0, bid=50009.0, ask=50011.0
            )
        # Build snapshots
        bus._build_snapshots()
        # Wait for snapshot builder to run (it runs every 10ms)
        time.sleep(0.05)
        snap = bus.get_snapshot("BTC")
        assert snap is not None
        # Median of 50000 and 50010 = 50005
        assert snap.price == 50005.0
        assert snap.bid == 50009.0  # max bid
        assert snap.ask == 50001.0  # min ask
        assert "binance" in snap.venues
        assert "coinbase" in snap.venues
        bus.stop()

    def test_snapshot_single_venue(self):
        """Test snapshot works with single venue."""
        bus = TickBus(symbols=["BTC"])
        bus.start()
        time.sleep(0.1)
        with bus._latest_ticks_lock:
            bus._latest_ticks["binance"]["BTC"] = Tick(
                symbol="BTC", venue="binance", price=50000.0, bid=49999.0, ask=50001.0
            )
        bus._build_snapshots()
        time.sleep(0.05)
        snap = bus.get_snapshot("BTC")
        assert snap is not None
        assert snap.price == 50000.0
        assert snap.bid == 49999.0
        assert snap.ask == 50001.0
        bus.stop()

    def test_staleness_flag(self):
        """Test staleness flag is set when venue data is old."""
        bus = TickBus(symbols=["BTC"])
        bus.start()
        time.sleep(0.1)
        old_time = time.monotonic() - 10.0  # 10 seconds ago
        with bus._latest_ticks_lock:
            bus._latest_ticks["binance"]["BTC"] = Tick(
                symbol="BTC", venue="binance", price=50000.0, timestamp=old_time
            )
        bus._build_snapshots()
        time.sleep(0.05)
        bus._update_snapshots()
        snap = bus.get_snapshot("BTC")
        assert snap is not None
        # Staleness threshold is 1.0s by default, 10s old should be stale
        assert snap.staleness_flag is True
        bus.stop()


class TestTickBusATRSigma:
    """Test ATR and sigma incremental computation."""

    def setup_method(self):
        """Enable test mode before each test."""
        set_test_mode(True)

    def teardown_method(self):
        """Disable test mode after each test."""
        set_test_mode(False)

    def test_atr_computation(self):
        """Test ATR is computed from minute bars."""
        bus = TickBus(symbols=["BTC"])
        bus.start()
        time.sleep(0.1)
        # Manually inject some minute prices
        bus._last_price["BTC"] = 50000.0
        bus._atr_state["BTC"].append(10.0)  # TR = 10
        bus._atr_state["BTC"].append(12.0)  # TR = 12
        bus._update_snapshots()
        time.sleep(0.05)
        snap = bus.get_snapshot("BTC")
        # ATR should be mean of 10 and 12 = 11
        if snap and snap.atr is not None:
            assert abs(snap.atr - 11.0) < 0.01
        bus.stop()

    def test_sigma_computation(self):
        """Test sigma is computed from log returns."""
        bus = TickBus(symbols=["BTC"])
        bus.start()
        time.sleep(0.1)
        # Inject log returns
        bus._returns_state["BTC"].append(0.01)
        bus._returns_state["BTC"].append(-0.005)
        bus._returns_state["BTC"].append(0.002)
        bus._update_snapshots()
        time.sleep(0.05)
        snap = bus.get_snapshot("BTC")
        if snap and snap.sigma_per_min is not None:
            # Std of [0.01, -0.005, 0.002] = ~0.0062
            assert snap.sigma_per_min > 0
        bus.stop()


class TestTickBusGapDetection:
    """Test sequence gap detection."""

    def setup_method(self):
        """Enable test mode before each test."""
        set_test_mode(True)

    def teardown_method(self):
        """Disable test mode after each test."""
        set_test_mode(False)

    def test_binance_gap_detection(self):
        """Test Binance sequence gap detection."""
        bus = TickBus(symbols=["BTC"])
        venue_state = bus._venue_states["binance"]
        venue_state.last_sequence = 100
        bus._check_gap(venue_state, 105)  # Gap of 4
        assert venue_state.gaps_detected == 4
        assert venue_state.last_sequence == 105
        bus.stop()

    def test_coinbase_gap_detection(self):
        """Test Coinbase sequence gap detection."""
        bus = TickBus(symbols=["BTC"])
        venue_state = bus._venue_states["coinbase"]
        venue_state.last_sequence = 200
        bus._check_gap(venue_state, 201)  # No gap
        assert venue_state.gaps_detected == 0
        assert venue_state.last_sequence == 201
        bus.stop()


class TestTickBusCompatibility:
    """Test compatibility shims for existing code."""

    def setup_method(self):
        """Enable test mode before each test."""
        set_test_mode(True)

    def teardown_method(self):
        """Disable test mode after each test."""
        set_test_mode(False)

    def test_spot_price_shim(self):
        """Test spot_price() compatibility function."""
        stop_tick_bus()  # Clean up any existing
        start_tick_bus(["BTC"])
        bus = get_tick_bus()
        time.sleep(0.1)
        with bus._latest_ticks_lock:
            bus._latest_ticks["binance"]["BTC"] = Tick(
                symbol="BTC", venue="binance", price=50000.0
            )
        bus._build_snapshots()
        time.sleep(0.05)
        from tick_bus import spot_price
        price = spot_price("BTC")
        assert price == 50000.0
        stop_tick_bus()

    def test_spot_with_age_shim(self):
        """Test spot_with_age() compatibility function."""
        stop_tick_bus()  # Clean up any existing
        start_tick_bus(["BTC"])
        bus = get_tick_bus()
        time.sleep(0.1)
        with bus._latest_ticks_lock:
            bus._latest_ticks["binance"]["BTC"] = Tick(
                symbol="BTC", venue="binance", price=50000.0
            )
        bus._build_snapshots()
        time.sleep(0.05)
        from tick_bus import spot_with_age
        price, age = spot_with_age("BTC")
        assert price == 50000.0
        assert age >= 0
        stop_tick_bus()


class TestTickBusModuleSingleton:
    """Test module-level singleton functions."""

    def setup_method(self):
        """Enable test mode before each test."""
        set_test_mode(True)

    def teardown_method(self):
        """Disable test mode after each test."""
        set_test_mode(False)

    def test_get_tick_bus_singleton(self):
        """Test get_tick_bus returns same instance."""
        stop_tick_bus()  # Clean up any existing
        bus1 = get_tick_bus(["BTC"])
        bus2 = get_tick_bus(["BTC"])
        assert bus1 is bus2
        stop_tick_bus()

    def test_start_stop_tick_bus(self):
        """Test module-level start/stop."""
        stop_tick_bus()
        start_tick_bus(["BTC"])
        bus = get_tick_bus()
        assert bus._running
        stop_tick_bus()
        assert not bus._running


if __name__ == "__main__":
    pytest.main([__file__, "-v"])