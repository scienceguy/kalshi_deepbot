"""Tests for decision.py — T15-25: Decision policy."""
import pytest
from unittest.mock import Mock, patch, MagicMock
import config

import decision
from decision import DecisionInput, DecisionResult, make_decision, should_trade, evaluate_kl_health, evaluate_calibration


class MockMarket:
    """Mock market object for testing."""
    ticker = "KXBTC15M-TEST"
    strike = 95000.0
    minutes_left = 8.0
    series = "KXBTC15M"
    mid_yes = 95
    raw = {"event_ticker": "KXBTC15M-TEST"}


class TestEvaluateKLHealth:
    """Test KL divergence health evaluation."""

    def test_no_prices_returns_none(self):
        """None prices returns (None, False, False)."""
        kl, warn, block = evaluate_kl_health(None, 95.0)
        assert kl is None
        assert warn is False
        assert block is False

    def test_both_prices_computes_kl(self):
        """Both prices present computes symmetric KL."""
        with patch.object(config, "GATES_KL_WATCH_THRESHOLD", 0.05), \
             patch.object(config, "GATES_KL_BLOCK_THRESHOLD", 0.15):
            kl, warn, block = evaluate_kl_health(95.0, 94.0)
            assert kl is not None
            assert kl > 0

    def test_watch_threshold_warning(self):
        """KL above watch threshold sets warning."""
        with patch.object(config, "GATES_KL_WATCH_THRESHOLD", 0.01), \
             patch.object(config, "GATES_KL_BLOCK_THRESHOLD", 1.0):  # high block threshold
            # 95 vs 50 should have high KL
            kl, warn, block = evaluate_kl_health(95.0, 50.0)
            assert warn is True
            assert block is False

    def test_block_threshold_blocks(self):
        """KL above block threshold sets block."""
        with patch.object(config, "GATES_KL_WATCH_THRESHOLD", 0.05), \
             patch.object(config, "GATES_KL_BLOCK_THRESHOLD", 0.10):
            kl, warn, block = evaluate_kl_health(95.0, 50.0)
            assert block is True
            assert warn is True

    def test_boundary_clamping(self):
        """Probabilities clamped away from 0/1 to avoid inf."""
        with patch.object(config, "GATES_KL_WATCH_THRESHOLD", 0.05), \
             patch.object(config, "GATES_KL_BLOCK_THRESHOLD", 0.15):
            # 99.999% vs 50% - should clamp and not error
            kl, warn, block = evaluate_kl_health(99.999, 50.0)
            assert kl is not None
            assert kl != float("inf")


class TestEvaluateCalibration:
    """Test calibration breaker evaluation."""

    def test_no_brier_skips(self):
        """No Brier score returns ok."""
        ok, reason = evaluate_calibration(None, 0.20)
        assert ok is True
        assert "skip" in reason

    def test_brier_below_threshold_ok(self):
        """Brier below threshold returns ok."""
        ok, reason = evaluate_calibration(0.15, 0.20)
        assert ok is True
        assert "calibrated" in reason

    def test_brier_above_threshold_blocks(self):
        """Brier above threshold returns fail."""
        ok, reason = evaluate_calibration(0.25, 0.20)
        assert ok is False
        assert "miscalibrated" in reason

    def test_brier_at_threshold_ok(self):
        """Brier at threshold is ok (not strictly greater)."""
        ok, reason = evaluate_calibration(0.20, 0.20)
        assert ok is True


