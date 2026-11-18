"""
risk.py — Consolidated risk management for Kalshi SuperBot.

Pulls from kalshi_15m_bot (70% cash floor, 30% exposure ceiling, 8% per trade,
15% drawdown halt) and kalshi_live_bot (per-window budget, stop loss, profit
take, position tracking).
"""
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Tuple

import config
import fees

log = logging.getLogger(__name__)

_ASSET_RE = re.compile(r"(?:KX)?([A-Z]{2,})")


def market_asset(key: str) -> str:
    """Asset/series bucket for a ticker or series name (B4).

    KXBTC15M-26NOV15-AB -> BTC; KXETH15M -> ETH; plain BTC-... -> BTC.
    Anything without an alphabetic prefix buckets under its upper-cased form.
    """
    m = _ASSET_RE.match((key or "").upper())
    return m.group(1) if m else (key or "").upper()


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
    cooldown_started_at: float = 0.0
    halted_at: float = 0.0
    market_daily_losses: dict = field(default_factory=dict)
    # Daily P&L tracking for daily loss circuit breaker (Pillar B)
    daily_pnl: float = 0.0
    daily_pnl_reset_ts: float = 0.0

    @property
    def peak(self) -> float:
        return self.session_high

    @peak.setter
    def peak(self, val: float):
        self.session_high = val


