"""Kalshi fee math — exact model from kalshi_live_bot.

Model (see https://docs.kalshi.com/getting_started/fee_rounding):

* trade fee  = 0.07 * contracts * price * (1 - price), rounded UP to $0.000001
* rounding fee = adjustment that restores the account's balance precision
                 ($0.01 for non-direct members, $0.0001 for direct members)
* rebate = returns accumulated rounding overpayment, never below a zero net fee

For a plain buy this means the cash leaving the account is
``ceil_to_cent(price * contracts + trade_fee)`` — i.e. the effective fee is the
model fee rounded up to the next cent.
"""
import logging
import math
import os

log = logging.getLogger(__name__)


# Kalshi taker fee rate (0.07 = 7% of contracts * price * (1-price)).
# Configurable via KALSHI_FEE_RATE env var in case the fee schedule changes.
# Validated at startup via validate_fee_rate(), not at import time.
_FEE_RATE_STR = os.environ.get("KALSHI_FEE_RATE", "0.07")
try:
    FEE_RATE = float(_FEE_RATE_STR)
except ValueError:
    log.warning("Invalid KALSHI_FEE_RATE='%s', using default 0.07", _FEE_RATE_STR)
    FEE_RATE = 0.07


def validate_fee_rate():
    """Validate FEE_RATE at startup. Call from trader.main() before trading."""
    if not 0 <= FEE_RATE <= 1:
        raise ValueError(f"KALSHI_FEE_RATE={FEE_RATE} must be in [0, 1]")


def taker_fee_dollars(contracts: float, price_dollars: float, multiplier: float = 1.0) -> float:
    """Raw trade fee, rounded up to 6 decimal places (no balance-grid rounding)."""
    if contracts <= 0 or price_dollars <= 0:
        return 0.0
    raw = multiplier * FEE_RATE * contracts * price_dollars * (1.0 - price_dollars)
    return math.ceil((raw + 1e-12) * 1_000_000) / 1_000_000


def taker_fee_cents(contracts: float, price_dollars: float, multiplier: float = 1.0) -> float:
    """Effective fee in cents after the balance-precision rounding (non-direct member)."""
    raw = taker_fee_dollars(contracts, price_dollars, multiplier)
    return math.ceil(raw * 100 - 1e-9)


def buy_cash_out(contracts: float, price_dollars: float, multiplier: float = 1.0,
                 taker: bool = True) -> float:
    """Exact cash leaving the balance for a buy, per the rounding rules.

    Kalshi charges the fee to the taker only, and settlement is not a trade.
    A maker limit order therefore pays no fee: pass taker=False for our own
    posted orders and for settlement payouts.
    """
    cost = contracts * price_dollars
    fee = taker_fee_dollars(contracts, price_dollars, multiplier) if taker else 0.0
    return math.ceil((cost + fee) * 100 - 1e-9) / 100.0


def sell_cash_in(contracts: float, price_dollars: float, multiplier: float = 1.0,
                 taker: bool = True) -> float:
    """Exact cash credited for a sell (revenue minus fee, floored to the cent grid)."""
    proceeds = contracts * price_dollars
    fee = taker_fee_dollars(contracts, price_dollars, multiplier) if taker else 0.0
    return math.floor((proceeds - fee) * 100 + 1e-9) / 100.0


def net_profit_roundtrip(qty: int, entry_cents: int, exit_cents: int) -> tuple:
    """Round-trip P&L using Kalshi's quadratic taker fee (6dp raw model).

    Uses taker_fee_dollars for both legs (no cent-grid rounding), so the
    result is consistent with buy_cash_out / sell_cash_in and with the
    6-decimal-place fee model, not the balance-precision cent grid.

    Returns (profit_dollars, profit_pct).
    """
    entry_value = qty * entry_cents / 100.0
    exit_value = qty * exit_cents / 100.0
    entry_price = entry_cents / 100.0
    exit_price = exit_cents / 100.0
    fee = taker_fee_dollars(qty, entry_price) + taker_fee_dollars(qty, exit_price)
    profit = exit_value - entry_value - fee
    profit_pct = (profit / entry_value * 100.0) if entry_value else 0.0
    return profit, profit_pct