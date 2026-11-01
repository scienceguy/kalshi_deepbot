"""Tests for fees.py — Kalshi fee math."""
import pytest
from fees import (
    taker_fee_dollars,
    taker_fee_cents,
    buy_cash_out,
    sell_cash_in,
    net_profit_roundtrip,
)


def test_taker_fee_dollars_basic():
    """1 contract at 50c: fee = 0.07 * 1 * 0.5 * 0.5 = 0.0175 (rounded up to 6dp)."""
    fee = taker_fee_dollars(1, 0.50)
    assert abs(fee - 0.0175) < 0.00001


def test_taker_fee_dollars_zero_contracts():
    assert taker_fee_dollars(0, 0.50) == 0.0


def test_taker_fee_dollars_zero_price():
    assert taker_fee_dollars(1, 0.0) == 0.0


def test_buy_cash_out_basic():
    """Buy 1 contract at 50c: cost = 0.50 + 0.0175 fee = 0.5175 → ceil to 0.52."""
    cost = buy_cash_out(1, 0.50)
    assert cost == 0.52


def test_sell_cash_in_basic():
    """Sell 1 contract at 50c: proceeds = 0.50 - 0.0175 fee = 0.4825 → floor to 0.48."""
    revenue = sell_cash_in(1, 0.50)
    assert revenue == 0.48


def test_net_profit_roundtrip_win():
    """Buy 10 YES at 60c, sell at 100c (win)."""
    profit, pct = net_profit_roundtrip(10, 60, 100)
    # entry_value = 6.00, exit_value = 10.00, fee ≈ 0.168, pnl ≈ 3.832
    assert abs(profit - 3.832) < 0.001
    assert pct > 0


def test_net_profit_roundtrip_loss():
    """Buy 10 YES at 60c, sell at 0c (loss)."""
    profit, pct = net_profit_roundtrip(10, 60, 0)
    # entry_value = 6.00, exit_value = 0.00, fee ≈ 0.168, pnl ≈ -6.168
    assert abs(profit + 6.168) < 0.001
    assert pct < 0


def test_fees_never_negative():
    """Fees should never be negative for valid inputs."""
    assert taker_fee_dollars(1, 0.50) >= 0
    assert taker_fee_cents(1, 0.50) >= 0
    assert buy_cash_out(1, 0.50) >= 0
    assert sell_cash_in(1, 0.50) >= 0
