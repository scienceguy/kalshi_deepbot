"""Focused checks for non-frozen risk_shield.yaml enforcement."""
import threading

import pytest

import config
import control
import resting_guard
import risk


def test_risk_shield_yaml_loads_attached_values():
    shield = risk._load_risk_shield()
    assert shield["portfolio"]["correlated_exposure_cap_pct"] == 15.0
    tiers = shield["drawdown_throttles"]
    assert tiers["tier_1_yellow"]["kelly_multiplier_scale"] == 0.50
    assert tiers["tier_2_orange"]["kelly_multiplier_scale"] == 0.25
    assert tiers["tier_2_orange"]["allowed_series"] == ["BTC", "ETH"]
    assert tiers["tier_3_red"]["requires_operator_review"] is True


def test_correlated_cap_uses_stricter_yaml_limit(monkeypatch):
    monkeypatch.setattr(config, "EXPOSURE_CEILING_PCT", 0.30)
    monkeypatch.setattr(config, "CASH_FLOOR_PCT", 0.0)
    monkeypatch.setattr(config, "MIN_CASH_ABSOLUTE", 0.0)
    monkeypatch.setattr(config, "PER_TRADE_PCT", 1.0)
    monkeypatch.setattr(config, "MAX_TRADE_DOLLARS", 1000.0)
    manager = risk.RiskManager()
    assert manager.max_trade_dollars(
        100.0, 0.0, mark_to_market_exposure=10.0) == 5.0


def test_drawdown_tiers_scale_kelly_and_restrict_series():
    manager = risk.RiskManager()
    manager.initialize(100.0)
    baseline = manager.kelly_size(0.80, 50, 100.0)
    assert baseline > 0.0

    manager.update_balance(96.0, mark_to_market_exposure=0.0)
    assert manager.state.shield_tier == "yellow"
    assert manager.shield_kelly_scale() == 0.50
    assert manager.kelly_size(0.80, 50, 100.0) == pytest.approx(
        baseline * 0.50, abs=0.011)

    manager.update_balance(94.0, mark_to_market_exposure=0.0)
    assert manager.state.shield_tier == "orange"
    assert manager.shield_kelly_scale() == 0.25
    assert manager.shield_min_ev_multiplier() == 1.50
    assert manager.shield_series_allowed("KXBTC15M") is True
    assert manager.shield_series_allowed("KXSOL15M") is False
    assert manager.kelly_size(0.80, 50, 100.0) == pytest.approx(
        baseline * 0.25, abs=0.011)


def test_red_pauses_and_cancels_without_live_orders():
    order = {
        "order_id": "paper-only", "slug": "paper", "side": "yes",
        "price": 0.80, "symbol": "BTC", "strike": 95.0, "spot": 100.0,
        "above_market": True, "state": "resting",
    }
    cancelled = []

    def cancel_one(row, reason):
        cancelled.append((row["order_id"], reason))
        row["state"] = "cancelled"
        return True

    guard = resting_guard.RestingGuard(
        lock=threading.RLock(), list_orders=lambda: [order],
        cancel_one=cancel_one, spot_for=lambda symbol: None,
        bbo_for=lambda slug: None, feed_age=lambda: None,
        halt_active=lambda: True,
    )
    control.STATE.pause_entries = False
    manager = risk.RiskManager()
    manager.initialize(100.0)

    manager.update_balance(90.0, mark_to_market_exposure=0.0)

    assert manager.state.shield_tier == "red"
    assert manager.state.operator_review_required is True
    assert control.STATE.pause_entries is True
    assert cancelled == [("paper-only", resting_guard.TRIGGER_HALT)]

    manager.update_balance(96.0, mark_to_market_exposure=0.0)
    assert manager.state.halted is True
    assert manager.state.operator_review_required is True
    control.approve_risk_review("operator")
    assert manager.state.halted is False
    assert manager.state.operator_review_required is False
    assert control.STATE.pause_entries is False