class TestMakeDecision:
    """Test the main make_decision function."""

    def test_gates_block_stops_early(self, monkeypatch):
        """Gate block stops evaluation early."""
        monkeypatch.setattr(config, "GATES_ENABLED", True)
        monkeypatch.setattr(config, "GATES_SHADOW_MODE", False)
        
        market = MockMarket()
        market.strike = 95000.0
        market.minutes_left = 8.0
        
        # Mock gate to block
        mock_gate = Mock()
        mock_gate.allowed = False
        mock_gate.reasons = ["gate1: ATR hurdle failed"]
        mock_gate.metrics = {"decision": "block"}
        
        with patch("decision.gates.evaluate_entry", return_value=mock_gate):
            inputs = DecisionInput(market=market)
            result = make_decision(inputs)
        
        assert result.allow is False
        assert any("gate1" in r for r in result.reasons)

    def test_gates_shadow_mode_continues(self, monkeypatch):
        """Shadow mode continues to other checks."""
        monkeypatch.setattr(config, "GATES_ENABLED", True)
        monkeypatch.setattr(config, "GATES_SHADOW_MODE", True)
        monkeypatch.setattr(config, "FAIR_VALUE_ENABLED", True)
        monkeypatch.setattr(config, "FAIR_VALUE_MIN_EV", 0.001)
        monkeypatch.setattr(config, "GATES_KL_ENABLED", False)
        monkeypatch.setattr(config, "CALIBRATION_BREAKER_ENABLED", False)
        
        market = MockMarket()
        market.strike = 95000.0
        market.minutes_left = 8.0
        
        # Mock gate to would_block
        mock_gate = Mock()
        mock_gate.allowed = True  # shadow mode allows
        mock_gate.reasons = ["gate1: would_block"]
        mock_gate.metrics = {"decision": "would_block"}
        
        with patch("decision.gates.evaluate_entry", return_value=mock_gate), \
             patch("decision.fair_value.compute_fair_value") as mock_fv:
            
            # Mock fair value to pass
            from fair_value import FairValueResult
            mock_fv.return_value = FairValueResult(
                p_hat=0.6, p_hat_lower=0.55, p_hat_upper=0.65,
                maker_entry_price_cents=90, maker_fill_prob=0.5,
                maker_fee=0.0, maker_ev=0.02, maker_net_ev=0.01,
                taker_entry_price_cents=95, taker_fee=0.005,
                taker_ev=0.015, taker_net_ev=0.015,
                recommended_path="maker", recommended_entry_price=90,
                recommended_ev=0.01,
            )
            
            inputs = DecisionInput(
                market=market,
                spot_state={"spot": 100000.0, "sigma_per_min": 0.001},
                yes_bid=94, yes_ask=96, no_bid=4, no_ask=6,
            )
            result = make_decision(inputs)
        
        # Should continue and allow
        assert result.allow is True

    def test_fair_value_block_stops(self, monkeypatch):
        """Fair value EV below threshold blocks."""
        monkeypatch.setattr(config, "GATES_ENABLED", True)
        monkeypatch.setattr(config, "GATES_SHADOW_MODE", False)
        monkeypatch.setattr(config, "FAIR_VALUE_ENABLED", True)
        monkeypatch.setattr(config, "FAIR_VALUE_MIN_EV", 0.01)
        monkeypatch.setattr(config, "GATES_KL_ENABLED", False)
        monkeypatch.setattr(config, "CALIBRATION_BREAKER_ENABLED", False)
        
        market = MockMarket()
        market.strike = 95000.0
        market.minutes_left = 8.0
        
        # Mock gate to pass
        mock_gate = Mock()
        mock_gate.allowed = True
        mock_gate.reasons = []
        mock_gate.metrics = {"decision": "allow"}
        
        with patch("decision.gates.evaluate_entry", return_value=mock_gate), \
             patch("decision.fair_value.compute_fair_value") as mock_fv:
            
            # Mock fair value with low EV
            from fair_value import FairValueResult
            mock_fv.return_value = FairValueResult(
                p_hat=0.5, p_hat_lower=0.45, p_hat_upper=0.55,
                maker_entry_price_cents=50, maker_fill_prob=0.5,
                maker_fee=0.0, maker_ev=0.001, maker_net_ev=0.0005,
                taker_entry_price_cents=55, taker_fee=0.005,
                taker_ev=0.001, taker_net_ev=0.001,
                recommended_path="taker", recommended_entry_price=55,
                recommended_ev=0.001,  # Below 0.01 threshold
            )
            
            inputs = DecisionInput(
                market=market,
                spot_state={"spot": 100000.0, "sigma_per_min": 0.001},
                yes_bid=94, yes_ask=96, no_bid=4, no_ask=6,
            )
            result = make_decision(inputs)
        
        assert result.allow is False
        assert any("fair_value" in r for r in result.reasons)

    def test_kl_block_stops(self, monkeypatch):
        """KL divergence block stops."""
        monkeypatch.setattr(config, "GATES_ENABLED", True)
        monkeypatch.setattr(config, "GATES_SHADOW_MODE", False)
        monkeypatch.setattr(config, "FAIR_VALUE_ENABLED", True)
        monkeypatch.setattr(config, "FAIR_VALUE_MIN_EV", 0.001)
        monkeypatch.setattr(config, "GATES_KL_ENABLED", True)
        monkeypatch.setattr(config, "GATES_KL_BLOCK_THRESHOLD", 0.10)
        monkeypatch.setattr(config, "CALIBRATION_BREAKER_ENABLED", False)
        
        market = MockMarket()
        market.strike = 95000.0
        market.minutes_left = 8.0
        
        # Mock gate to pass
        mock_gate = Mock()
        mock_gate.allowed = True
        mock_gate.reasons = []
        mock_gate.metrics = {"decision": "allow"}
        
        with patch("decision.gates.evaluate_entry", return_value=mock_gate), \
             patch("decision.fair_value.compute_fair_value") as mock_fv:
            
            # Mock fair value to pass
            from fair_value import FairValueResult
            mock_fv.return_value = FairValueResult(
                p_hat=0.6, p_hat_lower=0.55, p_hat_upper=0.65,
                maker_entry_price_cents=90, maker_fill_prob=0.5,
                maker_fee=0.0, maker_ev=0.02, maker_net_ev=0.01,
                taker_entry_price_cents=95, taker_fee=0.005,
                taker_ev=0.015, taker_net_ev=0.015,
                recommended_path="maker", recommended_entry_price=90,
                recommended_ev=0.01,
            )
            
            inputs = DecisionInput(
                market=market,
                spot_state={"spot": 100000.0, "sigma_per_min": 0.001},
                yes_bid=94, yes_ask=96, no_bid=4, no_ask=6,
                kalshi_mid=95.0, polymarket_mid=50.0,  # High KL
            )
            result = make_decision(inputs)
        
        assert result.allow is False
        assert any("kl" in r.lower() for r in result.reasons)

    def test_calibration_block_stops(self, monkeypatch):
        """Calibration breaker stops."""
        monkeypatch.setattr(config, "GATES_ENABLED", True)
        monkeypatch.setattr(config, "GATES_SHADOW_MODE", False)
        monkeypatch.setattr(config, "FAIR_VALUE_ENABLED", True)
        monkeypatch.setattr(config, "FAIR_VALUE_MIN_EV", 0.001)
        monkeypatch.setattr(config, "GATES_KL_ENABLED", False)
        monkeypatch.setattr(config, "CALIBRATION_BREAKER_ENABLED", True)
        monkeypatch.setattr(config, "CALIBRATION_BRIER_THRESHOLD", 0.20)
        
        market = MockMarket()
        market.strike = 95000.0
        market.minutes_left = 8.0
        
        # Mock gate to pass
        mock_gate = Mock()
        mock_gate.allowed = True
        mock_gate.reasons = []
        mock_gate.metrics = {"decision": "allow"}
        
        with patch("decision.gates.evaluate_entry", return_value=mock_gate), \
             patch("decision.fair_value.compute_fair_value") as mock_fv:
            
            # Mock fair value to pass
            from fair_value import FairValueResult
            mock_fv.return_value = FairValueResult(
                p_hat=0.6, p_hat_lower=0.55, p_hat_upper=0.65,
                maker_entry_price_cents=90, maker_fill_prob=0.5,
                maker_fee=0.0, maker_ev=0.02, maker_net_ev=0.01,
                taker_entry_price_cents=95, taker_fee=0.005,
                taker_ev=0.015, taker_net_ev=0.015,
                recommended_path="maker", recommended_entry_price=90,
                recommended_ev=0.01,
            )
            
            inputs = DecisionInput(
                market=market,
                spot_state={"spot": 100000.0, "sigma_per_min": 0.001},
                yes_bid=94, yes_ask=96, no_bid=4, no_ask=6,
                brier_score=0.25,  # Above threshold
            )
            result = make_decision(inputs)
        
        assert result.allow is False
        assert any("calibration" in r.lower() for r in result.reasons)

    def test_all_pass_allows(self, monkeypatch):
        """All checks pass -> allow."""
        monkeypatch.setattr(config, "GATES_ENABLED", True)
        monkeypatch.setattr(config, "GATES_SHADOW_MODE", False)
        monkeypatch.setattr(config, "FAIR_VALUE_ENABLED", True)
        monkeypatch.setattr(config, "FAIR_VALUE_MIN_EV", 0.001)
        monkeypatch.setattr(config, "GATES_KL_ENABLED", True)
        monkeypatch.setattr(config, "GATES_KL_BLOCK_THRESHOLD", 0.15)
        monkeypatch.setattr(config, "CALIBRATION_BREAKER_ENABLED", True)
        monkeypatch.setattr(config, "CALIBRATION_BRIER_THRESHOLD", 0.20)
        
        market = MockMarket()
        market.strike = 95000.0
        market.minutes_left = 8.0
        
        # Mock gate to pass
        mock_gate = Mock()
        mock_gate.allowed = True
        mock_gate.reasons = []
        mock_gate.metrics = {"decision": "allow"}
        
        with patch("decision.gates.evaluate_entry", return_value=mock_gate), \
             patch("decision.fair_value.compute_fair_value") as mock_fv:
            
            # Mock fair value to pass
            from fair_value import FairValueResult
            mock_fv.return_value = FairValueResult(
                p_hat=0.6, p_hat_lower=0.55, p_hat_upper=0.65,
                maker_entry_price_cents=90, maker_fill_prob=0.5,
                maker_fee=0.0, maker_ev=0.02, maker_net_ev=0.01,
                taker_entry_price_cents=95, taker_fee=0.005,
                taker_ev=0.015, taker_net_ev=0.015,
                recommended_path="maker", recommended_entry_price=90,
                recommended_ev=0.01,
            )
            
            inputs = DecisionInput(
                market=market,
                spot_state={"spot": 100000.0, "sigma_per_min": 0.001},
                yes_bid=94, yes_ask=96, no_bid=4, no_ask=6,
                kalshi_mid=95.0, polymarket_mid=94.5,  # Low KL
                brier_score=0.15,  # Good calibration
            )
            result = make_decision(inputs)
        
        assert result.allow is True
        assert result.recommended_path == "maker"
        assert result.recommended_price == 90


