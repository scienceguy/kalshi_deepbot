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
import math
import os

# Kalshi taker fee rate (0.07 = 7% of contracts * price * (1-price)).
# Configurable via KALSHI_FEE_RATE env var in case the fee schedule changes.
FEE_RATE = float(os.environ.get("KALSHI_FEE_RATE", "0.07"))


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


def buy_cash_out(contracts: float, price_dollars: float, multiplier: float = 1.0) -> float:
    """Exact cash leaving the balance for a buy, per the rounding rules."""
    cost = contracts * price_dollars
    fee = taker_fee_dollars(contracts, price_dollars, multiplier)
    return math.ceil((cost + fee) * 100 - 1e-9) / 100.0


def sell_cash_in(contracts: float, price_dollars: float, multiplier: float = 1.0) -> float:
    """Exact cash credited for a sell (revenue minus fee, floored to the cent grid)."""
    proceeds = contracts * price_dollars
    fee = taker_fee_dollars(contracts, price_dollars, multiplier)
    return math.floor((proceeds - fee) * 100 + 1e-9) / 100.0


def net_profit_roundtrip(qty: int, entry_cents: int, exit_cents: int) -> tuple:
    """Round-trip P&L using Kalshi's quadratic taker fee (rounded up per leg).

    Returns (profit_dollars, profit_pct).
    """
    entry_value = qty * entry_cents / 100.0
    exit_value = qty * exit_cents / 100.0
    fee = (taker_fee_cents(qty, entry_cents / 100.0)
           + taker_fee_cents(qty, exit_cents / 100.0)) / 100.0
    profit = exit_value - entry_value - fee
    profit_pct = (profit / entry_value * 100.0) if entry_value else 0.0
    return profit, profit_pct