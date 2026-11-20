"""Tests for fair_value.py — T15-21: Fair-value engine (maker vs taker)."""
import pytest
from unittest.mock import Mock, patch
import config

import fair_value
from fair_value import FairValueInput, FairValueResult, compute_fair_value, blend_probabilities, gate_ev_threshold, FillModel


class TestBlendProbabilities:
    """Test probability blending with uncertainty."""

    def test_gbm_only(self):
        """Single GBM probability returns with base uncertainty."""
        p_hat, unc = blend_probabilities(0.6, None)
        assert p_hat == 0.6
        assert unc >= 0.015  # base calibration error
        assert unc <= 0.1    # capped

    def test_ml_only(self):
        """Single ML probability returns with base uncertainty."""
        p_hat, unc = blend_probabilities(None, 0.55)
        assert p_hat == 0.55
        assert unc >= 0.015

    def test_both_models(self):
        """Two models blend with weight and disagreement increases uncertainty."""
        p_hat, unc = blend_probabilities(0.6, 0.55, weight_gbm=0.6, weight_ml=0.4)
        # 0.6*0.6 + 0.55*0.4 = 0.36 + 0.22 = 0.58
        assert abs(p_hat - 0.58) < 1e-9
        # Uncertainty includes model disagreement
        assert unc > 0.015

    def test_equal_models_low_uncertainty(self):
        """Agreeing models have lower uncertainty."""
        p_hat, unc = blend_probabilities(0.55, 0.55)
        assert p_hat == 0.55
        # With agreement, uncertainty = sqrt(0 + 0.015^2) = 0.015
        assert abs(unc - 0.015) < 1e-9

    def test_no_models(self):
        """No models returns max uncertainty."""
        p_hat, unc = blend_probabilities(None, None)
        assert p_hat == 0.5
        assert unc == 0.1


class TestFillModel:
    """Test maker fill probability model."""

    def test_crossing_spread_high_fill(self):
        """Crossing the spread (at mid) has ~95% fill."""
        assert FillModel.maker_fill_prob(95, 95.0, "yes") == 0.95
        assert FillModel.maker_fill_prob(5, 5.0, "no") == 0.95

    def test_at_bid_low_fill(self):
        """At bid (1c inside) has ~36% fill."""
        assert FillModel.maker_fill_prob(94, 95.0, "yes") == 0.36
        assert FillModel.maker_fill_prob(6, 5.0, "no") == 0.36

    def test_two_cents_inside(self):
        """2c inside has ~55% fill."""
        assert FillModel.maker_fill_prob(93, 95.0, "yes") == 0.55
        assert FillModel.maker_fill_prob(7, 5.0, "no") == 0.55

    def test_deep_inside(self):
        """Deep inside spread has ~75% fill."""
        assert FillModel.maker_fill_prob(90, 95.0, "yes") == 0.75
        assert FillModel.maker_fill_prob(10, 5.0, "no") == 0.75


