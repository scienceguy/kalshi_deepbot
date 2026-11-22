"""Tests for session_limits.py - Session-level risk limits."""
import time
from session_limits import SessionLimits


def test_initial_state_is_zero():
    s = SessionLimits()
    assert s.daily_loss_cents == 0
    assert s.daily_trades == 0
    assert s.consecutive_losses == 0
    assert s.cooldown_remaining() == 0.0


def test_record_market_loss_updates_loss_and_streak():
    s = SessionLimits()
    s.record_market_loss(150)
    assert s.daily_loss_cents == 150
    assert s.consecutive_losses == 1
    s.record_market_loss(250)
    assert s.daily_loss_cents == 400
    assert s.consecutive_losses == 2


def test_record_market_loss_ignores_non_positive():
    s = SessionLimits()
    s.record_market_loss(0)
    s.record_market_loss(-50)
    assert s.daily_loss_cents == 0
    assert s.consecutive_losses == 0


def test_daily_loss_exceeded_true_when_limit_crossed():
    s = SessionLimits()
    s.record_market_loss(100)
    assert not s.daily_loss_exceeded(200)
    s.record_market_loss(100)
    assert s.daily_loss_exceeded(200)


def test_daily_trade_exceeded_true_when_limit_crossed():
    s = SessionLimits()
    assert not s.daily_trade_exceeded(3)
    s.record_trade()
    s.record_trade()
    assert not s.daily_trade_exceeded(3)
    s.record_trade()
    assert s.daily_trade_exceeded(3)


def test_consecutive_loss_exceeded_true_when_streak_crossed():
    s = SessionLimits()
    assert not s.consecutive_loss_exceeded(3)
    s.record_market_loss(100)
    s.record_market_loss(100)
    assert not s.consecutive_loss_exceeded(3)
    s.record_market_loss(100)
    assert s.consecutive_loss_exceeded(3)


def test_record_market_win_resets_consecutive_losses():
    s = SessionLimits()
    s.record_market_loss(100)
    s.record_market_loss(100)
    s.record_market_loss(100)
    assert s.consecutive_losses == 3
    s.record_market_win()
    assert s.consecutive_losses == 0
    assert s.cooldown_remaining() == 0.0


def test_cooldown_remaining_positive_when_in_cooldown():
    s = SessionLimits()
    s.record_market_loss(100)
    s.record_market_loss(100)
    s.record_market_loss(100)
    s.consecutive_loss_exceeded(3)  # arms the cooldown
    s.cooldown_started_at = time.time()  # stamp it now
    remaining = s.cooldown_remaining()
    assert remaining > 0.0
    assert remaining <= s.cooldown_seconds


def test_cooldown_remaining_zero_when_not_in_cooldown():
    s = SessionLimits()
    assert s.cooldown_remaining() == 0.0


def test_cooldown_remaining_zero_after_expiry():
    s = SessionLimits(cooldown_seconds=1)
    s.record_market_loss(100)
    s.record_market_loss(100)
    s.record_market_loss(100)
    s.consecutive_loss_exceeded(3)
    s.cooldown_started_at = time.time() - 2  # window already expired
    assert s.cooldown_remaining() == 0.0
    assert s.consecutive_losses == 0  # cleared on expiry


def test_reset_daily_clears_day_counts_keeps_streak():
    s = SessionLimits()
    s.record_market_loss(500)
    s.record_trade()
    s.record_trade()
    s.record_market_loss(500)
    assert s.daily_loss_cents == 1000
    assert s.daily_trades == 2
    assert s.consecutive_losses == 2
    s.reset_daily()
    assert s.daily_loss_cents == 0
    assert s.daily_trades == 0
    assert s.consecutive_losses == 2  # carried across day boundary
