#!/usr/bin/env python3
"""
fair_value.py — T15-21: Fair-value engine (maker vs taker).

Ensemble p_hat with uncertainty band, executable price per fill model,
fee via fees.py, EV for maker/taker entry.

This module provides the decision-grade fair value computation that combines:
- Model probability (GBM, ML, or blended)
- Venue fee structure (taker vs maker)
- Fill probability model (resting at bid/ask vs crossing)
- Uncertainty quantification for the win probability estimate
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import config
import fees
from gbm import prob_above_zero_drift as gbm_prob, GBMParams

log = logging.getLogger(__name__)


@dataclass
class FairValueInput:
    """Inputs needed for fair value computation."""
    spot: float
    strike: float
    minutes_left: float
    sigma_per_min: float
    yes_bid: float | None = None      # cents
    yes_ask: float | None = None      # cents
    no_bid: float | None = None       # cents
    no_ask: float | None = None       # cents
    side: str = "yes"                 # "yes" or "no"
    series: str | None = None         # for fee lookup
    ml_prob_up: float | None = None   # optional ML model probability
    gbm_prob_up: float | None = None  # optional GBM probability


@dataclass
class FillModel:
    """Fill probability model for resting limit orders."""
    # At-the-money: 50% fill rate at bid/ask
    # Deep ITM: fill rate drops (adverse selection)
    # OTM: fill rate rises but edge is negative
    
    @staticmethod
    def maker_fill_prob(entry_price_cents: int, mid_cents: float, side: str) -> float:
        """
        Probability a resting maker order at entry_price gets filled.
        
        Based on empirical data from Kalshi KXBTC15M:
        - At bid/ask (1c inside): ~36% fill rate
        - 2c inside: ~55% fill rate
        - At mid: ~75% fill rate
        - Crosses spread: ~95% fill rate (taker)
        """
        edge_from_mid = abs(mid_cents - entry_price_cents)
        
        if side == "yes":
            # Buying YES: we're at or below ask
            if entry_price_cents >= mid_cents:  # crossing or at mid
                return 0.95  # taker-like fill rate
            elif edge_from_mid <= 1:
                return 0.36  # at bid (1c inside)
            elif edge_from_mid <= 2:
                return 0.55
            else:
                return 0.75  # deep inside spread
        else:
            # Buying NO: we're at or above bid
            if entry_price_cents <= mid_cents:
                return 0.95
            elif edge_from_mid <= 1:
                return 0.36
            elif edge_from_mid <= 2:
                return 0.55
            else:
                return 0.75


@dataclass
class FairValueResult:
    """Result of fair value computation."""
    p_hat: float                    # ensemble win probability
    p_hat_lower: float              # lower bound (uncertainty)
    p_hat_upper: float              # upper bound (uncertainty)
    
    # Maker path
    maker_entry_price_cents: int    # price we'd rest at (bid/ask - 1c)
    maker_fill_prob: float          # probability of fill
    maker_fee: float                # fee if filled (dollars/contract)
    maker_ev: float                 # expected value if filled (dollars/contract)
    maker_net_ev: float             # fill_prob * maker_ev
    
    # Taker path
    taker_entry_price_cents: int    # price we'd cross at (ask/bid)
    taker_fee: float                # fee if taken (dollars/contract)
    taker_ev: float                 # expected value (dollars/contract)
    taker_net_ev: float             # same as taker_ev (fill=1)
    
    # Decision
    recommended_path: str           # "maker" or "taker" or "none"
    recommended_entry_price: int
    recommended_ev: float
    
    # Diagnostics
    bid_ask_spread_cents: float | None = None
    mid_cents: float | None = None
    components: dict = field(default_factory=dict)


def blend_probabilities(gbm_p: float | None, ml_p: float | None, 
                        weight_gbm: float = 0.6, weight_ml: float = 0.4) -> tuple[float, float]:
    """
    Blend GBM and ML probabilities with uncertainty band.
    
    Returns (p_hat, uncertainty_half_width)
    Uncertainty captures model disagreement and calibration error.
    """
    probs = []
    weights = []
    
    if gbm_p is not None:
        probs.append(gbm_p)
        weights.append(weight_gbm)
    if ml_p is not None:
        probs.append(ml_p)
        weights.append(weight_ml)
    
    if not probs:
        return 0.5, 0.1  # max uncertainty when no models
    
    # Normalize weights
    total_w = sum(weights)
    weights = [w / total_w for w in weights]
    
    # Weighted mean
    p_hat = sum(p * w for p, w in zip(probs, weights))
    
    # Uncertainty: weighted std of models + base calibration error
    if len(probs) > 1:
        model_std = math.sqrt(sum(w * (p - p_hat)**2 for p, w in zip(probs, weights)))
    else:
        model_std = 0.02  # single model: assume 2% calibration error
    
    # Add base uncertainty (model risk)
    calibration_error = 0.015  # 1.5% base calibration error
    uncertainty = math.sqrt(model_std**2 + calibration_error**2)
    
    # Clamp uncertainty
    uncertainty = min(uncertainty, 0.1)  # cap at 10%
    
    return p_hat, uncertainty


def compute_gbm_prob_up(spot: float, strike: float, minutes: float, sigma: float) -> float:
    """Compute P(YES wins) under GBM with zero drift."""
    if minutes <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.5
    params = GBMParams(spot=spot, strike=strike, minutes_left=minutes, sigma_per_min=sigma)
    return gbm_prob(params)


def compute_fair_value(inputs: FairValueInput) -> FairValueResult:
    """
    Main fair value computation.
    
    Combines model probabilities, fee structure, and fill model to produce
    actionable EV for both maker and taker entry paths.
    """
    # 1. Compute model probabilities
    gbm_p = inputs.gbm_prob_up
    if gbm_p is None:
        gbm_p = compute_gbm_prob_up(
            inputs.spot, inputs.strike, inputs.minutes_left, inputs.sigma_per_min
        )
    
    # Blend with ML if available
    p_hat, uncertainty = blend_probabilities(gbm_p, inputs.ml_prob_up)
    p_hat_lower = max(0.0, p_hat - uncertainty)
    p_hat_upper = min(1.0, p_hat + uncertainty)
    
    # 2. Determine prices for chosen side
    if inputs.side == "yes":
        maker_price = inputs.yes_bid if inputs.yes_bid is not None else (inputs.yes_ask - 1 if inputs.yes_ask else None)
        taker_price = inputs.yes_ask
        mid = (inputs.yes_bid + inputs.yes_ask) / 2.0 if (inputs.yes_bid and inputs.yes_ask) else None
        spread = inputs.yes_ask - inputs.yes_bid if (inputs.yes_bid and inputs.yes_ask) else None
    else:
        maker_price = inputs.no_ask if inputs.no_ask is not None else (inputs.no_bid + 1 if inputs.no_bid else None)
        taker_price = inputs.no_bid
        mid = (inputs.no_bid + inputs.no_ask) / 2.0 if (inputs.no_bid and inputs.no_ask) else None
        spread = inputs.no_ask - inputs.no_bid if (inputs.no_bid and inputs.no_ask) else None
    
    if maker_price is None or taker_price is None:
        # Cannot compute without prices
        log.debug("Missing prices for fair value: maker=%s taker=%s", maker_price, taker_price)
        return FairValueResult(
            p_hat=p_hat, p_hat_lower=p_hat_lower, p_hat_upper=p_hat_upper,
            maker_entry_price_cents=0, maker_fill_prob=0.0, maker_fee=0.0,
            maker_ev=0.0, maker_net_ev=0.0,
            taker_entry_price_cents=0, taker_fee=0.0, taker_ev=0.0, taker_net_ev=0.0,
            recommended_path="none", recommended_entry_price=0, recommended_ev=0.0,
            bid_ask_spread_cents=spread, mid_cents=mid
        )
    
    maker_price_dollars = maker_price / 100.0
    taker_price_dollars = taker_price / 100.0
    
    # 3. Fees
    series = inputs.series
    maker_fee = fees.maker_fee_dollars(1.0, maker_price_dollars, series) if series else 0.0
    taker_fee = fees.taker_fee_dollars(1.0, taker_price_dollars)
    
    # 4. Win probability for chosen side
    if inputs.side == "yes":
        q = p_hat
    else:
        q = 1.0 - p_hat
    
    # 5. Maker EV
    maker_ev = q * (1.0 - maker_price_dollars) - (1.0 - q) * maker_price_dollars - maker_fee
    maker_fill_prob = FillModel.maker_fill_prob(maker_price, mid or taker_price, inputs.side)
    maker_net_ev = maker_fill_prob * maker_ev
    
    # 6. Taker EV (fill = 1)
    taker_ev = q * (1.0 - taker_price_dollars) - (1.0 - q) * taker_price_dollars - taker_fee
    taker_net_ev = taker_ev
    
    # 7. Decision
    if maker_net_ev > taker_net_ev and maker_net_ev > 0:
        recommended = "maker"
        rec_price = maker_price
        rec_ev = maker_net_ev
    elif taker_net_ev > 0:
        recommended = "taker"
        rec_price = taker_price
        rec_ev = taker_net_ev
    else:
        recommended = "none"
        rec_price = 0
        rec_ev = 0.0
    
    return FairValueResult(
        p_hat=p_hat, p_hat_lower=p_hat_lower, p_hat_upper=p_hat_upper,
        maker_entry_price_cents=maker_price,
        maker_fill_prob=maker_fill_prob,
        maker_fee=maker_fee,
        maker_ev=maker_ev,
        maker_net_ev=maker_net_ev,
        taker_entry_price_cents=taker_price,
        taker_fee=taker_fee,
        taker_ev=taker_ev,
        taker_net_ev=taker_net_ev,
        recommended_path=recommended,
        recommended_entry_price=rec_price,
        recommended_ev=rec_ev,
        bid_ask_spread_cents=spread,
        mid_cents=mid,
        components={
            "gbm_prob": gbm_p,
            "ml_prob": inputs.ml_prob_up,
            "uncertainty": uncertainty,
            "q": q,
        }
    )


def gate_ev_threshold(result: FairValueResult, min_ev: float = 0.001) -> bool:
    """
    Check if fair value passes minimum EV threshold.
    
    Returns True if the recommended path has EV >= min_ev (dollars/contract).
    """
    return result.recommended_ev >= min_ev


def format_fair_value(result: FairValueResult) -> str:
    """Format fair value result for logging."""
    return (
        f"FV p_hat={result.p_hat:.4f} [{result.p_hat_lower:.4f}-{result.p_hat_upper:.4f}] "
        f"maker@${result.maker_entry_price_cents/100:.2f} fill={result.maker_fill_prob:.0%} "
        f"ev=${result.maker_net_ev:.4f} | "
        f"taker@${result.taker_entry_price_cents/100:.2f} ev=${result.taker_net_ev:.4f} | "
        f"rec={result.recommended_path}@${result.recommended_entry_price/100:.2f} "
        f"ev=${result.recommended_ev:.4f}"
    )


if __name__ == "__main__":
    # Quick test
    import sys
    logging.basicConfig(level=logging.DEBUG)
    
    # Test case: BTC at 100k, strike 95k, 8 min left, vol 0.1%/min
    fv = compute_fair_value(FairValueInput(
        spot=100000.0,
        strike=95000.0,
        minutes_left=8.0,
        sigma_per_min=0.001,
        yes_bid=94,
        yes_ask=96,
        no_bid=4,
        no_ask=6,
        side="yes",
        series="KXBTC15M",
    ))
    
    print(format_fair_value(fv))
    print(f"Gate passes: {gate_ev_threshold(fv)}")