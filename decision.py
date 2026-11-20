#!/usr/bin/env python3
"""
decision.py — T15-25: Decision policy.

Combines gates, fair-value, KL health, calibration breaker into one
documented decision function.

This is the single entry point for "should we trade this signal?".
It composes:
1. Pre-entry gates (ATR, EV, time-decay, book-age, BRTI)
2. Fair-value engine (maker vs taker EV)
3. KL divergence health check (cross-venue disagreement)
4. Calibration breaker (Brier score threshold)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config
import gates
import fair_value
from kl_tools import symmetric_kl_divergence, kl_divergence

log = logging.getLogger(__name__)


@dataclass
class DecisionInput:
    """All inputs needed for the decision function."""
    # Market data
    market: object              # Kalshi/Polymarket market object
    spot_state: dict | None = None  # spot, atr_1m, sigma_per_min
    orderbook: object | None = None
    side: str | None = None     # "yes" or "no"
    entry_price_cents: float | None = None
    
    # Fair value inputs
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    ml_prob_up: float | None = None
    
    # Cross-venue (for KL)
    kalshi_mid: float | None = None
    polymarket_mid: float | None = None
    
    # Calibration
    brier_score: float | None = None
    brier_threshold: float = 0.20  # default from config


@dataclass
class DecisionResult:
    """Result of the decision function."""
    allow: bool
    reasons: list[str] = field(default_factory=list)
    
    # Gate results
    gate_result: gates.GateResult | None = None
    
    # Fair value results
    fair_value: fair_value.FairValueResult | None = None
    
    # KL divergence
    kl_divergence: float | None = None
    kl_warning: bool = False
    kl_block: bool = False
    
    # Calibration
    calibration_ok: bool = True
    calibration_reason: str = ""
    
    # Final recommendation
    recommended_path: str = "none"  # "maker", "taker", "none"
    recommended_price: int = 0
    recommended_ev: float = 0.0


def evaluate_kl_health(kalshi_mid: float | None, polymarket_mid: float | None) -> tuple[float | None, bool, bool]:
    """
    Evaluate KL divergence between Kalshi and Polymarket mid prices.
    
    Returns (kl_value, warning, block)
    - warning: KL > GATES_KL_WATCH_THRESHOLD (log but don't block)
    - block: KL > GATES_KL_BLOCK_THRESHOLD (block the trade)
    """
    if kalshi_mid is None or polymarket_mid is None:
        return None, False, False
    
    # Convert to probabilities (cents -> fraction)
    p = kalshi_mid / 100.0
    q = polymarket_mid / 100.0
    
    # Clamp to avoid inf at boundaries
    eps = 1e-6
    p = max(eps, min(1.0 - eps, p))
    q = max(eps, min(1.0 - eps, q))
    
    try:
        kl = symmetric_kl_divergence(p, q)
    except (ValueError, ZeroDivisionError):
        return None, False, False
    
    warning_thresh = getattr(config, "GATES_KL_WATCH_THRESHOLD", 0.05)
    block_thresh = getattr(config, "GATES_KL_BLOCK_THRESHOLD", 0.15)
    
    warning = kl > warning_thresh
    block = kl > block_thresh
    
    if warning:
        log.warning("KL divergence %.4f > watch %.4f (Kalshi=%.2f%%, Poly=%.2f%%)",
                    kl, warning_thresh, kalshi_mid, polymarket_mid)
    if block:
        log.error("KL divergence %.4f > block %.4f — BLOCKING trade",
                  kl, block_thresh)
    
    return kl, warning, block


def evaluate_calibration(brier_score: float | None, threshold: float = 0.20) -> tuple[bool, str]:
    """
    Evaluate model calibration via Brier score.
    
    Returns (ok, reason)
    - Brier > threshold means model is miscalibrated
    - Default threshold 0.20 (random guessing on 50/50 is 0.25)
    """
    if brier_score is None:
        return True, "no brier score available (skip)"
    
    if brier_score > threshold:
        return False, f"Brier {brier_score:.4f} > threshold {threshold:.4f} (miscalibrated)"
    
    return True, f"Brier {brier_score:.4f} <= {threshold:.4f} (calibrated)"


def make_decision(inputs: DecisionInput) -> DecisionResult:
    """
    Single decision function combining all checks.
    
    Order of evaluation (short-circuit on hard blocks):
    1. Pre-entry gates (ATR, EV, time-decay, book-age, BRTI)
    2. Fair-value engine (maker vs taker EV)
    3. KL divergence health check
    4. Calibration breaker
    
    Returns DecisionResult with allow=True/False and detailed reasons.
    """
    reasons: list[str] = []
    result = DecisionResult(allow=False, reasons=[])
    
    # ── 1. Pre-entry gates ──────────────────────────────────────────────
    if config.GATES_ENABLED:
        gate_res = gates.evaluate_entry(
            inputs.market,
            spot_state=inputs.spot_state,
            orderbook=inputs.orderbook,
            side=inputs.side,
            entry_price_cents=inputs.entry_price_cents,
        )
        result.gate_result = gate_res
        
        if not gate_res.allowed:
            if config.GATES_SHADOW_MODE:
                result.reasons.append("gate: would_block (shadow mode)")
                # In shadow mode, continue to other checks but log the block
            else:
                result.reasons.extend(gate_res.reasons)
                result.allow = False
                return result  # Hard block
        
        # Collect gate reasons even if allowed
        if gate_res.reasons:
            reasons.extend([f"gate: {r}" for r in gate_res.reasons])
    
    # ── 2. Fair-value engine ────────────────────────────────────────────
    if config.FAIR_VALUE_ENABLED:
        # Determine side if not provided
        side = inputs.side
        if side is None:
            mid = getattr(inputs.market, "mid_yes", None)
            side = "no" if (mid is not None and mid < 50) else "yes"
        
        # Build fair value input
        fv_input = fair_value.FairValueInput(
            spot=inputs.spot_state.get("spot") if inputs.spot_state else None,
            strike=float(getattr(inputs.market, "strike", 0.0) or 0.0),
            minutes_left=float(getattr(inputs.market, "minutes_left", 0.0) or 0.0),
            sigma_per_min=inputs.spot_state.get("sigma_per_min") if inputs.spot_state else None,
            yes_bid=inputs.yes_bid,
            yes_ask=inputs.yes_ask,
            no_bid=inputs.no_bid,
            no_ask=inputs.no_ask,
            side=side,
            series=getattr(inputs.market, "series", None) or getattr(inputs.market, "ticker", None),
            ml_prob_up=inputs.ml_prob_up,
        )
        
        fv_result = fair_value.compute_fair_value(fv_input)
        result.fair_value = fv_result
        
        # Check EV threshold
        min_ev = getattr(config, "FAIR_VALUE_MIN_EV", 0.001)
        if not fair_value.gate_ev_threshold(fv_result, min_ev):
            result.reasons.append(f"fair_value: EV ${fv_result.recommended_ev:.4f} < min ${min_ev:.4f}")
            result.allow = False
            return result
        
        # Record recommendation
        result.recommended_path = fv_result.recommended_path
        result.recommended_price = fv_result.recommended_entry_price
        result.recommended_ev = fv_result.recommended_ev
        reasons.append(f"fair_value: {fv_result.recommended_path}@${fv_result.recommended_entry_price/100:.2f} EV=${fv_result.recommended_ev:.4f}")
    
    # ── 3. KL divergence health check ──────────────────────────────────
    if config.GATES_KL_ENABLED and inputs.kalshi_mid is not None and inputs.polymarket_mid is not None:
        kl_val, kl_warn, kl_block = evaluate_kl_health(inputs.kalshi_mid, inputs.polymarket_mid)
        result.kl_divergence = kl_val
        result.kl_warning = kl_warn
        result.kl_block = kl_block
        
        if kl_block:
            result.reasons.append(f"kl: divergence {kl_val:.4f} > block threshold")
            result.allow = False
            return result
        elif kl_warn:
            reasons.append(f"kl: divergence {kl_val:.4f} > watch threshold (warning)")
    
    # ── 4. Calibration breaker ──────────────────────────────────────────
    if config.CALIBRATION_BREAKER_ENABLED:
        brier = inputs.brier_score
        if brier is None:
            # Try to get from gate metrics or config
            brier = getattr(config, "CALIBRATION_BRIER_SCORE", None)
        
        cal_ok, cal_reason = evaluate_calibration(
            brier, 
            getattr(config, "CALIBRATION_BRIER_THRESHOLD", 0.20)
        )
        result.calibration_ok = cal_ok
        result.calibration_reason = cal_reason
        
        if not cal_ok:
            result.reasons.append(f"calibration: {cal_reason}")
            result.allow = False
            return result
        else:
            reasons.append(f"calibration: {cal_reason}")
    
    # All checks passed
    result.allow = True
    result.reasons = reasons
    return result


def format_decision(result: DecisionResult) -> str:
    """Format decision result for logging."""
    parts = [f"DECISION {'ALLOW' if result.allow else 'BLOCK'}"]
    
    if result.gate_result:
        parts.append(f"gate={result.gate_result.metrics.get('decision', 'n/a')}")
    
    if result.fair_value:
        parts.append(f"fv={result.fair_value.recommended_path}@${result.fair_value.recommended_entry_price/100:.2f} EV=${result.fair_value.recommended_ev:.4f}")
    
    if result.kl_divergence is not None:
        parts.append(f"KL={result.kl_divergence:.4f} {'BLOCK' if result.kl_block else 'WARN' if result.kl_warning else 'OK'}")
    
    if result.calibration_reason:
        parts.append(f"cal={'OK' if result.calibration_ok else 'FAIL'}")
    
    return " | ".join(parts)


# Convenience function for direct use in trader loops
def should_trade(
    market,
    spot_state: dict | None = None,
    orderbook: object | None = None,
    side: str | None = None,
    entry_price_cents: float | None = None,
    yes_bid: float | None = None,
    yes_ask: float | None = None,
    no_bid: float | None = None,
    no_ask: float | None = None,
    kalshi_mid: float | None = None,
    polymarket_mid: float | None = None,
    ml_prob_up: float | None = None,
    brier_score: float | None = None,
) -> DecisionResult:
    """
    Convenience wrapper for the main trading loop.
    
    Usage:
        decision = decision.should_trade(market, spot_state, orderbook, ...)
        if decision.allow:
            place_order(decision.recommended_path, decision.recommended_price)
    """
    inputs = DecisionInput(
        market=market,
        spot_state=spot_state,
        orderbook=orderbook,
        side=side,
        entry_price_cents=entry_price_cents,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        kalshi_mid=kalshi_mid,
        polymarket_mid=polymarket_mid,
        ml_prob_up=ml_prob_up,
        brier_score=brier_score,
    )
    return make_decision(inputs)


if __name__ == "__main__":
    # Quick test
    import sys
    logging.basicConfig(level=logging.INFO)
    
    # Mock market
    class MockMarket:
        ticker = "KXBTC15M-TEST"
        strike = 95000.0
        minutes_left = 8.0
        series = "KXBTC15M"
        mid_yes = 95
    
    market = MockMarket()
    
    decision = should_trade(
        market=market,
        spot_state={"spot": 100000.0, "sigma_per_min": 0.001, "atr_1m": 10.0},
        side="yes",
        entry_price_cents=95,
        yes_bid=94,
        yes_ask=96,
        no_bid=4,
        no_ask=6,
        kalshi_mid=95.0,
        polymarket_mid=94.5,
        brier_score=0.15,
    )
    
    print(format_decision(decision))
    print(f"Allow: {decision.allow}")
    print(f"Reasons: {decision.reasons}")