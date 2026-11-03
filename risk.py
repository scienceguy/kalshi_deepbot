"""
risk.py — Consolidated risk management for Kalshi SuperBot.

Pulls from kalshi_15m_bot (70% cash floor, 30% exposure ceiling, 8% per trade,
15% drawdown halt) and kalshi_live_bot (per-window budget, stop loss, profit
take, position tracking).
"""
import logging
from dataclasses import dataclass, field
from typing import Tuple

import config

log = logging.getLogger(__name__)


@dataclass
class RiskState:
    start_balance: float = 0.0
    session_high: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    consecutive_losses: int = 0
    market_daily_losses: dict = field(default_factory=dict)


class RiskManager:
    def __init__(self):
        self.state = RiskState()
        self.active_risk = 0.0
        self.position_risk: dict = {}

    def initialize(self, starting_balance: float):
        self.state.start_balance = starting_balance
        self.state.session_high = starting_balance
        log.info(
            "Risk: start=$%.2f | floor=%.0f%% cash | ceiling=%.0f%% exposure "
            "| %.0f%% per-trade | halt at -%.0f%%",
            starting_balance,
            config.CASH_FLOOR_PCT * 100, config.EXPOSURE_CEILING_PCT * 100,
            config.PER_TRADE_PCT * 100, config.DRAWDOWN_HALT_PCT * 100,
        )

    def update_balance(self, cash: float, exposure: float = 0.0,
                       mark_to_market_exposure: float = None):
        """Update balance tracking. Drawdown uses total portfolio value (cash + exposure).

        `exposure` is cost-basis exposure (position size × avg entry price). If
        `mark_to_market_exposure` is provided, it is used instead for drawdown
        calculation (current market value of positions), which is more accurate
        for drawdown detection.
        """
        # Use mark-to-market exposure if provided, otherwise cost-basis exposure
        effective_exposure = mark_to_market_exposure if mark_to_market_exposure is not None else exposure
        total_value = cash + effective_exposure
        if total_value <= 0:
            log.warning("⚠️  total_value=$%.2f <= 0 — skipping session_high/drawdown update", total_value)
            return
        if total_value > self.state.session_high:
            self.state.session_high = total_value

        if self.state.start_balance <= 0:
            return

        drawdown = (total_value - self.state.start_balance) / self.state.start_balance

        if drawdown <= -config.DRAWDOWN_HALT_PCT and not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = (
                f"drawdown {drawdown*100:.1f}% > -{config.DRAWDOWN_HALT_PCT*100:.0f}%"
            )
            log.warning("🛑 RISK HALT — %s. No new entries until balance recovers.",
                        self.state.halt_reason)

        if self.state.halted and total_value >= self.state.start_balance:
            self.state.halted = False
            self.state.halt_reason = ""
            log.info("✅ Risk halt lifted — balance $%.2f recovered to start $%.2f",
                     total_value, self.state.start_balance)

    def record_trade_outcome(self, won: bool, profit: float):
        """Track consecutive losses for cooldown logic (from Momentum v16)."""
        if won:
            self.state.consecutive_losses = 0
        else:
            self.state.consecutive_losses += 1
            if self.state.consecutive_losses >= 3:
                log.warning("🛑 %d consecutive losses — cooldown active",
                            self.state.consecutive_losses)

    def record_market_loss(self, ticker: str, loss: float):
        """Track per-market daily loss for risk limiting."""
        if loss <= 0:
            return
        self.state.market_daily_losses[ticker] = (
            self.state.market_daily_losses.get(ticker, 0.0) + loss
        )

    def record_unrealized_loss(self, ticker: str, unrealized_loss: float):
        """Track unrealized loss for an open position.
        
        This allows the per-market daily loss limit to account for
        currently-open losing positions before they settle.
        """
        if unrealized_loss <= 0:
            return
        # Use a separate key for unrealized losses so they can be distinguished
        # from realized losses, but still count toward the daily limit.
        key = f"{ticker}:unrealized"
        self.state.market_daily_losses[key] = (
            self.state.market_daily_losses.get(key, 0.0) + unrealized_loss
        )

    def record_realized_loss(self, ticker: str, loss: float):
        """Track realized loss for a settled position."""
        if loss <= 0:
            return
        self.state.market_daily_losses[ticker] = (
            self.state.market_daily_losses.get(ticker, 0.0) + loss
        )

    def market_loss_exceeded(self, ticker: str, max_daily_loss: float) -> bool:
        """Check if per-market daily loss limit has been exceeded.
        
        Includes both realized and unrealized losses for the ticker.
        """
        realized_loss = self.state.market_daily_losses.get(ticker, 0.0)
        unrealized_loss = self.state.market_daily_losses.get(f"{ticker}:unrealized", 0.0)
        total_loss = realized_loss + unrealized_loss
        return total_loss >= max_daily_loss

    def max_trade_dollars(self, cash: float, exposure: float,
                          resting_orders_value: float = 0.0) -> float:
        """
        Maximum we can spend on one trade right now.
        Returns 0.0 if no trades should be placed.
        """
        if self.state.halted:
            return 0.0

        # Consecutive loss cooldown (from Momentum v16)
        if self.state.consecutive_losses >= 3:
            return 0.0

        portfolio = cash + exposure
        if portfolio <= 0:
            return 0.0

        # Exposure room under ceiling
        exposure_room = max(0.0, portfolio * config.EXPOSURE_CEILING_PCT - exposure)

        # Cash room while keeping floor — subtract resting orders so we don't
        # double-spend cash already committed to open limit orders.
        floor_needed = max(config.MIN_CASH_ABSOLUTE, portfolio * config.CASH_FLOOR_PCT)
        cash_room = max(0.0, cash - resting_orders_value - floor_needed)

        # Per-trade cap
        per_trade_cap = cash * config.PER_TRADE_PCT

        return min(exposure_room, cash_room, per_trade_cap, config.MAX_TRADE_DOLLARS)

    def check(self, cash: float, exposure: float,
              trade_cost: float, ticker: str,
              total_value: float = None,
              resting_orders_value: float = 0.0) -> Tuple[bool, str]:
        """Final pre-order check. Returns (allowed, reason)."""
        if self.state.halted:
            return False, f"session halted ({self.state.halt_reason})"

        if self.state.consecutive_losses >= 3:
            return False, "consecutive loss cooldown"

        # Per-market daily loss limit check
        if self.market_loss_exceeded(ticker, config.MARKET_DAILY_LOSS_PCT):
            return False, f"market daily loss limit reached: {self.state.market_daily_losses.get(ticker, 0.0):.2f} >= {cash * config.MARKET_DAILY_LOSS_PCT:.2f}"

        max_trade = self.max_trade_dollars(cash, exposure, resting_orders_value)

        if trade_cost > max_trade:
            portfolio = cash + exposure
            return False, (
                f"risk cap: ${trade_cost:.2f} > max ${max_trade:.2f} "
                f"(cash=${cash:.2f} exposure=${exposure:.2f} "
                f"portfolio=${portfolio:.2f})"
            )

        if cash - trade_cost < config.MIN_CASH_ABSOLUTE:
            return False, f"cash floor: ${cash:.2f} - ${trade_cost:.2f} < ${config.MIN_CASH_ABSOLUTE:.2f}"

        return True, "ok"

    def summary(self, cash: float, exposure: float) -> str:
        portfolio = cash + exposure
        cash_pct = cash / portfolio * 100 if portfolio else 100
        exp_pct = exposure / portfolio * 100 if portfolio else 0
        status = "🛑HALT" if self.state.halted else "✅"
        return (
            f"{status} cash=${cash:.2f}({cash_pct:.0f}%) "
            f"exp=${exposure:.2f}({exp_pct:.0f}%) "
            f"total=${portfolio:.2f}"
        )

    def reset_daily(self):
        """Reset drawdown tracking for a new trading day.

        Clears the session high and halt flag so a drawdown halt from a
        previous day doesn't carry over. Call this at the start of each
        new trading day.
        """
        self.state.session_high = self.state.start_balance
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.consecutive_losses = 0
        # Clear both realized and unrealized per-market losses
        self.state.market_daily_losses.clear()
        log.info("✅ Risk daily reset — session_high reset to $%.2f, halt cleared, consecutive_losses cleared, market losses cleared",
                 self.state.start_balance)
