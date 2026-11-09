"""Tests for model.py predictive fair-value models."""

import math
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


def test_model_probability_gbm_falls_back_to_momentum():
    """GBM without spot/strike falls back to momentum model."""
    class FakeMarket:
        mid_yes = 80.0
    p = model.model_probability_from_market(
        FakeMarket(), model_type="gbm", minutes_left=1.0, momentum_cents_per_min=1.0,
    )
    # Should use momentum fallback: 80% + momentum push
    assert p > 0.8


def test_raw_mid_returns_mid():
    """raw_mid model returns market mid as probability."""
    class FakeMarket:
        mid_yes = 75.0
    assert model.model_probability_from_market(FakeMarket(), model_type="raw_mid") == 0.75
