"""Tests for risk.py — Risk management."""
import pytest
from risk import RiskManager, RiskState
import config


def test_risk_initialize():
    r = RiskManager()
    r.initialize(100.0)
    assert r.state.start_balance == 100.0
    assert r.state.session_high == 100.0
    assert not r.state.halted


def test_max_trade_dollars_halted():
    r = RiskManager()
    r.state.halted = True
    assert r.max_trade_dollars(100.0, 0.0) == 0.0


def test_max_trade_dollars_consecutive_losses():
    r = RiskManager()
    r.state.consecutive_losses = 3
    assert r.max_trade_dollars(100.0, 0.0) == 0.0


def test_max_trade_dollars_exposure_ceiling():
    config.EXPOSURE_CEILING_PCT = 0.30
    config.CASH_FLOOR_PCT = 0.70
    config.MIN_CASH_ABSOLUTE = 1.00
    config.PER_TRADE_PCT = 0.08
    config.MAX_TRADE_DOLLARS = 3.00
    r = RiskManager()
    r.state.start_balance = 100.0
    r.state.session_high = 100.0
    # portfolio = 100, exposure_room = 30 - 0 = 30, cash_room = 100 - 0 - 70 = 30
    # per_trade = 100 * 0.08 = 8, max = 3
    result = r.max_trade_dollars(100.0, 0.0)
    assert result == 3.00


def test_max_trade_dollars_skips_ceiling_when_mtm_none():
    """When mark_to_market_exposure is None, the exposure-ceiling cap is
    skipped (not silently using cost-basis); cash_room / per_trade / MAX still
    bind, so a missing mtm never becomes an unbounded trade."""
    config.EXPOSURE_CEILING_PCT = 0.30
    config.CASH_FLOOR_PCT = 0.0
    config.MIN_CASH_ABSOLUTE = 0.0
    config.PER_TRADE_PCT = 0.5
    config.MAX_TRADE_DOLLARS = 50.0
    r = RiskManager()
    r.state.start_balance = 100.0
    r.state.session_high = 100.0
    # portfolio=195, cost-basis exposure=95. If the ceiling were applied with
    # cost-basis it would cap at max(0, 58.5-95)=0. With mtm=None the ceiling is
    # skipped, so the result is min(cash_room=100, per_trade=50, MAX=50)=50.
    result = r.max_trade_dollars(100.0, 95.0)
    assert result == 50.0


def test_max_trade_dollars_uses_mtm_for_ceiling_when_given():
    """When mtm IS provided, the ceiling uses it (not cost-basis)."""
    config.EXPOSURE_CEILING_PCT = 0.30
    config.CASH_FLOOR_PCT = 0.0
    config.MIN_CASH_ABSOLUTE = 0.0
    config.PER_TRADE_PCT = 0.5
    config.MAX_TRADE_DOLLARS = 50.0
    r = RiskManager()
    r.state.start_balance = 100.0
    r.state.session_high = 100.0
    # portfolio=195, mtm=20 -> exposure_room = max(0, 58.5-20)=38.5, which binds
    # below per_trade (50): the ceiling is what caps the trade at 38.5.
    result = r.max_trade_dollars(100.0, 95.0, mark_to_market_exposure=20.0)
    assert result == 38.5


def test_risk_check_blocks():
    r = RiskManager()
    r.state.start_balance = 100.0
    r.state.session_high = 100.0
    config.EXPOSURE_CEILING_PCT = 0.30
    config.CASH_FLOOR_PCT = 0.70
    config.MIN_CASH_ABSOLUTE = 1.00
    config.PER_TRADE_PCT = 0.08
    config.MAX_TRADE_DOLLARS = 3.00
    allowed, reason = r.check(100.0, 0.0, 50.0, "TEST")
    assert not allowed
    assert "risk cap" in reason


def test_reset_daily_keeps_the_peak_and_the_halt():
    """A day roll must not un-halt a bot that is still deep in drawdown."""
    r = RiskManager()
    r.state.start_balance = 100.0
    r.state.session_high = 150.0
    r.state.halted = True
    r.state.consecutive_losses = 3
    r.reset_daily()
    assert r.state.session_high == 150.0  # peak carried over
    assert r.state.halted is True         # halt lasts until the drawdown recovers
    assert r.state.consecutive_losses == 3  # consecutive losses preserved across daily resets
