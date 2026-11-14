"""
crossvenue_edge.py — Cross-Venue Spot & Distributional Edge between Kalshi and Polymarket.

Compares Polymarket US market pricing against reference spot-derived fair value and
Kalshi implied distributions on equivalent underlying crypto assets (BTC, ETH, SOL).
Calculates the statistical deviation in cents:
    disagreement_cents = (reference_prob - poly_price) * 100.0

A positive edge means Polymarket is underpricing the outcome relative to cross-venue
fair value after fees.
"""

import bisect
import logging
import math
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Sequence, Tuple

import binance_feed
from model import gbm_probability_yes_wins

log = logging.getLogger("crossvenue_edge")

# A Kalshi sampled CDF: list of (strike, P(S_T > strike)) tuples, ascending strike.
KalshiCDF = Sequence[Tuple[float, float]]

_spot_cache: dict[str, tuple[float, float]] = {}
_vol_cache: dict[str, tuple[float, float]] = {}


def get_cached_spot(symbol: str) -> Optional[float]:
    """Fetch spot price for symbol with a 5s cache to avoid HTTP thrashing."""
    now = time.time()
    cached = _spot_cache.get(symbol)
    if cached and now - cached[0] < 5.0:
        return cached[1]
    try:
        spot = binance_feed.spot_price(f"{symbol}USDT")
        if spot is not None and spot > 0:
            _spot_cache[symbol] = (now, spot)
            return spot
    except Exception:
        pass
    return cached[1] if cached else None


def get_cached_vol(symbol: str) -> float:
    """Fetch annualized composite volatility with a 60s cache to avoid kline REST thrashing."""
    now = time.time()
    cached = _vol_cache.get(symbol)
    if cached and now - cached[0] < 60.0:
        return cached[1]
    vol = 0.60
    try:
        sigma_1m = binance_feed.composite_sigma(f"{symbol}USDT")
        if sigma_1m and sigma_1m > 0:
            vol = max(0.20, min(1.50, sigma_1m * (525600.0 ** 0.5)))
            _vol_cache[symbol] = (now, vol)
            return vol
    except Exception:
        pass
    return cached[1] if cached else 0.60


def extract_asset_and_strike(market: dict) -> tuple[Optional[str], Optional[float], Optional[datetime]]:
    """Extract symbol, strike, and expiration timestamp from Polymarket assetPriceTerms."""
    terms = market.get("assetPriceTerms") or {}
    asset_info = terms.get("asset") or {}
    symbol = (asset_info.get("symbol") or "").upper()
    if not symbol:
        return None, None, None

    strike_val = None
    strike_dict = terms.get("strike") or terms.get("priceToBeat")
    if strike_dict and isinstance(strike_dict, dict):
        try:
            strike_val = float(strike_dict.get("value", 0))
        except (ValueError, TypeError):
            pass

    end_str = terms.get("windowEnd") or market.get("endDate")
    end_dt = None
    if end_str:
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except Exception:
            pass

    return symbol, strike_val, end_dt


