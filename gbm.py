"""
gbm.py — Geometric Brownian Motion probability module.

Consolidates the GBM/log-normal math from:
- gates.cushion_z (signed strike cushion in standard deviations)
- binance_feed.prob_above (P(price > strike) for zero-drift lognormal)
- model.gbm_probability_yes_wins (Black-Scholes d2 with drift)

This module provides a single source of truth for GBM probabilities with
explicit units, P = Phi(d2) for point-in-time and average-window (BRTI-style)
variants, fat-tail/jump adjustment, and calibration support.
"""

import math
from dataclasses import dataclass
from typing import Optional

# Standard normal CDF
_NORMAL_CDF = lambda z: 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

# Default annualized volatility for crypto (80%)
DEFAULT_VOLATILITY = 0.80
DEFAULT_RISK_FREE = 0.05


@dataclass
class GBMParams:
    """GBM model parameters with explicit units."""
    spot: float              # Current spot price (dollars)
    strike: float            # Strike price (dollars)
    minutes_left: float      # Time to expiry (minutes)
    sigma_per_min: float     # Per-minute log-volatility (dimensionless, e.g., 0.001 = 0.1%/min)
    drift: float = 0.0       # Drift term (per minute, can be 0 for zero-drift)
    fat_tail_kurtosis: float = 0.0  # Excess kurtosis for fat-tail adjustment (0 = normal)


def cushion_z(params: GBMParams, side: str = "yes") -> Optional[float]:
    """
    Signed GBM/log strike cushion in standard deviations.

    z = |ln(S / K)| / (sigma * sqrt(t)) is the cushion magnitude; the sign is
    positive when the chosen side is in-the-money (cushion in our favour) and
    negative when out-of-the-money.

    Args:
        params: GBMParams with spot, strike, minutes_left, sigma_per_min
        side: "yes" or "no" — for "no", the cushion is the mirror (negated)

    Returns:
        Signed z-score, or None if undefined (spot/strike/sigma/minutes <= 0)
    """
    if params.spot <= 0 or params.strike <= 0 or params.sigma_per_min <= 0:
        return None
    if params.minutes_left <= 0:
        return None
    denom = params.sigma_per_min * math.sqrt(params.minutes_left)
    if denom <= 0:
        return None
    z = math.log(params.spot / params.strike) / denom
    return z if side.lower() == "yes" else -z


def prob_above_zero_drift(params: GBMParams) -> float:
    """
    P(price > strike) for a zero-drift lognormal walk (binance_feed.prob_above).

    This is the spot-implied fair value we compare both venues against.
    Near expiry the lognormal is a poor description of a single print, so treat
    the result as a reference, not a truth.

    Args:
        params: GBMParams with spot, strike, minutes_left, sigma_per_min

    Returns:
        Probability in [0, 1], or NaN if inputs invalid
    """
    if params.spot <= 0 or params.strike <= 0 or params.sigma_per_min <= 0 or params.minutes_left <= 0:
        return float("nan")
    denom = params.sigma_per_min * math.sqrt(params.minutes_left)
    if denom <= 0:
        return 1.0 if params.spot > params.strike else 0.0
    # Zero-drift: d2 = ln(S/K) / (sigma*sqrt(t)) - 0.5*sigma*sqrt(t)
    # This is equivalent to (ln(S/K) - 0.5*sigma^2*t) / (sigma*sqrt(t))
    d2 = (math.log(params.spot / params.strike) - 0.5 * denom * denom) / denom
    prob = _NORMAL_CDF(d2)
    return max(0.001, min(0.999, prob))


def gbm_probability_yes_wins(params: GBMParams,
                             volatility_annualized: float = DEFAULT_VOLATILITY,
                             risk_free_rate: float = DEFAULT_RISK_FREE) -> float:
    """
    GBM probability that spot >= strike at expiry (Black-Scholes d2).

    Uses the Black-Scholes-style d2 formula:
        P(S_T >= K) = N(d2),  d2 = (ln(S/K) + (r - 0.5*sigma^2)*T) / (sigma*sqrt(T))

    Args:
        params: GBMParams with spot, strike, minutes_left
        volatility_annualized: Annualized vol (0.80 = 80% for crypto)
        risk_free_rate: Annualized risk-free rate (5% default)

    Returns:
        Probability in [0.001, 0.999]
    """
    if params.minutes_left <= 0:
        return 1.0 if params.spot >= params.strike else 0.0
    if params.spot <= 0 or params.strike <= 0:
        return 0.5

    T = params.minutes_left / (365.0 * 24.0 * 60.0)  # years
    sigma = volatility_annualized
    if T <= 0 or sigma <= 0:
        return 1.0 if params.spot >= params.strike else 0.0
    try:
        d2 = (math.log(params.spot / params.strike) +
              (risk_free_rate - 0.5 * sigma * sigma) * T) / \
             (sigma * math.sqrt(T))
        base_prob = _NORMAL_CDF(d2)
    except (ValueError, OverflowError):
        return 0.5

    return max(0.001, min(0.999, base_prob))


