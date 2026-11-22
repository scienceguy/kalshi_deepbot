"""Tests for quote_freshness.QuoteFreshness (Chainlink RTDS freshness gate)."""

import quote_freshness
from quote_freshness import QuoteFreshness


class _Clock:
    """Controllable clock for deterministic freshness tests."""

    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


def test_initial_state_empty():
    """No prices: is_fresh False, get_price None, staleness None."""
    clock = _Clock(1000.0)
    qf = QuoteFreshness(now=clock)
    assert qf.is_fresh("BTC", 60.0) is False
    assert qf.get_price("BTC") is None
    assert qf.staleness_seconds("BTC") is None


def test_update_records_price_and_timestamp():
    """update() stores price and timestamp; staleness is 0 at update time."""
    clock = _Clock(1000.0)
    qf = QuoteFreshness(now=clock)
    qf.update("BTC", 0.95, 1000.0)
    assert qf.get_price("BTC") == 0.95
    assert qf.staleness_seconds("BTC") == 0.0


def test_update_defaults_timestamp_to_now():
    """update(symbol, price) uses the injected clock for its timestamp."""
    clock = _Clock(1000.0)
    qf = QuoteFreshness(now=clock)
    qf.update("BTC", 0.95)
    assert qf.staleness_seconds("BTC") == 0.0
    clock.t = 1004.0
    assert qf.staleness_seconds("BTC") == 4.0


def test_is_fresh_within_and_outside_threshold():
    """is_fresh True within threshold, False outside (boundary is fresh)."""
    clock = _Clock(1000.0)
    qf = QuoteFreshness(now=clock)
    qf.update("BTC", 0.95, 1000.0)
    assert qf.is_fresh("BTC", 60.0) is True
    clock.t = 1030.0
    assert qf.is_fresh("BTC", 60.0) is True  # 30s < 60s
    clock.t = 1060.0
    assert qf.is_fresh("BTC", 60.0) is True  # exactly 60s == threshold
    clock.t = 1060.001
    assert qf.is_fresh("BTC", 60.0) is False  # just past threshold


def test_staleness_seconds_elapsed():
    """staleness_seconds() returns correct elapsed time."""
    clock = _Clock(1000.0)
    qf = QuoteFreshness(now=clock)
    qf.update("BTC", 0.95, 1000.0)
    clock.t = 1007.5
    assert qf.staleness_seconds("BTC") == 7.5
    assert qf.staleness_seconds("ETH") is None


def test_get_price_returns_none_for_stale():
    """get_price() returns None once the default staleness horizon passes."""
    clock = _Clock(1000.0)
    qf = QuoteFreshness(default_max_age_seconds=10.0, now=clock)
    qf.update("BTC", 0.95, 1000.0)
    assert qf.get_price("BTC") == 0.95
    clock.t = 1010.0
    assert qf.get_price("BTC") == 0.95  # exactly 10s == horizon -> fresh
    clock.t = 1010.001
    assert qf.get_price("BTC") is None  # just past horizon -> stale
