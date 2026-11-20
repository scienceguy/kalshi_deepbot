"""
kl_tools.py — KL divergence tools for probability distributions.

Consolidates symmetric_kl_divergence in one place (fixes F-03).
Used by gates.py and crossvenue.py.

Uses:
1. Ladder consistency and monotonicity checks
2. Model-vs-market KL on multi-strike products
3. Drift gate: KL or PSI of live feature distributions vs training
4. Log-loss-based ensemble weights

Clips probabilities with epsilon; handles 0/1 inputs.
"""

import math
from typing import List, Tuple, Optional

# Small epsilon for clipping probabilities away from boundaries
EPS = 1e-12


def kl_divergence(p: float, q: float) -> float:
    """
    Kullback-Leibler divergence KL(P||Q) for binary distributions.

    For binary markets: KL(P||Q) = p*ln(p/q) + (1-p)*ln((1-p)/(1-q))
    where p and q are probabilities in (0, 1).

    Returns infinity if p or q is at boundary (0 or 1) to avoid log(0).

    Args:
        p: Probability from distribution P
        q: Probability from distribution Q

    Returns:
        KL divergence, or inf if either probability is at boundary
    """
    # Return infinity for boundary values to match original behavior
    if p <= 0.0 or p >= 1.0 or q <= 0.0 or q >= 1.0:
        return float("inf")
    return p * math.log(p / q) + (1.0 - p) * math.log((1.0 - p) / (1.0 - q))


def symmetric_kl_divergence(p: float, q: float) -> float:
    """
    Symmetric KL divergence (Jensen-Shannon like): [KL(P||Q) + KL(Q||P)] / 2.

    This measures the information-theoretic distance between two probability
    distributions. A symmetric KL > 0.15 indicates meaningful disagreement
    between the two venues' pricing (per prediction market research).

    Args:
        p: Probability from first venue (e.g., Kalshi)
        q: Probability from second venue (e.g., Polymarket)

    Returns:
        Symmetric KL divergence, or inf if either probability is at boundary.
    """
    kl_pq = kl_divergence(p, q)
    kl_qp = kl_divergence(q, p)

    # If either is inf (boundary case), return inf
    if kl_pq == float("inf") or kl_qp == float("inf"):
        return float("inf")

    return (kl_pq + kl_qp) / 2.0


def ladder_kl_divergence(kalshi_probs: List[float],
                         poly_probs: List[float]) -> float:
    """
    Average symmetric KL divergence across a ladder of strikes.

    Used to measure overall disagreement between two venue ladders.

    Args:
        kalshi_probs: List of probabilities from Kalshi at each strike
        poly_probs: List of probabilities from Polymarket at each strike

    Returns:
        Average symmetric KL divergence, or inf if any boundary hit
    """
    if len(kalshi_probs) != len(poly_probs) or len(kalshi_probs) == 0:
        return float("inf")

    total_kl = 0.0
    for p, q in zip(kalshi_probs, poly_probs):
        skl = symmetric_kl_divergence(p, q)
        if skl == float("inf"):
            return float("inf")
        total_kl += skl

    return total_kl / len(kalshi_probs)


def kl_arb_flag(p: float, q: float, threshold: float = 0.15) -> bool:
    """Return True if symmetric KL exceeds arbitrage threshold."""
    return symmetric_kl_divergence(p, q) > threshold


def kl_watch_flag(p: float, q: float, threshold: float = 0.05) -> bool:
    """Return True if symmetric KL exceeds watch threshold."""
    return symmetric_kl_divergence(p, q) > threshold