def prob_above_average_window(params: GBMParams,
                               window_seconds: int = 60,
                               volatility_annualized: float = DEFAULT_VOLATILITY,
                               risk_free_rate: float = DEFAULT_RISK_FREE) -> float:
    """
    P(average_price > strike) for BRTI-style average settlement.

    For a 60-second VWAP/TWAP, the distribution of the average is tighter than
    the terminal distribution. The variance of the average over window W is
    sigma^2 * (T - W/3) for T >> W, or approximately sigma^2 * T * (1 - W/(3T)).

    This is an approximation; for precise pricing use numerical integration.
    """
    if params.minutes_left <= 0:
        return 1.0 if params.spot >= params.strike else 0.0

    T_min = params.minutes_left
    W_min = window_seconds / 60.0
    if T_min <= W_min:
        # Window covers entire remaining time: use terminal distribution
        return gbm_probability_yes_wins(params, volatility_annualized, risk_free_rate)

    # Effective time is reduced by window smoothing
    # Variance of average over [T-W, T] is sigma^2 * (T - W/3)
    # For T >> W, this is approximately sigma^2 * T * (1 - W/(3T))
    effective_T = T_min * (1.0 - W_min / (3.0 * T_min))
    if effective_T <= 0:
        return 1.0 if params.spot >= params.strike else 0.0

    T_years = T_min / (365.0 * 24.0 * 60.0)
    effective_T_years = effective_T / (365.0 * 24.0 * 60.0)
    sigma = volatility_annualized

    try:
        d2 = (math.log(params.spot / params.strike) +
              (risk_free_rate - 0.5 * sigma * sigma) * effective_T_years) / \
             (sigma * math.sqrt(effective_T_years))
        prob = _NORMAL_CDF(d2)
    except (ValueError, OverflowError):
        return 0.5

    return max(0.001, min(0.999, prob))


def prob_above_fat_tail(params: GBMParams,
                        kurtosis: float = 0.0,
                        volatility_annualized: float = DEFAULT_VOLATILITY,
                        risk_free_rate: float = DEFAULT_RISK_FREE) -> float:
    """
    P(price > strike) with fat-tail/jump adjustment via Cornish-Fisher expansion.

    For excess kurtosis > 0 (fat tails), the CDF is adjusted:
        z_adj = z + (z^2 - 1) * kurtosis / 6 + (z^3 - 3z) * kurtosis^2 / 24 + ...

    This is an approximation; for production use a proper jump-diffusion model.
    """
    base_prob = gbm_probability_yes_wins(params, volatility_annualized, risk_free_rate)

    if kurtosis <= 0:
        return base_prob

    # Convert probability back to z-score
    z = _inv_normal_cdf(base_prob)

    # Cornish-Fisher adjustment for excess kurtosis
    z_adj = z + (z*z - 1) * kurtosis / 6.0

    return _NORMAL_CDF(z_adj)


def _inv_normal_cdf(p: float) -> float:
    """Inverse normal CDF (approximation via scipy.stats.norm.ppf logic)."""
    # Simple approximation for p in (0, 1)
    if p <= 0:
        return -10.0
    if p >= 1:
        return 10.0
    # Use the inverse error function approximation
    # For better precision, scipy would be needed; this is adequate for adjustments
    a = math.sqrt(2.0) * math.erfinv(2.0 * p - 1.0)
    return a


# Backward-compatible helpers (delegating to gates/binance_feed patterns)


def cushion_z_legacy(spot: float, strike: float, sigma: float, minutes: float,
                     side: str) -> Optional[float]:
    """Legacy signature: cushion_z(spot, strike, sigma, minutes, side)."""
    params = GBMParams(spot=spot, strike=strike, minutes_left=minutes, sigma_per_min=sigma)
    return cushion_z(params, side)


def prob_above_legacy(spot: float, strike: float, sigma_min: float,
                      minutes_left: float) -> float:
    """Legacy signature: prob_above(spot, strike, sigma_min, minutes_left)."""
    params = GBMParams(spot=spot, strike=strike, minutes_left=minutes_left, sigma_per_min=sigma_min)
    return prob_above_zero_drift(params)


if __name__ == "__main__":
    # Quick smoke test
    params = GBMParams(spot=100000.0, strike=95000.0, minutes_left=8.0, sigma_per_min=0.001)
    print(f"cushion_z(yes): {cushion_z(params, 'yes'):.4f}")
    print(f"cushion_z(no): {cushion_z(params, 'no'):.4f}")
    print(f"prob_above_zero_drift: {prob_above_zero_drift(params):.6f}")
    print(f"gbm_probability_yes_wins: {gbm_probability_yes_wins(params):.6f}")
    print(f"prob_above_average_window (60s): {prob_above_average_window(params):.6f}")
    print(f"prob_above_fat_tail (kurtosis=3): {prob_above_fat_tail(params, kurtosis=3.0):.6f}")