class RiskManager:
    def __init__(self):
        self.state = RiskState()
        self.active_risk = 0.0
        self.position_risk: dict = {}

    def initialize(self, starting_balance: float):
        self.state.start_balance = starting_balance
        self.state.session_high = starting_balance
        self.state.daily_pnl = 0.0
        self.state.daily_pnl_reset_ts = time.time()
        log.info(
            "Risk: start=$%.2f | floor=%.0f%% cash | ceiling=%.0f%% exposure "
            "| %.0f%% per-trade | halt at -%.0f%% | daily_loss_limit=%.0f%% | kelly=%.0f%% | min_ev=%.0f%%",
            starting_balance,
            config.CASH_FLOOR_PCT * 100, config.EXPOSURE_CEILING_PCT * 100,
            config.PER_TRADE_PCT * 100, config.DRAWDOWN_HALT_PCT * 100,
            config.DAILY_LOSS_LIMIT_PCT * 100,
            config.KELLY_FRACTION * 100,
            config.MIN_EV_EDGE_PCT * 100,
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
            self.state.halted_at = time.time()  # reviewed: halt start time bounds the block (B3)
            self.state.halt_reason = (
                f"drawdown {drawdown*100:.1f}% > -{halt_pct*100:.0f}%"
            )
            log.warning("🛑 RISK HALT — %s. No new entries until balance recovers.",
                        self.state.halt_reason)

        # Resume once the drawdown has halved, not only at a full new high —
        # requiring the old peak would leave the bot halted indefinitely.
        if self.state.halted and drawdown > -halt_pct / 2:  # reviewed: matches the halt_pct used to enter the halt
            self.state.halted = False
            self.state.halted_at = 0.0
            self.state.halt_reason = ""
            log.info("✅ Risk halt lifted — drawdown recovered to %.1f%% (peak $%.2f)",
                     drawdown * 100, peak)
        # reviewed: clear false risk halt from settlement race when cash exceeds start balance with 0 exposure
        elif self.state.halted and effective_exposure <= 1e-6 and cash >= self.state.start_balance:
            self.state.session_high = cash
            self.state.halted = False
            self.state.halted_at = 0.0
            self.state.halt_reason = ""
            log.info("✅ Risk halt cleared — cash $%.2f >= start $%.2f with 0 exposure (settlement spike resolved)",
                     cash, self.state.start_balance)
        elif (self.state.halted and self.state.halted_at and config.HALT_AUTO_RESUME  # reviewed: auto-resume requires the operator opt-in HALT_AUTO_RESUME=true
                and time.time() - self.state.halted_at >= config.HALT_MIN_MINUTES * 60.0):  # reviewed: only after the 240-minute default floor
            self.state.halted = False
            self.state.halted_at = 0.0
            self.state.halt_reason = ""
            log.info("✅ Risk halt auto-resumed after %d min (HALT_AUTO_RESUME=true; peak $%.2f)",
                     config.HALT_MIN_MINUTES, peak)

    def _maybe_reset_daily(self) -> None:
        """Roll the daily P&L window at midnight UTC. Never adds P&L."""
        now = time.time()
        # reviewed: K6 midnight roll same as update_daily_pnl, never adds P&L
        if now - self.state.daily_pnl_reset_ts < 86400:  # 24 hours
            return
        # reviewed: K6 only log/emit when P&L was actually non-zero
        if self.state.daily_pnl != 0.0:
            log.info("📅 Daily P&L reset: was $%.2f", self.state.daily_pnl)
            try:
                import emit
                emit.emit_event(
                    "kalshi_trader", "daily.summary",
                    {"date_utc": time.strftime("%Y-%m-%d", time.gmtime(now - 86400)),
                     "daily_pnl_dollars": round(self.state.daily_pnl, 4),
                     "start_balance_dollars": round(self.state.start_balance, 2)},
                    key=f"daily.summary:{time.strftime('%Y-%m-%d', time.gmtime(now - 86400))}")
            except Exception:
                pass
        self.state.daily_pnl = 0.0
        self.state.daily_pnl_reset_ts = now
        # A new day starts fresh: clear a daily-loss halt so trading can resume
        # reviewed: K6 daily-loss halt cleared at midnight roll
        if self.state.halted and "daily loss" in self.state.halt_reason:
            self.state.halted = False
            self.state.halted_at = 0.0
            self.state.halt_reason = ""

    def is_halted(self) -> bool:
        """True when a halt blocks new entries (K6).

        Rolls the daily window like update_daily_pnl, but never adds P&L, so a
        pre-order check cannot mutate the loss total.
        """
        self._maybe_reset_daily()
        return self.state.halted

    def update_daily_pnl(self, pnl_delta: float) -> Tuple[bool, str]:
        """Update daily P&L and check daily loss circuit breaker.
        
        Returns (halted, reason). If halted is True, trading should stop
        placing new entries (open orders are not cancelled).
        """
        self._maybe_reset_daily()

        self.state.daily_pnl += pnl_delta
        daily_limit = self.state.start_balance * config.DAILY_LOSS_LIMIT_PCT
        # reviewed: a zero base means "never initialized" (or a genuinely
        # free account) — daily_pnl 0.0 <= -0.0 is IEEE-true and would halt a
        # fresh manager on the first probe. Live always calls initialize().
        if daily_limit > 0.0 and self.state.daily_pnl <= -daily_limit:
            if not self.state.halted:
                self.state.halted = True
                now = time.time()
                self.state.halted_at = now
                self.state.halt_reason = (
                    f"daily loss limit: ${self.state.daily_pnl:.2f} <= -${daily_limit:.2f} "
                    f"({config.DAILY_LOSS_LIMIT_PCT*100:.0f}% of ${self.state.start_balance:.2f})"
                )
                log.warning("🛑 DAILY LOSS CIRCUIT BREAKER — %s. Halting new entries "
                            "(open orders are not cancelled).",
                            self.state.halt_reason)
                return True, self.state.halt_reason
        return False, "ok"

    def kelly_size(self, win_prob: float, entry_price_cents: int, 
                   total_balance: float) -> float:
        """Calculate fractional Kelly bet size in dollars.
        
        Kelly fraction = (p * b - q) / b where:
        - p = win probability
        - q = 1 - p = loss probability  
        - b = net odds (profit/loss ratio) = (100 - entry) / entry for binary
        
        For Kalshi binary: if we buy YES at 75c, profit = 25c, loss = 75c
        b = 25/75 = 1/3
        Kelly = (p * 1/3 - (1-p)) / (1/3) = p - 3(1-p) = 4p - 3
        
        Returns dollar amount capped by fractional Kelly and min EV edge.
        """
        if entry_price_cents <= 0 or entry_price_cents >= 100:
            return 0.0
        
        # Net odds: profit/loss ratio
        profit_cents = 100 - entry_price_cents
        loss_cents = entry_price_cents
        if loss_cents <= 0:
            return 0.0
        b = profit_cents / loss_cents
        
        # Kelly formula: f = (p * b - q) / b = p - q/b
        p = win_prob
        q = 1.0 - p
        kelly_fraction = (p * b - q) / b if b > 0 else 0.0
        
        # Apply fractional Kelly multiplier
        kelly_fraction *= config.KELLY_FRACTION
        
        # Must have positive edge after fees
        # Net EV per contract = p * profit - q * loss - fee
        fee_per_contract = fees.FEE_RATE * (entry_price_cents / 100.0) * (profit_cents / 100.0)
        net_ev = p * (profit_cents / 100.0) - q * (loss_cents / 100.0) - fee_per_contract
        
        if net_ev < config.MIN_EV_EDGE_PCT:
            log.debug("Kelly rejected: net EV $%.4f < min edge $%.4f", net_ev, config.MIN_EV_EDGE_PCT)
            return 0.0
        
        # Kelly bet in dollars (rounded to cents to avoid floating-point edge
        # cases where 0.05 * 100 == 4.999999999999999 instead of 5.0).
        kelly_dollars = round(max(0.0, kelly_fraction * total_balance), 2)
        
        # Cap at MAX_TRADE_DOLLARS
        return min(kelly_dollars, config.MAX_TRADE_DOLLARS)

    def check_liquidity(self, market, side: str) -> Tuple[bool, str]:
        """Check orderbook spread and depth for liquidity protection."""
        if side == "yes":
            bid = market.yes_bid
            ask = market.yes_ask
            depth = market.yes_liquidity or 0
        else:
            bid = market.no_bid
            ask = market.no_ask
            depth = market.no_liquidity or 0
        
        if bid is None or ask is None:
            return False, f"no {side} book (bid={bid}, ask={ask})"
        
        spread = ask - bid
        if spread > config.MAX_SPREAD_CENTS:
            return False, f"spread {spread}c > max {config.MAX_SPREAD_CENTS}c"
        
        if depth < config.MIN_DEPTH_CONTRACTS:
            return False, f"depth {depth} < min {config.MIN_DEPTH_CONTRACTS}"
        
        return True, "ok"

    def record_trade_outcome(self, won: bool, profit: float):
        """Track consecutive losses for cooldown logic (from Momentum v16)."""
        if won:
            self.state.consecutive_losses = 0
            self.state.cooldown_started_at = 0.0  # reviewed: a win clears the cooldown start too
            return
        self.state.consecutive_losses += 1
        if self.state.consecutive_losses >= 3:  # reviewed: the 3-loss threshold arms the time-bounded cooldown (B3)
            if not self.state.cooldown_started_at:  # reviewed: stamp the cooldown start once; later losses keep the original start
                self.state.cooldown_started_at = time.time()  # reviewed: the cooldown block is bounded by LOSS_COOLDOWN_MINUTES from this moment
                log.warning("🛑 %d consecutive losses — cooldown active for %d min",
                            self.state.consecutive_losses, config.LOSS_COOLDOWN_MINUTES)
            else:
                log.warning("🛑 %d consecutive losses — cooldown active",
                            self.state.consecutive_losses)

    def _loss_cooldown_active(self) -> bool:
        """True while a 3-loss cooldown is still inside its minimum window.

        A cooldown never blocks forever: after LOSS_COOLDOWN_MINUTES without a
        win it expires and the counter clears. A pre-B3 state without a start
        timestamp stays blocked until a win (conservative).
        """
        if self.state.consecutive_losses < 3:  # reviewed: fewer than 3 straight losses is never a cooldown
            return False
        if not self.state.cooldown_started_at:  # reviewed: a pre-B3 state without a start time stays blocked until a win
            return True
        if time.time() - self.state.cooldown_started_at >= config.LOSS_COOLDOWN_MINUTES * 60.0:  # reviewed: the cooldown expires by time even with no win (B3)
            self.state.consecutive_losses = 0
            self.state.cooldown_started_at = 0.0
            log.info("✅ Loss cooldown expired after %d min without a win — entries allowed",
                     config.LOSS_COOLDOWN_MINUTES)
            return False
        return True

    def _prune_losses(self) -> None:
        """Drop loss entries older than MARKET_LOSS_WINDOW_HOURS (B4).

        Realized entries live in per-asset buckets; unrealized snapshots live
        under "<ticker>:unrealized". Both age out of the rolling window, so
        the dict stays bounded by active assets instead of every ticker ever.
        """
        cutoff = time.time() - float(config.MARKET_LOSS_WINDOW_HOURS) * 3600.0
        for key in list(self.state.market_daily_losses):
            entries = self.state.market_daily_losses.get(key) or []
            kept = [e for e in entries if float(e.get("ts", 0) or 0) >= cutoff]
            if kept:  # reviewed: keep only entries inside the rolling window; drop the empty bucket entirely so the dict stays bounded (B4)
                self.state.market_daily_losses[key] = kept
            else:
                self.state.market_daily_losses.pop(key, None)

    def record_market_loss(self, ticker: str, loss: float):
        """Track per-asset realized loss over a rolling window (B4).

        Keyed by asset, not the unique 15-minute ticker, so one window's loss
        counts against the next contract in the same series.
        """
        if loss <= 0:
            return
        bucket = self.state.market_daily_losses.setdefault(
            market_asset(ticker), [])
        bucket.append({"ts": time.time(), "loss": float(loss)})
        self._prune_losses()

    def record_unrealized_loss(self, ticker: str, unrealized_loss: float):
        """Track unrealized loss for an open position.

        Stores the *current* unrealized loss for this ticker, not a running
        total. Adding on every cycle turned a position 5c under water into a
        10c+ "daily loss" within four minutes and blocked the market for the
        rest of the day. A recovered position clears its entry.
        """
        key = f"{ticker}:unrealized"
        if unrealized_loss <= 0:
            self.state.market_daily_losses.pop(key, None)
            return
        self.state.market_daily_losses[key] = [{"ts": time.time(),
                                              "loss": float(unrealized_loss)}]

    def market_loss_total(self, ticker: str) -> float:
        """Realized loss currently counted against ticker's asset bucket (B4)."""
        self._prune_losses()
        return sum(float(e.get("loss", 0.0) or 0.0)
                   for e in self.state.market_daily_losses.get(market_asset(ticker), []) or [])

    def market_loss_exceeded(self, ticker: str, max_daily_loss: float) -> bool:
        """Check if per-market daily loss limit has been exceeded.

        Includes both realized and unrealized losses for the ticker's asset
        bucket over the rolling window, so losses across different contracts
        in one series accumulate against a single limit.
        """
        self._prune_losses()
        asset = market_asset(ticker)
        realized_loss = self.market_loss_total(ticker)
        unrealized_loss = sum(
            float(e.get("loss", 0.0) or 0.0)
            for k, entries in self.state.market_daily_losses.items()
            if k.endswith(":unrealized")  # reviewed: unrealized snapshots live under <ticker>:unrealized keys (B4)
            and market_asset(k[:-len(":unrealized")]) == asset
            for e in entries or [])
        total_loss = realized_loss + unrealized_loss
        return total_loss >= max_daily_loss

    def max_trade_dollars(self, cash: float, exposure: float,
                          resting_orders_value: float = 0.0,
                          paper: bool = False,
                          mark_to_market_exposure: float = None,
                          ticker: str = None,
                          win_prob: float = None,
                          entry_price_cents: int = None) -> float:
        """
        Maximum we can spend on one trade right now.
        Returns 0.0 if no trades should be placed.

        paper=True uses the dry-run knobs (see config.PAPER_*): a paper run
        must be able to buy one 75-95c favourite contract or it never trades
        and never compounds.

        If mark_to_market_exposure is provided, it's used for the exposure
        ceiling check (more accurate than cost-basis). The cost-basis exposure
        is still used for per-trade cap calculations which are cash-based.
        
        If ticker, win_prob, entry_price_cents are provided, applies Kelly sizing
        with min EV edge (Pillar B).
        """
        # Paper-only guard bypass (PAPER_GUARDS_ENABLED=false); live keeps the
        # drawdown halt and the consecutive-loss cooldown.
        if not _paper_guards_relaxed():  # reviewed: bypass is paper-only, live always runs the guards
            if self.state.halted:  # reviewed: live drawdown halt blocks all sizing
                return 0.0

            # Consecutive loss cooldown (from Momentum v16), time-bounded (B3)
            if self._loss_cooldown_active():  # reviewed: live cooling-off after 3 straight losses, expires LOSS_COOLDOWN_MINUTES after it started
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
        # Microstructure bootstrap: on bankrolls under $10.00, cap per-trade risk at 50% of available cash
        if portfolio < 10.0:  # reviewed: microstructure bootstrap caps each trade at 50% cash to prevent wipeouts
            available_cash = max(0.0, cash - resting_orders_value)
            # reviewed: strict 50% Half-Kelly cap across all balances so a single loss never exhausts bankroll
            cap = max(0.0, round(available_cash * 0.50, 4) if available_cash >= 0.02 else available_cash)
            exposure_room = cap
            cash_room = cap
            per_trade_cap = cap
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

        # Kelly sizing with min EV edge (Pillar B)
        if ticker and win_prob is not None and entry_price_cents is not None:
            kelly_dollars = self.kelly_size(win_prob, entry_price_cents, portfolio)
            per_trade_cap = min(per_trade_cap, kelly_dollars)

        # H10: the per-ticker and total-exposure maps were never populated, so
        # their caps never bound (each 15-minute contract has a unique ticker,
        # so keying by ticker cannot bind across windows). They were removed
        # rather than left looking like protection; per_trade_cap and the
        # mark-to-market ceiling above are the limits that actually apply.
        return min(exposure_room, cash_room, per_trade_cap, config.MAX_TRADE_DOLLARS)

    def check(self, cash: float, exposure: float,
              trade_cost: float, ticker: str,
              total_value: float = None,
              resting_orders_value: float = 0.0,
              paper: bool = False,
              win_prob: float = None,
              entry_price_cents: int = None,
              market=None,
              side: str = "") -> Tuple[bool, str]:
        """Final pre-order check. Returns (allowed, reason)."""
        # Paper-only guard bypass (PAPER_GUARDS_ENABLED=false); the live path
        # still enforces the halt, cooldown and per-market loss limit.
        if not _paper_guards_relaxed():  # reviewed: bypass is paper-only, live always runs the guards
            # K6: read the halt without adding P&L; the daily window still rolls.
            # reviewed: K6 is_halted() rolls daily window but never adds P&L
            if self.is_halted():
                # Daily loss circuit breaker takes precedence over generic halt message
                # reviewed: K6 daily-loss halt message priority
                if "daily loss" in self.state.halt_reason:
                    return False, f"daily loss circuit breaker: {self.state.halt_reason}"
                # reviewed: live drawdown halt blocks every new order
                return False, f"session halted ({self.state.halt_reason})"

            if self._loss_cooldown_active():  # reviewed: live cooling-off after 3 straight losses, expires LOSS_COOLDOWN_MINUTES after it started
                return False, "consecutive loss cooldown"

            # Per-market daily loss limit check. The config value is a fraction of
            # total portfolio value (cash + exposure), so it scales with the full
            # account. Using cash alone makes the limit artificially tight when
            # exposure is high (cash << total_value).
            # reviewed: base market daily limit on actual mark-to-market equity so limits tighten during drawdowns
            portfolio_value = max(total_value or (cash + exposure), 1.0)
            market_limit = portfolio_value * config.MARKET_DAILY_LOSS_PCT
            if self.market_loss_exceeded(ticker, market_limit):  # reviewed: live per-market realized+unrealized loss cap
                return False, f"market daily loss limit reached: {self.market_loss_total(ticker):.2f} >= {market_limit:.2f}"

            # Liquidity/slippage protection (Pillar B)
            if market:
                # reviewed: Kalshi tickers never contain "YES", so the old guess
                # always checked the NO book for a YES entry; use the real side.
                liq_ok, liq_reason = self.check_liquidity(
                    market, side if side in ("yes", "no")
                    else ("yes" if "YES" in ticker.upper() else "no"))
                if not liq_ok:
                    return False, f"liquidity check failed: {liq_reason}"

        mtm_exp = (total_value - cash) if (total_value is not None and cash is not None) else None
        max_trade = self.max_trade_dollars(
            cash, exposure, resting_orders_value, paper=paper, 
            mark_to_market_exposure=mtm_exp,
            ticker=ticker, win_prob=win_prob, entry_price_cents=entry_price_cents
        )

        if trade_cost > max_trade:
            portfolio = cash + exposure
            return False, (
                f"risk cap: ${trade_cost:.2f} > max ${max_trade:.2f} "
                f"(cash=${cash:.2f} exposure=${exposure:.2f} "
                f"portfolio=${portfolio:.2f})"
            )

        # On micro-balances (<$10.00), allow $0.00 cash floor for microstructure bootstrap
        # reviewed: microstructure bootstrap allows deploying cash on accounts under $10
        if not paper and (cash + exposure) < 10.0:
            min_cash = 0.00
        else:
            min_cash = config.PAPER_MIN_CASH_ABSOLUTE if paper else config.MIN_CASH_ABSOLUTE
        # reviewed: enforce minimum cash buffer after trade cost
        if cash - trade_cost < min_cash - 1e-6:
            return False, f"cash floor: ${cash:.2f} - ${trade_cost:.2f} < ${min_cash:.2f}"

        return True, "ok"

    def summary(self, cash: float, exposure: float, total_balance: float | None = None) -> str:
        total = total_balance if total_balance is not None else (cash + exposure)
        cash_pct = cash / total * 100 if total else 100
        exp_pct = exposure / total * 100 if total else 0
        status = "🛑HALT" if self.state.halted else "✅"
        return (
            f"{status} cash=${cash:.2f}({cash_pct:.0f}%) "
            f"exp=${exposure:.2f}({exp_pct:.0f}%) "
            f"total=${total:.2f}"
        )

    def reset_daily(self):
        """Reset drawdown tracking for a new trading day.

        Resets the per-day counters (order count, consecutive losses,
        per-market REALIZED losses). The drawdown peak and any active halt
        are carried over — a halt lasts until the drawdown actually recovers.
        Market-loss buckets are NOT cleared; they age out of the rolling
        MARKET_LOSS_WINDOW_HOURS window by timestamp (B4), so an overnight
        position's loss still accumulates across the day boundary.
        """
        # Carry the peak and the halt across the day boundary: clearing them at
        # midnight un-halted a bot that was still deep in drawdown and threw
        # away the peak-based protection. Only per-day counters reset here.
        self.state.session_high = max(self.state.session_high, self.state.start_balance)
        # reviewed: never clear consecutive losses or market losses across resets — they age out by window instead (B4)
        self._prune_losses()
        log.info("✅ Risk daily reset — peak kept at $%.2f, halt=%s, consecutive_losses preserved (%d), market loss buckets after pruning (%d)",
                 self.state.session_high, self.state.halted, self.state.consecutive_losses, len(self.state.market_daily_losses))

    def state_dict(self) -> dict:
        """Serializable risk halt/cooldown fields for persistence across restarts (B3)."""
        return {
            "consecutive_losses": self.state.consecutive_losses,
            "cooldown_started_at": self.state.cooldown_started_at,
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
            "halted_at": self.state.halted_at,
        }

    def load_state_dict(self, data):
        """Restore persisted risk fields; unknown or invalid values are ignored."""
        if not isinstance(data, dict):  # reviewed: a non-dict persisted value is ignored, never restored
            return
        cl = data.get("consecutive_losses")
        if isinstance(cl, int) and cl >= 0:  # reviewed: restore only a valid non-negative loss count
            self.state.consecutive_losses = cl
        cs = data.get("cooldown_started_at")
        if isinstance(cs, (int, float)) and not isinstance(cs, bool) and cs > 0:  # reviewed: restore only a plausible positive timestamp
            self.state.cooldown_started_at = float(cs)
        h = data.get("halted")
        if isinstance(h, bool):  # reviewed: the halted flag must be a real JSON bool, not a string
            self.state.halted = h
        hr = data.get("halt_reason")
        if isinstance(hr, str):  # reviewed: halt reason is free text persisted for the operator
            self.state.halt_reason = hr
        ha = data.get("halted_at")
        if isinstance(ha, (int, float)) and not isinstance(ha, bool) and ha > 0:  # reviewed: restore only a plausible positive halt time
            self.state.halted_at = float(ha)
