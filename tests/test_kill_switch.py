"""Tests for kill_switch.py - armed/halted circuit breaker."""

import time

import pytest

from kill_switch import ARMED, HALTED, KillSwitch, RESET_MIN_INTERVAL_SECONDS


def test_constants():
    assert ARMED == "armed"
    assert HALTED == "halted"
    assert RESET_MIN_INTERVAL_SECONDS == 5 * 60


def test_initial_state_is_armed():
    ks = KillSwitch(scope="btc-15m")
    assert ks.state == ARMED
    assert ks.is_halted() is False
    assert ks.check() is True
    st = ks.status()
    assert st["scope"] == "btc-15m"
    assert st["state"] == ARMED
    assert st["halted"] is False
    assert st["reason"] is None
    assert st["tripped_by"] is None
    assert st["tripped_at"] is None


def test_trip_sets_halted_and_records_fields():
    ks = KillSwitch()
    ks.trip("atr hurdle", "monitor", metric="atr_cents", value=12, limit=15)
    assert ks.state == HALTED
    assert ks.is_halted() is True
    assert ks.check() is False
    st = ks.status()
    assert st["state"] == HALTED
    assert st["halted"] is True
    assert st["reason"] == "atr hurdle"
    assert st["tripped_by"] == "monitor"
    assert st["metric"] == "atr_cents"
    assert st["value"] == 12
    assert st["limit"] == 15
    assert isinstance(st["tripped_at"], (int, float))
    assert st["tripped_at"] > 0


def test_check_blocks_when_halted_allows_when_armed():
    ks = KillSwitch()
    assert ks.check() is True
    ks.trip("x", "human")
    assert ks.check() is False
    assert ks.is_halted() is True
    ks.reset("cleared", "operator", confirm=True)
    assert ks.check() is True
    assert ks.is_halted() is False


def test_reset_requires_confirmation():
    ks = KillSwitch()
    ks.trip("x", "monitor")
    with pytest.raises(ValueError, match="confirmation"):
        ks.reset("cleared", "operator")
    # a rejected reset leaves the switch halted
    assert ks.is_halted() is True
    assert ks.state == HALTED


def test_reset_rearms_with_confirmation():
    ks = KillSwitch()
    ks.trip("x", "monitor")
    assert ks.is_halted() is True
    ks.reset("false positive", "operator", confirm=True)
    assert ks.state == ARMED
    assert ks.is_halted() is False
    assert ks.check() is True
    st = ks.status()
    assert st["reset_by"] == "operator"
    assert st["reset_reason"] == "false positive"
    assert isinstance(st["reset_at"], (int, float))
    assert st["tripped_by"] == "monitor"  # trip record preserved


def test_reset_is_rate_limited_per_scope():
    ks = KillSwitch(scope="btc-15m", reset_interval_seconds=1.0)
    ks.trip("x", "monitor")
    ks.reset("first", "operator", confirm=True)
    assert ks.check() is True
    # a second reset within the window is rejected
    with pytest.raises(ValueError, match="rate-limited"):
        ks.reset("second", "operator", confirm=True)
    assert ks.state == ARMED  # untouched by the rejected reset
    # after the window elapses, reset is allowed again
    time.sleep(1.1)
    ks.reset("third", "operator", confirm=True)
    assert ks.check() is True
    st = ks.status()
    assert st["reset_by"] == "operator"
    assert st["reset_reason"] == "third"