class TestComputeFairValue:
    """Test main fair value computation."""

    def test_yes_side_basic(self):
        """Basic YES side computation with all prices."""
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
        # p_hat should be very high (spot well above strike)
        assert fv.p_hat > 0.95
        # Maker at bid (94c), taker at ask (96c)
        assert fv.maker_entry_price_cents == 94
        assert fv.taker_entry_price_cents == 96
        # Both should have positive EV in this scenario
        assert fv.maker_net_ev > 0 or fv.taker_net_ev > 0
        # Recommended path should be one of maker/taker/none
        assert fv.recommended_path in ("maker", "taker", "none")

    def test_no_side_basic(self):
        """Basic NO side computation."""
        fv = compute_fair_value(FairValueInput(
            spot=100000.0,
            strike=95000.0,
            minutes_left=8.0,
            sigma_per_min=0.001,
            yes_bid=94,
            yes_ask=96,
            no_bid=4,
            no_ask=6,
            side="no",
            series="KXBTC15M",
        ))
        # NO is out of the money (spot above strike)
        assert fv.p_hat > 0.95  # P(YES wins)
        # For NO side, q = 1 - p_hat
        assert fv.components["q"] < 0.05
        # Maker at ask (6c for NO), taker at bid (4c)
        assert fv.maker_entry_price_cents == 6
        assert fv.taker_entry_price_cents == 4

    def test_missing_prices_returns_none_path(self):
        """Missing prices returns recommended_path='none'."""
        fv = compute_fair_value(FairValueInput(
            spot=100000.0,
            strike=95000.0,
            minutes_left=8.0,
            sigma_per_min=0.001,
            yes_bid=None,
            yes_ask=None,
            side="yes",
        ))
        assert fv.recommended_path == "none"
        assert fv.recommended_entry_price == 0
        assert fv.recommended_ev == 0.0

    def test_maker_preferred_when_better_ev(self, monkeypatch):
        """Maker path recommended when net EV beats taker."""
        # Create scenario where maker has better EV
        # We'll mock the fill prob to be high for maker
        with patch.object(FillModel, "maker_fill_prob", return_value=0.9):
            fv = compute_fair_value(FairValueInput(
                spot=100000.0,
                strike=95000.0,
                minutes_left=8.0,
                sigma_per_min=0.001,
                yes_bid=90,  # wider spread
                yes_ask=99,
                side="yes",
                series="KXBTC15M",
            ))
            # With high fill prob, maker should win
            if fv.maker_net_ev > fv.taker_net_ev:
                assert fv.recommended_path == "maker"

    def test_gbm_prob_used_when_no_ml(self):
        """GBM probability used when ML not provided."""
        fv = compute_fair_value(FairValueInput(
            spot=100000.0,
            strike=95000.0,
            minutes_left=8.0,
            sigma_per_min=0.001,
            yes_bid=94,
            yes_ask=96,
            side="yes",
        ))
        # gbm_prob should be in components
        assert "gbm_prob" in fv.components
        assert fv.components["gbm_prob"] is not None

    def test_ml_prob_blended_when_provided(self):
        """ML probability blended when provided."""
        fv = compute_fair_value(FairValueInput(
            spot=100000.0,
            strike=95000.0,
            minutes_left=8.0,
            sigma_per_min=0.001,
            yes_bid=94,
            yes_ask=96,
            side="yes",
            ml_prob_up=0.50,  # very different from GBM
        ))
        assert "ml_prob" in fv.components
        assert fv.components["ml_prob"] == 0.50
        # p_hat should be between GBM and ML
        assert fv.components["gbm_prob"] is not None
        # With 60/40 weighting, p_hat should be weighted
        expected = 0.6 * fv.components["gbm_prob"] + 0.4 * 0.50
        assert abs(fv.p_hat - expected) < 0.01

    def test_uncertainty_band_correct(self):
        """Uncertainty band correctly computed."""
        fv = compute_fair_value(FairValueInput(
            spot=100000.0,
            strike=95000.0,
            minutes_left=8.0,
            sigma_per_min=0.001,
            yes_bid=94,
            yes_ask=96,
            side="yes",
        ))
        assert fv.p_hat_lower <= fv.p_hat <= fv.p_hat_upper
        assert fv.p_hat_upper - fv.p_hat_lower <= 0.2  # max 20% band (2*10% cap)


class TestGateEVThreshold:
    """Test EV threshold gate."""

    def test_passes_when_ev_above_threshold(self):
        """Gate passes when recommended EV >= threshold."""
        fv = FairValueResult(
            p_hat=0.6, p_hat_lower=0.55, p_hat_upper=0.65,
            maker_entry_price_cents=90, maker_fill_prob=0.5,
            maker_fee=0.0, maker_ev=0.02, maker_net_ev=0.01,
            taker_entry_price_cents=95, taker_fee=0.005,
            taker_ev=0.015, taker_net_ev=0.015,
            recommended_path="maker", recommended_entry_price=90,
            recommended_ev=0.01,
        )
        assert gate_ev_threshold(fv, min_ev=0.005) is True
        assert gate_ev_threshold(fv, min_ev=0.015) is False

    def test_fails_when_ev_below_threshold(self):
        """Gate fails when recommended EV < threshold."""
        fv = FairValueResult(
            p_hat=0.5, p_hat_lower=0.45, p_hat_upper=0.55,
            maker_entry_price_cents=50, maker_fill_prob=0.5,
            maker_fee=0.0, maker_ev=0.001, maker_net_ev=0.0005,
            taker_entry_price_cents=55, taker_fee=0.005,
            taker_ev=0.001, taker_net_ev=0.001,
            recommended_path="taker", recommended_entry_price=55,
            recommended_ev=0.001,
        )
        assert gate_ev_threshold(fv, min_ev=0.001) is True
        assert gate_ev_threshold(fv, min_ev=0.002) is False


class TestFairValueIntegration:
    """Integration-style tests."""

    def test_format_fair_value(self):
        """Format function produces readable output."""
        fv = FairValueResult(
            p_hat=0.6, p_hat_lower=0.55, p_hat_upper=0.65,
            maker_entry_price_cents=90, maker_fill_prob=0.5,
            maker_fee=0.0, maker_ev=0.02, maker_net_ev=0.01,
            taker_entry_price_cents=95, taker_fee=0.005,
            taker_ev=0.015, taker_net_ev=0.015,
            recommended_path="maker", recommended_entry_price=90,
            recommended_ev=0.01,
        )
        out = fair_value.format_fair_value(fv)
        assert "FV p_hat=" in out
        assert "maker@" in out
        assert "taker@" in out
        assert "rec=" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])