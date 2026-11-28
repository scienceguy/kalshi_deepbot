import pytest

import config
import fees
import sizing


def test_aggressive_net_ev_uses_series_taker_multiplier_once():
    fees.set_series_fees({"KXTEST15M": {"fee_type": "quadratic", "fee_multiplier": 2.0}})
    try:
        result = sizing.calculate_net_ev(0.80, 50, aggressive_fill=True,
                                         series="KXTEST15M")
        assert result == pytest.approx(0.80 * 0.50 - 0.20 * 0.50 - 0.035)
    finally:
        fees.clear_series_fees()


def test_series_multiplier_does_not_change_other_sizing_policy(monkeypatch):
    fees.set_series_fees({"KXTEST15M": {"fee_type": "quadratic", "fee_multiplier": 2.0}})
    monkeypatch.setattr(config, "AGGRESSIVE_FILL", True)
    monkeypatch.setattr(config, "MIN_EV_EDGE_PCT", 0.26)
    monkeypatch.setattr(config, "KELLY_FRACTION", 0.25)
    inputs = sizing.SizingInputs(
        kalshi_balance=sizing.VenueBalance("kalshi", 100.0, 0.0, 100.0, 1.0),
        win_prob=0.80, entry_price_cents=50, series="KXTEST15M",
        ticker="KXTEST15M-X", depth_at_price=1_000_000.0,
    )
    try:
        result = sizing.calculate_sizing(inputs)
        assert result.size_dollars > 0
        assert result.edge_cents == pytest.approx(26.5)
        assert result.min_ev_edge_cents == pytest.approx(26.0)
    finally:
        fees.clear_series_fees()
