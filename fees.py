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
# Validated immediately at import (and again at startup via validate_fee_rate()).
_FEE_RATE_STR = os.environ.get("KALSHI_FEE_RATE", "0.07")
try:
    FEE_RATE = float(_FEE_RATE_STR)
except ValueError:
    log.warning("Invalid KALSHI_FEE_RATE='%s', using default 0.07", _FEE_RATE_STR)
    FEE_RATE = 0.07
# Bounds check immediately after parse (SF8)
# reviewed: clamping is defensive; a misconfigured env var must not crash or allow fee > 100%
if not 0 <= FEE_RATE <= 1:
    log.warning("KALSHI_FEE_RATE=%.4f out of bounds [0,1], clamping to 0.07", FEE_RATE)
    FEE_RATE = 0.07


def validate_fee_rate():
    """Validate FEE_RATE at startup. Call from trader.main() before trading."""
    if not 0 <= FEE_RATE <= 1:
        raise ValueError(f"KALSHI_FEE_RATE={FEE_RATE} must be in [0, 1]")


# ── Per-series fee models (B6) ──────────────────────────────────────────────
# Kalshi exposes fee_type and fee_multiplier per series (GET /series). A series
# whose fee_type includes maker fees (quadratic_with_maker_fees) charges resting
# orders a maker fee coefficient (0.0175) when they FILL — never on cancel.
# Standard series (no entry or an unknown-to-us entry) use the plain taker model
# with multiplier 1.0, unchanged from before.
MAKER_FEE_RATE = 0.0175

_SERIES_FEES: dict = {}

_KNOWN_FEE_TYPES = {"", "none", "no_fee", "quadratic", "quadratic_with_maker_fees"}


def series_from_ticker(ticker: str) -> str:
    """Series ticker for a market ticker (KXBTC15M-26NOV15-AB -> KXBTC15M)."""
    return (ticker or "").split("-", 1)[0].upper()


def set_series_fees(models) -> None:
    """Replace the series fee cache from {series: {fee_type, fee_multiplier}}."""
    from config import MAX_FEE_MULTIPLIER
    _SERIES_FEES.clear()
    for series, info in (models or {}).items():
        if not series:  # reviewed: skip catalogue entries with no series name — they carry no fee info (B6)
            continue
        try:
            mult = float((info or {}).get("fee_multiplier", 1.0) or 1.0)
        except (TypeError, ValueError):
            mult = 1.0
        # Cap runaway multipliers from the API
        # reviewed: clamping prevents a runaway API multiplier from blowing up fee math
        if mult > MAX_FEE_MULTIPLIER:
            log.warning("Series %s fee_multiplier=%.2f exceeds MAX_FEE_MULTIPLIER=%d, clamping",
                        series, mult, MAX_FEE_MULTIPLIER)
            mult = float(MAX_FEE_MULTIPLIER)
        fee_type = str((info or {}).get("fee_type", "") or "")
        _SERIES_FEES[str(series).upper()] = {"fee_type": fee_type,
                                             "fee_multiplier": mult}


def clear_series_fees() -> None:
    _SERIES_FEES.clear()


def series_fee_info(series: str | None):
    """Cached {fee_type, fee_multiplier} for a series, or None (standard model)."""
    if not series:  # reviewed: no series argument means "standard model" — never guess a multiplier
        return None
    return _SERIES_FEES.get(str(series).upper())


def taker_multiplier(series: str | None = None) -> float:
    info = series_fee_info(series)
    return float(info["fee_multiplier"]) if info else 1.0


def series_has_maker_fees(fee_type) -> bool:
    return "maker" in str(fee_type or "").lower()


def require_known_fee_type(series, fee_type, live: bool = True) -> None:
    """Refuse a live start on an unknown fee_type; dry runs only warn (B6)."""
    t = str(fee_type or "").strip().lower()
    if t in _KNOWN_FEE_TYPES:  # reviewed: empty/quadratic/quadratic_with_maker_fees/no_fee are the modelled types; anything else is not (B6)
        return
    msg = f"Unknown Kalshi fee_type {fee_type!r} for series {series}"
    if live:  # reviewed: unknown fee type on a live start refuses — the venue's price is not modelled, so sizing is unpriced (B6)
        raise ValueError(msg + " — refusing live start (cannot price fees safely)")
    log.warning(msg + " — continuing with the standard taker model (dry run)")


def maker_fee_dollars(contracts: float, price_dollars: float, series: str | None = None) -> float:
    """Maker fill fee in dollars (quadratic, 0.0175 coefficient x multiplier).

    Charged by the venue when a resting order FILLS on a maker-fee series —
    never on cancel, and never on standard series. 0 contracts -> 0.0.
    """
    if contracts <= 0 or price_dollars <= 0:  # reviewed: zero contracts/price books zero fee — a cancelled order pays nothing (B6)
        return 0.0
    info = series_fee_info(series)
    if not info or not series_has_maker_fees(info.get("fee_type")):  # reviewed: maker fee only on series whose fee_type includes maker fees — standard series stay free for resting orders (B6)
        return 0.0
    raw = (MAKER_FEE_RATE * float(info.get("fee_multiplier", 1.0))
           * contracts * price_dollars * (1.0 - price_dollars))
    return math.ceil((raw + 1e-12) * 1_000_000) / 1_000_000


def taker_fee_dollars(contracts: float, price_dollars: float, multiplier: float = 1.0,
                      series: str | None = None) -> float:
    """Raw trade fee, rounded up to 6 decimal places (no balance-grid rounding).

    ``series`` applies the series' fee_multiplier on top of ``multiplier``
    (B6); with no series data the standard model (multiplier 1.0) applies.
    """
    if contracts <= 0 or price_dollars <= 0:
        return 0.0
    if series is not None:  # reviewed: unknown series keep multiplier 1.0 — standard taker model unchanged (B6)
        multiplier = multiplier * taker_multiplier(series)
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