def ladder_consistency_kl(ladder_probs: List[float]) -> float:
    """
    Check monotonicity of a probability ladder using KL divergence.

    For a valid CDF, P(price > strike) must be non-increasing as strike increases.
    This computes the average KL between adjacent rungs to detect violations.

    Returns:
        Average KL divergence between adjacent rungs (lower = more monotonic)
    """
    if len(ladder_probs) < 2:
        return float("inf")

    total = 0.0
    for i in range(len(ladder_probs) - 1):
        p = ladder_probs[i]
        q = ladder_probs[i + 1]
        # Should have p >= q (probability decreases with strike)
        if p < q:
            # Violation: probability increases with strike
            total += symmetric_kl_divergence(p, q)
        else:
            total += kl_divergence(p, q)  # One-way KL for valid direction

    return total / (len(ladder_probs) - 1)


def psi_divergence(expected: List[float], observed: List[float],
                   bins: int = 10) -> float:
    """
    Population Stability Index (PSI) for drift detection.

    PSI = sum((obs_i - exp_i) * ln(obs_i / exp_i))

    Used for: drift gate, KL or PSI of live feature distributions vs training.

    Args:
        expected: Reference distribution (e.g., training data)
        observed: Live distribution
        bins: Number of bins for histogram (if inputs are raw values)

    Returns:
        PSI value (0 = no drift, >0.2 = significant drift)
    """
    if len(expected) != len(observed) or len(expected) == 0:
        return float("inf")

    # Clip to avoid log(0)
    exp_clipped = [max(EPS, min(1.0 - EPS, x)) for x in expected]
    obs_clipped = [max(EPS, min(1.0 - EPS, x)) for x in observed]

    psi = 0.0
    for e, o in zip(exp_clipped, obs_clipped):
        psi += (o - e) * math.log(o / e)

    return psi


def ensemble_weight_from_logloss(loglosses: List[float]) -> List[float]:
    """
    Compute ensemble weights from log-loss values.

    Lower log-loss -> higher weight. Uses softmax over negative log-loss.

    Args:
        loglosses: List of log-loss values for each model

    Returns:
        List of weights summing to 1.0
    """
    if not loglosses:
        return []

    # Convert to negative log-loss (higher = better)
    scores = [-ll for ll in loglosses]

    # Softmax
    max_score = max(scores)
    exp_scores = [math.exp(s - max_score) for s in scores]
    total = sum(exp_scores)

    return [s / total for s in exp_scores]


if __name__ == "__main__":
    # Quick smoke test
    print("Testing kl_tools...")
    print(f"KL(0.5||0.6): {kl_divergence(0.5, 0.6):.6f}")
    print(f"KL(0.6||0.5): {kl_divergence(0.6, 0.5):.6f}")
    print(f"Sym KL(0.5, 0.6): {symmetric_kl_divergence(0.5, 0.6):.6f}")
    print(f"Sym KL(0.5, 0.9): {symmetric_kl_divergence(0.5, 0.9):.6f}")
    print(f"Sym KL(0.01, 0.01): {symmetric_kl_divergence(0.01, 0.01):.6f}")
    print(f"Sym KL(0.99, 0.01): {symmetric_kl_divergence(0.99, 0.01):.6f}")

    # Test ladder KL
    kalshi = [0.9, 0.7, 0.3, 0.1]
    poly = [0.85, 0.65, 0.35, 0.15]
    print(f"Ladder KL: {ladder_kl_divergence(kalshi, poly):.6f}")

    # Test consistency
    valid_ladder = [0.9, 0.7, 0.5, 0.3, 0.1]
    invalid_ladder = [0.5, 0.7, 0.3, 0.1]  # Increases then decreases
    print(f"Valid ladder consistency KL: {ladder_consistency_kl(valid_ladder):.6f}")
    print(f"Invalid ladder consistency KL: {ladder_consistency_kl(invalid_ladder):.6f}")

    # Test PSI
    expected = [0.2, 0.3, 0.5]
    observed = [0.25, 0.25, 0.5]
    print(f"PSI: {psi_divergence(expected, observed):.6f}")

    # Test ensemble weights
    loglosses = [0.45, 0.52, 0.38, 0.61]
    weights = ensemble_weight_from_logloss(loglosses)
    print(f"Log-losses: {loglosses}")
    print(f"Weights: {[f'{w:.3f}' for w in weights]}")