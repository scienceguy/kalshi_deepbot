"""Tests for model.py predictive fair-value models."""

import math
from statistics import NormalDist

import pytest

import model


def test_gbm_atm_no_time():
    """Zero minutes left: deterministic — spot >= strike => YES wins."""
    assert model.gbm_probability_yes_wins(100.0, 100.0, 0.0) == 1.0
    assert model.gbm_probability_yes_wins(99.0, 100.0, 0.0) == 0.0


def test_gbm_above_strike():
    """Spot well above strike with time left: high YES probability."""
    p = model.gbm_probability_yes_wins(110.0, 100.0, 15.0)
    assert 0.5 < p < 1.0


def test_gbm_below_strike():
    """Spot well below strike with time left: low YES probability."""
    p = model.gbm_probability_yes_wins(90.0, 100.0, 15.0)
    assert 0.0 < p < 0.5


def test_gbm_clamped_to_range():
    """Extreme parameters never escape [0, 1]."""
    p = model.gbm_probability_yes_wins(0.0, 100.0, 15.0)
    assert p == 0.5  # spot<=0 -> 0.5 fallback
    p = model.gbm_probability_yes_wins(100.0, 0.0, 15.0)
    assert p == 0.5  # strike<=0 -> 0.5 fallback


def test_gbm_converges_toward_extremes_with_time():
    """More time = more uncertainty = probability closer to 0.5."""
    p_short = model.gbm_probability_yes_wins(110.0, 100.0, 1.0)
    p_long = model.gbm_probability_yes_wins(110.0, 100.0, 10000.0)
    assert abs(p_long - 0.5) < abs(p_short - 0.5)


def test_edge_cents_positive_when_underpriced():
    """Edge positive when model prob exceeds ask + fee."""
    fee = model.taker_fee_cents_at(87)
    edge = model.edge_cents(0.90, 87, fee)
    # 90% prob - 87c ask - ~0.79c fee = ~+2.2c
    assert edge > 0


def test_edge_cents_negative_when_overpriced():
    """Edge negative when model prob below ask + fee."""
    fee = model.taker_fee_cents_at(95)
    edge = model.edge_cents(0.90, 95, fee)
    # 90% prob - 95c ask - ~0.33c fee = negative
    assert edge < 0


def test_taker_fee_delegates_to_fees_module():
    """taker_fee_cents_at matches fees.taker_fee_cents for 1 contract."""
    from fees import taker_fee_cents
    for cents in [1, 10, 50, 87, 95, 99]:
        a = model.taker_fee_cents_at(cents)
        b = taker_fee_cents(1.0, cents / 100.0)
        assert a == b, f"mismatch at {cents}c: model={a} fees={b}"


def test_momentum_adjusted_probability():
    """Momentum pushes probability up when price rising, down when falling."""
    base = model.momentum_adjusted_probability(50.0, 5.0, 0.0)
    assert base == pytest.approx(0.5)
    up = model.momentum_adjusted_probability(50.0, 5.0, 2.0)  # +2c/min
    assert up > base
    down = model.momentum_adjusted_probability(50.0, 5.0, -2.0)  # -2c/min
    assert down < base


def test_momentum_clamped():
    """Momentum adjustment clamped to max_adjustment_cents."""
    extreme = model.momentum_adjusted_probability(50.0, 60.0, 100.0)
    assert extreme <= 1.0
    extreme_down = model.momentum_adjusted_probability(50.0, 60.0, -100.0)
    assert extreme_down >= 0.0


def test_locked_share_reduce_uncertainty():
    """More seconds into final minute = more locked = probability pushed toward extreme."""
    base = model.locked_share_probability(80.0, 0)  # start of final minute
    half = model.locked_share_probability(80.0, 30)  # halfway through
    full = model.locked_share_probability(80.0, 60)  # settled
    assert base == pytest.approx(0.8)  # 0s -> raw mid
    assert full == pytest.approx(0.8)  # fully settled -> raw mid (no adjustment)
    assert half > base  # partly locked pushes toward extreme (above 80%)


def test_model_probability_from_market_returns_default():
    """MarketInfo with no mid -> 0.5."""
    class FakeMarket:
        mid_yes = None
    assert model.model_probability_from_market(FakeMarket()) == 0.5


def test_model_probability_gbm_falls_back_to_raw_mid():
    """GBM without spot/strike falls back to raw mid probability without phantom edge."""
    class FakeMarket:
        mid_yes = 80.0
    p = model.model_probability_from_market(
        FakeMarket(), model_type="gbm", minutes_left=1.0, momentum_cents_per_min=1.0,
    )
    # Should use raw mid fallback: 80% (0.80)
    assert p == 0.80


def test_raw_mid_returns_mid():
    """raw_mid model returns market mid as probability."""
    class FakeMarket:
        mid_yes = 75.0
    assert model.model_probability_from_market(FakeMarket(), model_type="raw_mid") == 0.75


# ── settlement-index terms (measured 2026-11-21, scripts/measure_index_basis.py) ──
def _d2_prob(spot, strike, minutes, vol):
    T = minutes / (365.0 * 24.0 * 60.0)
    z = (math.log(spot / strike) + (0.05 - 0.5 * vol * vol) * T) / (vol * math.sqrt(T))
    return NormalDist().cdf(z)


def test_index_terms_off_is_unchanged():
    # Zero bias/dispersion must reproduce the raw-feed probability exactly:
    # no live gate changes unless the terms are set on purpose.
    args = (60000.0, 60100.0, 12.0, 0.80)
    raw = model.gbm_probability_yes_wins(*args)
    assert raw == _d2_prob(*args)
    assert model.gbm_probability_yes_wins(*args, index_bias_bps=0.0,
                                          index_dispersion=0.0) == raw


def test_negative_bias_lowers_p_above_a_farther_strike():
    # The index prints below the feed (-13.15bps measured), so an above-spot
    # strike is farther from the settlement level than the feed suggests.
    spot, strike, minutes, vol = 58900.0, 59100.0, 15.0, 0.80
    raw = model.gbm_probability_yes_wins(spot, strike, minutes, vol)
    corrected = model.gbm_probability_yes_wins(spot, strike, minutes, vol,
                                               index_bias_bps=-13.15)
    assert corrected < raw
    # and it matches the same formula priced on the index level
    assert corrected == _d2_prob(spot * 0.998685, strike, minutes, vol)


def test_dispersion_widens_the_band_toward_the_mid():
    spot, strike, minutes, vol = 58900.0, 59100.0, 15.0, 0.80
    raw = model.gbm_probability_yes_wins(spot, strike, minutes, vol)
    wide = model.gbm_probability_yes_wins(spot, strike, minutes, vol,
                                          index_dispersion=0.00175)
    assert wide > raw          # above-strike probability moves up toward 0.5
    assert model.gbm_probability_yes_wins(58900.0, 58700.0, minutes, vol,
                                          index_dispersion=0.00175) < \
           model.gbm_probability_yes_wins(58900.0, 58700.0, minutes, vol)


def test_no_time_left_prices_against_the_index_level():
    # Spot exactly at the strike: the index level, not the feed, decides.
    assert model.gbm_probability_yes_wins(58900.0, 58900.0, 0.0, 0.80) == 1.0
    assert model.gbm_probability_yes_wins(58900.0, 58900.0, 0.0, 0.80,
                                          index_bias_bps=-13.15) == 0.0
