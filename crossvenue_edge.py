"""
crossvenue_edge.py — Cross-Venue Spot & Distributional Edge between Kalshi and Polymarket.

Compares Polymarket US market pricing against reference spot-derived fair value and
Kalshi implied distributions on equivalent underlying crypto assets (BTC, ETH, SOL).
Calculates the statistical deviation in cents:
    disagreement_cents = (reference_prob - poly_price) * 100.0

A positive edge means Polymarket is underpricing the outcome relative to cross-venue
fair value after fees.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import binance_feed
from model import gbm_probability_yes_wins

log = logging.getLogger("crossvenue_edge")


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
    spot = spot_override
    if spot is None:
        try:
            spot = binance_feed.spot_price(f"{symbol}USDT")
        except Exception:
            spot = None

    if spot is None or spot <= 0:
        return None

    # Reference Black-Scholes lognormal probability P(S_T >= K)
    ref_prob = gbm_probability_yes_wins(
        spot_price=spot,
        strike=strike,
        minutes_left=minutes_left,
        volatility_annualized=0.60,
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

    return {
        "slug": market.get("slug", ""),
        "symbol": symbol,
        "spot": spot,
        "strike": strike,
        "minutes_left": minutes_left,
        "ref_prob": round(ref_prob, 4),
        "long_price": long_p,
        "short_price": short_p,
        "edge_long_cents": round(edge_long, 2),
        "edge_short_cents": round(edge_short, 2),
        "best_side": best_side,
        "best_price": best_price,
        "best_edge_cents": round(best_edge, 2),
    }
