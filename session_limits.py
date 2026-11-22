"""
session_limits.py - Session-level risk limits (non-money-path).

Patterns extracted from:
  * AstroTick: daily risk limits (MAX_DAILY_LOSS_CENTS, MAX_DAILY_TRADES)
    that block NEW entries while open positions continue to be managed/exited.
  * kel-reid: market-maker safeguards (session limits, cooldown after a
    consecutive-loss streak, daily reset at UTC).

This module tracks state only. It never places, cancels, or amends orders and
never touches cash, positions, or P&L. Callers decide what to do when a limit
is exceeded (the usual pattern: block new entries, still manage exits).

All amounts are whole cents (ints) to avoid float drift.
"""

import time


class SessionLimits:
    """Session-level daily and consecutive-loss limits.

    Attributes:
        daily_loss_cents: realized loss this session/day, in cents.
        daily_trades: number of trade entries this session/day.
        consecutive_losses: current loss streak.
        cooldown_started_at: epoch seconds when the cooldown window began, or
            0.0 when not in cooldown.
        cooldown_seconds: length of the cooldown window in seconds.
    """

    def __init__(self, cooldown_seconds=3600):
        self.daily_loss_cents = 0
        self.daily_trades = 0
        self.consecutive_losses = 0
        self.cooldown_started_at = 0.0
        self.cooldown_seconds = float(cooldown_seconds)

    def record_market_loss(self, cents):
        """Record a realized market loss.

        Adds `cents` to the daily loss and increments the consecutive-loss
        streak. Non-positive values are ignored (a loss of zero is not a loss).
        """
        if cents <= 0:
            return
        self.daily_loss_cents += int(cents)
        self.consecutive_losses += 1

    def record_market_win(self):
        """Record a realized win: clears the loss streak and the cooldown."""
        self.consecutive_losses = 0
        self.cooldown_started_at = 0.0

    def record_trade(self):
        """Record a new trade entry: increments the daily trade count."""
        self.daily_trades += 1

    def daily_loss_exceeded(self, limit_cents):
        """True when the daily loss has reached or passed `limit_cents`."""
        return self.daily_loss_cents >= limit_cents

    def daily_trade_exceeded(self, limit):
        """True when the daily trade count has reached or passed `limit`."""
        return self.daily_trades >= limit

    def consecutive_loss_exceeded(self, limit):
        """True when the consecutive-loss streak has reached `limit`.

        Stamps the cooldown start on the first crossing (later crossings keep
        the original start so the window is not extended on every check).
        """
        if self.consecutive_losses >= limit and not self.cooldown_started_at:
            self.cooldown_started_at = time.time()
        return self.consecutive_losses >= limit

    def cooldown_remaining(self):
        """Seconds left in the cooldown window after the loss-limit hit.

        Returns 0.0 when not in cooldown. When the window has expired without a
        win, clears the stale cooldown state (and the streak) so new entries are
        allowed again, and returns 0.0.
        """
        if not self.cooldown_started_at:
            return 0.0
        remaining = self.cooldown_seconds - (time.time() - self.cooldown_started_at)
        if remaining <= 0.0:
            self.cooldown_started_at = 0.0
            self.consecutive_losses = 0
            return 0.0
        return remaining

    def reset_daily(self):
        """Called at UTC day reset: clears per-day loss and trade counts.

        The consecutive-loss streak and cooldown are carried across the day
        boundary (a losing streak is not forgiven at midnight); only the
        per-day counters reset here.
        """
        self.daily_loss_cents = 0
        self.daily_trades = 0
