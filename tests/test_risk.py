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


def test_reset_daily():
    r = RiskManager()
    r.state.start_balance = 100.0
    r.state.session_high = 150.0
    r.state.halted = True
    r.reset_daily()
    assert r.state.session_high == 100.0
    assert not r.state.halted
    assert r.state.halt_reason == ""
