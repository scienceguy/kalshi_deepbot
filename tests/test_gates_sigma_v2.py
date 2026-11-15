"""B11: ATR->sigma conversion V2 behind GATES_SIGMA_FROM_ATR_V2 (default off).

The legacy conversion treated ATR (a mean TRUE RANGE of high-low candles,
~1.6 sigma) as E|dS| (~0.8 sigma) and multiplied by sqrt(pi/2) ~ 1.25, so it
overstated per-minute log-vol by roughly 1.9x. V2 divides by 1.6. The flag
defaults off, and the gate record logs both sigmas and the source used.
"""
import math
import random
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

sys.argv = ["test"]

import config
import gates
from tests.test_gates import MockMarket


def _gbm_candles(n_minutes, sigma=0.002, spot0=100.0, seed=20261115):
    """Synthetic 1-minute GBM candles as (open, high, low, close) rows.

    120 substeps per minute keep the discrete Brownian bridge's range within a
    few percent of the continuous one (E[range] = 1.596 sigma); fewer substeps
    biases TR low and would mask the estimator's true accuracy.
    """
    rng = random.Random(seed)
    candles = []
    price = spot0
    substeps = 120
    dt = 1.0 / substeps  # minutes
    for _ in range(n_minutes):
        open_ = price
        hi = lo = open_
        for _ in range(substeps):
            price *= math.exp(-0.5 * sigma * sigma * dt
                              + sigma * math.sqrt(dt) * rng.gauss(0, 1))
            hi, lo = max(hi, price), min(lo, price)
        candles.append((open_, hi, lo, price))
    return candles


def _mean_true_range(candles, lookback=500):
    """Wilder mean TR, same formula gates.atr_1m uses."""
    trs = []
    prev_close = candles[0][3]
    for _o, h, lo, c in candles[1:]:
        trs.append(max(h - lo, abs(h - prev_close), abs(lo - prev_close)))
        prev_close = c
    window = trs[-lookback:]
    return sum(window) / len(window)


# ── acceptance: synthetic candles recover the truth ────────────────────

def test_v2_sigma_within_10pct_of_truth(monkeypatch):
    """Fixed seed, 6,000 GBM 1-minute candles (>= 5,000 required)."""
    monkeypatch.setattr(config, "GATES_SIGMA_FROM_ATR_V2", True)
    candles = _gbm_candles(6000, sigma=0.002, seed=20261115)
    atr = _mean_true_range(candles)
    est = gates.sigma_from_atr(atr, candles[-1][3])
    assert abs(est - 0.002) / 0.002 <= 0.10


def test_legacy_conversion_overstates_truth(monkeypatch):
    """V1 multiplies ATR by sqrt(pi/2); it overstates sigma ~1.9x."""
    monkeypatch.setattr(config, "GATES_SIGMA_FROM_ATR_V2", False)
    candles = _gbm_candles(6000, sigma=0.002, seed=20261115)
    atr = _mean_true_range(candles)
    est = gates.sigma_from_atr(atr, candles[-1][3])
    assert est > 0.002 * 1.5  # overstated well beyond 50%


def test_flag_off_path_is_byte_identical(monkeypatch):
    """With V2 off, the conversion is exactly (atr/spot) * sqrt(pi/2)."""
    monkeypatch.setattr(config, "GATES_SIGMA_FROM_ATR_V2", False)
    atr, spot = 0.32, 100.0
    assert gates.sigma_from_atr(atr, spot) == pytest.approx(atr / spot * math.sqrt(math.pi / 2))


def test_v2_conversion(monkeypatch):
    monkeypatch.setattr(config, "GATES_SIGMA_FROM_ATR_V2", True)
    atr, spot = 0.32, 100.0
    assert gates.sigma_from_atr(atr, spot) == pytest.approx(atr / spot / 1.6)


def test_v2_handles_missing_atr():
    assert gates.sigma_from_atr(None, 100.0) is None
    assert gates.sigma_from_atr(0.32, 0) is None


# ── the gate record logs old and new sigma + source ────────────────────

def _eval(monkeypatch, spot_state, **cfg):
    monkeypatch.setattr(config, "GATES_ENABLED", True)
    for k, v in cfg.items():
        monkeypatch.setattr(config, k, v)
    market = MockMarket(strike=95000.0, minutes_left=5.0)
    with patch("gates.atr_1m", return_value=100.0), \
         patch("gates.spot_for", return_value=94000.0):
        return gates.evaluate_entry(market, side="yes", entry_price_cents=77,
                                    spot_state=spot_state)


def test_atr_path_logs_both_sigmas_and_source(monkeypatch):
    res = _eval(monkeypatch, {}, GATES_SIGMA_FROM_ATR_V2=True)
    assert res.metrics["sigma_source"] == "atr_v2"
    assert res.metrics["sigma_atr_v1"] == pytest.approx((100.0 / 94000.0) * math.sqrt(math.pi / 2))
    assert res.metrics["sigma_atr_v2"] == pytest.approx((100.0 / 94000.0) / 1.6)
    assert "cushion_z" in res.metrics


def test_flag_off_uses_atr_v1(monkeypatch):
    res = _eval(monkeypatch, {}, GATES_SIGMA_FROM_ATR_V2=False)
    assert res.metrics["sigma_source"] == "atr_v1"
    assert res.metrics["sigma_atr_v1"] == pytest.approx((100.0 / 94000.0) * math.sqrt(math.pi / 2))


def test_composite_sigma_preferred_over_atr(monkeypatch):
    """A provided composite sigma wins; ATR is not consulted (B11)."""
    res = _eval(monkeypatch,
                {"spot": 94000.0, "sigma_per_sqrt_min": 0.0018, "atr_1m": 100.0},
                GATES_SIGMA_FROM_ATR_V2=True)
    assert res.metrics["sigma_source"] == "spot_state"
    assert "sigma_atr_v2" not in res.metrics
