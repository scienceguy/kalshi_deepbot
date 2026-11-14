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


def _paper_guards_relaxed() -> bool:
    """True when a DRY_RUN runs with the paper guards switched off.

    # reviewed: paper-only relaxation (config.PAPER_GUARDS_ENABLED); the live
    # path never takes this branch because config.DRY_RUN is False there, so
    # live halt/cooldown/market-loss behaviour is unchanged.
    """
    return bool(config.DRY_RUN) and not bool(config.PAPER_GUARDS_ENABLED)


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

        # Measure from the peak, not the opening balance: measuring from the
        # start meant profits could be given back entirely before the halt
        # fired, which is the drawdown this is supposed to stop.
        peak = max(self.state.session_high, self.state.start_balance)
        drawdown = (total_value - peak) / peak

        # Paper experiments may raise the halt threshold (PAPER_DRAWDOWN_HALT_PCT)
        # so a losing streak cannot deadlock the run. # reviewed: paper-only
        # threshold swap, live keeps config.DRAWDOWN_HALT_PCT.
        halt_pct = (config.PAPER_DRAWDOWN_HALT_PCT if _paper_guards_relaxed()
                    else config.DRAWDOWN_HALT_PCT)

        if drawdown <= -halt_pct and not self.state.halted:  # reviewed: halt_pct is paper-swapped above, live unchanged
            self.state.halted = True
            self.state.halt_reason = (
                f"drawdown {drawdown*100:.1f}% > -{halt_pct*100:.0f}%"
            )
            log.warning("🛑 RISK HALT — %s. No new entries until balance recovers.",
                        self.state.halt_reason)

        # Resume once the drawdown has halved, not only at a full new high —
        # requiring the old peak would leave the bot halted indefinitely.
        if self.state.halted and drawdown > -halt_pct / 2:  # reviewed: matches the halt_pct used to enter the halt
            self.state.halted = False
            self.state.halt_reason = ""
            log.info("✅ Risk halt lifted — drawdown recovered to %.1f%% (peak $%.2f)",
                     drawdown * 100, peak)
        # reviewed: clear false risk halt from settlement race when cash exceeds start balance with 0 exposure
        elif self.state.halted and effective_exposure <= 1e-6 and cash >= self.state.start_balance:
            self.state.session_high = cash
            self.state.halted = False
            self.state.halt_reason = ""
            log.info("✅ Risk halt cleared — cash $%.2f >= start $%.2f with 0 exposure (settlement spike resolved)",
                     cash, self.state.start_balance)

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

        Stores the *current* unrealized loss, not a running total. Adding on
        every cycle turned a position 5c under water into a 10c+ "daily loss"
        within four minutes and blocked the market for the rest of the day.
        A recovered position clears its entry.
        """
        key = f"{ticker}:unrealized"
        if unrealized_loss <= 0:
            self.state.market_daily_losses.pop(key, None)
            return
        self.state.market_daily_losses[key] = unrealized_loss

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
                          resting_orders_value: float = 0.0,
                          paper: bool = False,
                          mark_to_market_exposure: float = None) -> float:
        """
        Maximum we can spend on one trade right now.
        Returns 0.0 if no trades should be placed.

        paper=True uses the dry-run knobs (see config.PAPER_*): a paper run
        must be able to buy one 75-95c favourite contract or it never trades
        and never compounds.

        If mark_to_market_exposure is provided, it's used for the exposure
        ceiling check (more accurate than cost-basis). The cost-basis exposure
        is still used for per-trade cap calculations which are cash-based.
        """
        # Paper-only guard bypass (PAPER_GUARDS_ENABLED=false); live keeps the
        # drawdown halt and the consecutive-loss cooldown.
        if not _paper_guards_relaxed():  # reviewed: bypass is paper-only, live always runs the guards
            if self.state.halted:  # reviewed: live drawdown halt blocks all sizing
                return 0.0

            # Consecutive loss cooldown (from Momentum v16)
            if self.state.consecutive_losses >= 3:  # reviewed: live cooling-off after 3 straight losses
                return 0.0

        portfolio = cash + exposure
        if portfolio <= 0:
            return 0.0

        floor_pct = config.PAPER_CASH_FLOOR_PCT if paper else config.CASH_FLOOR_PCT
        min_cash = config.PAPER_MIN_CASH_ABSOLUTE if paper else config.MIN_CASH_ABSOLUTE
        ceiling_pct = (config.PAPER_EXPOSURE_CEILING_PCT if paper
                       else config.EXPOSURE_CEILING_PCT)
        per_trade_pct = config.PAPER_PER_TRADE_PCT if paper else config.PER_TRADE_PCT

        # Exposure room under ceiling — use mark-to-market if available for
        # accuracy. When mtm is unavailable (None), skip the ceiling instead of
        # silently using cost-basis exposure: cost-basis is stale after a move,
        # so it can over- or under-tighten. The cash-floor and per-trade caps
        # still bind, so a missing mtm never becomes an unbounded trade.
        # reviewed: skipping the ceiling when mtm is unknown is a deliberate
        # conservative-default; cash_room and per_trade_cap still limit size.
        # reviewed: mtm branch uses mark-to-market ceiling when it is present
        # Micro-balance bootstrap: on bankrolls under $2.00, reserving a 70% cash
        # floor and 30% exposure ceiling leaves <$0.35, which cannot purchase a single
        # contract (min 1x @ 80-95c). Allow buying 1 contract when bankroll < $2.00 and no exposure exists.
        # reviewed: micro-balance bootstrap allows 1 single contract when bankroll is under $2.00
        if portfolio < 2.0 and exposure == 0.0 and resting_orders_value == 0.0:
            exposure_room = min(cash, 1.00)
            cash_room = cash
            per_trade_cap = min(cash, 1.00)
        else:
            # reviewed: use mark-to-market ceiling when present on standard sizing
            if mark_to_market_exposure is not None:
                exposure_room = max(0.0, portfolio * ceiling_pct - mark_to_market_exposure)
            else:
                exposure_room = float("inf")

            # Cash room while keeping floor — subtract resting orders so we don't
            # double-spend cash already committed to open limit orders.
            floor_needed = max(min_cash, portfolio * floor_pct)
            cash_room = max(0.0, cash - resting_orders_value - floor_needed)

            # Per-trade cap (based on cash, not exposure)
            per_trade_cap = cash * per_trade_pct

        return min(exposure_room, cash_room, per_trade_cap, config.MAX_TRADE_DOLLARS)

    def check(self, cash: float, exposure: float,
              trade_cost: float, ticker: str,
              total_value: float = None,
              resting_orders_value: float = 0.0,
              paper: bool = False) -> Tuple[bool, str]:
        """Final pre-order check. Returns (allowed, reason)."""
        # Paper-only guard bypass (PAPER_GUARDS_ENABLED=false); the live path
        # still enforces the halt, cooldown and per-market loss limit.
        if not _paper_guards_relaxed():  # reviewed: bypass is paper-only, live always runs the guards
            if self.state.halted:  # reviewed: live drawdown halt blocks every new order
                return False, f"session halted ({self.state.halt_reason})"

            if self.state.consecutive_losses >= 3:  # reviewed: live cooling-off after 3 straight losses
                return False, "consecutive loss cooldown"

            # Per-market daily loss limit check. The config value is a fraction of
            # total portfolio value (cash + exposure), so it scales with the full
            # account. Using cash alone makes the limit artificially tight when
            # exposure is high (cash << total_value).
            # reviewed: prefer total_value (mark-to-market) with start_balance floor for robust micro-balance sizing
            portfolio_value = max(total_value or (cash + exposure), self.state.start_balance, 1.0)
            market_limit = portfolio_value * config.MARKET_DAILY_LOSS_PCT
            if self.market_loss_exceeded(ticker, market_limit):  # reviewed: live per-market realized+unrealized loss cap
                return False, f"market daily loss limit reached: {self.state.market_daily_losses.get(ticker, 0.0):.2f} >= {market_limit:.2f}"

        max_trade = self.max_trade_dollars(cash, exposure, resting_orders_value, paper=paper)

        if trade_cost > max_trade:
            portfolio = cash + exposure
            return False, (
                f"risk cap: ${trade_cost:.2f} > max ${max_trade:.2f} "
                f"(cash=${cash:.2f} exposure=${exposure:.2f} "
                f"portfolio=${portfolio:.2f})"
            )

        min_cash = config.PAPER_MIN_CASH_ABSOLUTE if paper else config.MIN_CASH_ABSOLUTE
        if cash - trade_cost < min_cash:
            return False, f"cash floor: ${cash:.2f} - ${trade_cost:.2f} < ${min_cash:.2f}"

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

        Resets the per-day counters (order count, consecutive losses,
        per-market REALIZED losses). The drawdown peak and any active halt
        are carried over — a halt lasts until the drawdown actually recovers.
        Unrealized losses (open positions) are KEPT across the day boundary
        so an overnight position's loss accumulates over its holding period,
        not just the calendar day.
        """
        # Carry the peak and the halt across the day boundary: clearing them at
        # midnight un-halted a bot that was still deep in drawdown and threw
        # away the peak-based protection. Only per-day counters reset here.
        self.state.session_high = max(self.state.session_high, self.state.start_balance)
        self.state.consecutive_losses = 0
        # Clear ONLY realized per-market losses; unrealized (":unrealized" keys) persist
        realized_keys = [k for k in self.state.market_daily_losses if not k.endswith(":unrealized")]
        for k in realized_keys:
            del self.state.market_daily_losses[k]
        log.info("✅ Risk daily reset — peak kept at $%.2f, halt=%s, consecutive_losses cleared, market losses cleared",
                 self.state.session_high, self.state.halted)