def evaluate_crossvenue_edge(market: dict, spot_override: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Evaluate cross-venue fair value edge for a Polymarket crypto market."""
    symbol, strike, end_dt = extract_asset_and_strike(market)
    if not symbol or not strike or not end_dt:
        return None

    now = datetime.now(timezone.utc)
    seconds_left = (end_dt - now).total_seconds()
    if seconds_left <= 0:
        return None
    minutes_left = seconds_left / 60.0

    # Get live composite spot price
    spot = spot_override if spot_override is not None else get_cached_spot(symbol)

    if spot is None or spot <= 0:
        return None

    # Derive live annualized volatility from composite 1m sigma, falling back to 0.60
    vol = get_cached_vol(symbol)

    # Reference Black-Scholes lognormal probability P(S_T >= K)
    ref_prob = gbm_probability_yes_wins(
        spot_price=spot,
        strike=strike,
        minutes_left=minutes_left,
        volatility_annualized=vol,
    )

    # Extract Polymarket outcome prices
    outcome_raw = market.get("outcomePrices") or []
    if isinstance(outcome_raw, str):
        try:
            import json
            outcome_raw = json.loads(outcome_raw)
        except Exception:
            outcome_raw = []

    long_p = None
    short_p = None
    if len(outcome_raw) == 1:
        try:
            long_p = float(outcome_raw[0])
            short_p = round(1.0 - long_p, 4)
        except (ValueError, TypeError):
            pass
    elif len(outcome_raw) >= 2:
        try:
            long_p = float(outcome_raw[0])
            short_p = float(outcome_raw[1])
        except (ValueError, TypeError):
            pass

    if long_p is None or short_p is None:
        return None

    # Calculate net deviation in cents
    edge_long = (ref_prob - long_p) * 100.0
    edge_short = ((1.0 - ref_prob) - short_p) * 100.0

    best_side = "yes" if edge_long >= edge_short else "no"
    best_edge = max(edge_long, edge_short)
    best_price = long_p if best_side == "yes" else short_p

    # Strike cushion (GBM/log form, aligned with gates.cushion_z):
    # z = |ln(S / K)| / (sigma * sqrt(t)), sigma per sqrt-minute =
    # annualized vol / sqrt(minutes per year). Large |z| = spot far from
    # strike = low pin risk near expiry.
    sigma_per_sqrt_min = vol / math.sqrt(525600.0)
    cushion_z = None
    if sigma_per_sqrt_min > 0 and minutes_left > 0:
        cushion_z = abs(math.log(spot / strike)) / (
            sigma_per_sqrt_min * math.sqrt(minutes_left)
        )

    return {
        "slug": market.get("slug", ""),
        "symbol": symbol,
        "spot": spot,
        "strike": strike,
        "minutes_left": minutes_left,
        "ref_prob": round(ref_prob, 4),
        "cushion_z": round(cushion_z, 3) if cushion_z is not None else None,
        "long_price": long_p,
        "short_price": short_p,
        "edge_long_cents": round(edge_long, 2),
        "edge_short_cents": round(edge_short, 2),
        "best_side": best_side,
        "best_price": best_price,
        "best_edge_cents": round(best_edge, 2),
    }


def kalshi_implied_prob_at_strike(kalshi_cdf: KalshiCDF, strike: float) -> Optional[float]:
    """Interpolate P(S_T > strike) from Kalshi's sampled CDF.

    `kalshi_cdf` is a list of (strike, probability) tuples ascending by strike,
    as produced by crossvenue.to_cdf(). Pure stdlib, no network. Returns None if
    the CDF is empty or the strike lies outside the sampled range (we never
    extrapolate a CDF — that invents a number).
    """
    if not kalshi_cdf:
        return None
    strikes = [s for s, _ in kalshi_cdf]
    probs = [p for _, p in kalshi_cdf]
    if strike < strikes[0] or strike > strikes[-1]:
        return None
    i = bisect.bisect_left(strikes, strike)
    if i == 0 or strikes[i] == strike:
        return probs[i]
    x0, x1 = strikes[i - 1], strikes[i]
    f0, f1 = probs[i - 1], probs[i]
    if x1 == x0:
        return f0
    return f0 + (f1 - f0) * (strike - x0) / (x1 - x0)


def evaluate_crossvenue_edge_cdf(
    market: dict,
    kalshi_cdf: KalshiCDF,
    spot_override: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Cross-venue edge of Polymarket's price vs Kalshi's implied CDF.

    Compares Polymarket's YES price at strike K against Kalshi's ladder-implied
    P(S_T > K) at the same strike:

        edge_cents = (P_kalshi(S_T > K) - P_poly(YES @ K)) * 100

    A positive edge means Polymarket is underpricing the high-certainty favorite
    relative to Kalshi's implied CDF. Unlike evaluate_crossvenue_edge, the
    reference is Kalshi's own CDF rather than a spot-implied GBM probability, so
    spot-model error and the Kalshi-vs-Polymarket settlement-reference disagreement
    are removed from the comparison.

    `kalshi_cdf` is supplied by the caller (crossvenue.to_cdf(fetch_kalshi_ladder(...)))
    so this function stays pure and network-free.

    Returns the same dict shape as evaluate_crossvenue_edge, with
    edge_source = "kalshi_cdf", or None if the market or CDF is unusable.
    """
    symbol, strike, end_dt = extract_asset_and_strike(market)
    if not symbol or not strike or not end_dt:
        return None

    now = datetime.now(timezone.utc)
    seconds_left = (end_dt - now).total_seconds()
    if seconds_left <= 0:
        return None
    minutes_left = seconds_left / 60.0

    spot = spot_override if spot_override is not None else get_cached_spot(symbol)
    if spot is None or spot <= 0:
        return None

    k_prob = kalshi_implied_prob_at_strike(kalshi_cdf, strike)
    if k_prob is None:
        return None

    outcome_raw = market.get("outcomePrices") or []
    if isinstance(outcome_raw, str):
        try:
            import json
            outcome_raw = json.loads(outcome_raw)
        except Exception:
            outcome_raw = []

    long_p = None
    short_p = None
    if len(outcome_raw) == 1:
        try:
            long_p = float(outcome_raw[0])
            short_p = round(1.0 - long_p, 4)
        except (ValueError, TypeError):
            pass
    elif len(outcome_raw) >= 2:
        try:
            long_p = float(outcome_raw[0])
            short_p = float(outcome_raw[1])
        except (ValueError, TypeError):
            pass

    if long_p is None or short_p is None:
        return None

    edge_cents = (k_prob - long_p) * 100.0
    edge_short_cents = ((1.0 - k_prob) - short_p) * 100.0

    best_side = "yes" if edge_cents >= edge_short_cents else "no"
    best_edge = max(edge_cents, edge_short_cents)
    best_price = long_p if best_side == "yes" else short_p

    return {
        "slug": market.get("slug", ""),
        "symbol": symbol,
        "spot": spot,
        "strike": strike,
        "minutes_left": minutes_left,
        "ref_prob": round(k_prob, 4),
        "long_price": long_p,
        "short_price": short_p,
        "edge_long_cents": round(edge_cents, 2),
        "edge_short_cents": round(edge_short_cents, 2),
        "best_side": best_side,
        "best_price": best_price,
        "best_edge_cents": round(best_edge, 2),
        "edge_source": "kalshi_cdf",
    }