class TestShouldTrade:
    """Test convenience wrapper."""

    def test_should_trade_calls_make_decision(self, monkeypatch):
        """should_trade wraps make_decision correctly."""
        monkeypatch.setattr(config, "GATES_ENABLED", True)
        monkeypatch.setattr(config, "GATES_SHADOW_MODE", False)
        monkeypatch.setattr(config, "FAIR_VALUE_ENABLED", True)
        monkeypatch.setattr(config, "FAIR_VALUE_MIN_EV", 0.001)
        monkeypatch.setattr(config, "GATES_KL_ENABLED", False)
        monkeypatch.setattr(config, "CALIBRATION_BREAKER_ENABLED", False)
        
        market = MockMarket()
        
        mock_gate = Mock()
        mock_gate.allowed = True
        mock_gate.reasons = []
        mock_gate.metrics = {"decision": "allow"}
        
        with patch("decision.gates.evaluate_entry", return_value=mock_gate), \
             patch("decision.fair_value.compute_fair_value") as mock_fv:
            
            from fair_value import FairValueResult
            mock_fv.return_value = FairValueResult(
                p_hat=0.6, p_hat_lower=0.55, p_hat_upper=0.65,
                maker_entry_price_cents=90, maker_fill_prob=0.5,
                maker_fee=0.0, maker_ev=0.02, maker_net_ev=0.01,
                taker_entry_price_cents=95, taker_fee=0.005,
                taker_ev=0.015, taker_net_ev=0.015,
                recommended_path="maker", recommended_entry_price=90,
                recommended_ev=0.01,
            )
            
            result = should_trade(
                market=market,
                spot_state={"spot": 100000.0},
                side="yes",
                entry_price_cents=95,
                yes_bid=94, yes_ask=96, no_bid=4, no_ask=6,
            )
        
        assert result.allow is True


class TestFormatDecision:
    """Test decision formatting."""

    def test_format_decision_allow(self):
        """Format allowed decision."""
        result = DecisionResult(
            allow=True,
            reasons=["gate: allow", "fair_value: maker@$0.90 EV=$0.0100"],
            gate_result=Mock(metrics={"decision": "allow"}),  # need gate_result for format
            recommended_path="maker",
            recommended_price=90,
            recommended_ev=0.01,
        )
        out = decision.format_decision(result)
        assert "ALLOW" in out
        assert "gate=allow" in out

    def test_format_decision_block(self):
        """Format blocked decision."""
        result = DecisionResult(
            allow=False,
            reasons=["gate: block"],
            recommended_path="none",
            recommended_price=0,
            recommended_ev=0.0,
        )
        out = decision.format_decision(result)
        assert "BLOCK" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])