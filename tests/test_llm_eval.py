"""Integration tests for llm_eval.py trade evaluation."""

import pytest
import time
from unittest.mock import Mock, patch, MagicMock

import config
import llm_eval


class TestLLMEvalShouldBlockTrade:
    """Test llm_eval.should_block_trade decision logic."""

    def test_should_block_trade_fails_open_when_eval_none(self):
        """When eval returns None and FAIL_OPEN=true, should not block."""
        with patch.object(config, 'LLM_EVAL_FAIL_OPEN', True):
            with patch('llm_eval.evaluate_trade', return_value=None):
                signal_info = {"ticker": "TEST"}
                should_block, result = llm_eval.should_block_trade(signal_info, min_probability=0.65)
                assert should_block is False
                assert result is None

    def test_should_block_trade_blocks_when_eval_none_and_fail_closed(self):
        """When eval returns None and FAIL_OPEN=false, should block."""
        with patch.object(config, 'LLM_EVAL_FAIL_OPEN', False):
            with patch('llm_eval.evaluate_trade', return_value=None):
                signal_info = {"ticker": "TEST"}
                should_block, result = llm_eval.should_block_trade(signal_info, min_probability=0.65)
                assert should_block is True
                assert result is None

    def test_should_block_trade_blocks_when_prob_below_threshold(self):
        """Should block when probability < min_probability."""
        with patch.object(config, 'LLM_EVAL_FAIL_OPEN', True):
            with patch.object(config, 'LLM_EVAL_MIN_CONFIDENCE', 0.45):
                signal_info = {"ticker": "TEST"}
                eval_result = {
                    "probability_of_net_profit": 0.50,  # Below 0.65 default
                    "confidence_hold_to_expiry": 0.80,
                    "layer": "test",
                    "model": "test-model"
                }
                with patch('llm_eval.evaluate_trade', return_value=eval_result):
                    should_block, result = llm_eval.should_block_trade(signal_info, min_probability=0.65)
                    assert should_block is True
                    assert result["probability_of_net_profit"] == 0.50

    def test_should_block_trade_blocks_when_conf_below_threshold(self):
        """Should block when confidence < LLM_EVAL_MIN_CONFIDENCE."""
        with patch.object(config, 'LLM_EVAL_FAIL_OPEN', True):
            with patch.object(config, 'LLM_EVAL_MIN_CONFIDENCE', 0.45):
                signal_info = {"ticker": "TEST"}
                eval_result = {
                    "probability_of_net_profit": 0.80,  # Above threshold
                    "confidence_hold_to_expiry": 0.30,  # Below 0.45
                    "layer": "test",
                    "model": "test-model"
                }
                with patch('llm_eval.evaluate_trade', return_value=eval_result):
                    should_block, result = llm_eval.should_block_trade(signal_info, min_probability=0.65)
                    assert should_block is True
                    assert result["confidence_hold_to_expiry"] == 0.30

    def test_should_block_trade_allows_when_both_above_threshold(self):
        """Should not block when both prob and conf are above thresholds."""
        with patch.object(config, 'LLM_EVAL_FAIL_OPEN', True):
            with patch.object(config, 'LLM_EVAL_MIN_CONFIDENCE', 0.45):
                signal_info = {"ticker": "TEST"}
                eval_result = {
                    "probability_of_net_profit": 0.80,
                    "confidence_hold_to_expiry": 0.80,
                    "layer": "test",
                    "model": "test-model"
                }
                with patch('llm_eval.evaluate_trade', return_value=eval_result):
                    should_block, result = llm_eval.should_block_trade(signal_info, min_probability=0.65)
                    assert should_block is False

    def test_should_block_trade_respects_custom_min_probability(self):
        """Should use caller's min_probability argument."""
        with patch.object(config, 'LLM_EVAL_FAIL_OPEN', True):
            with patch.object(config, 'LLM_EVAL_MIN_CONFIDENCE', 0.45):
                signal_info = {"ticker": "TEST"}
                eval_result = {
                    "probability_of_net_profit": 0.70,  # Above 0.65 but below 0.75
                    "confidence_hold_to_expiry": 0.80,
                    "layer": "test",
                    "model": "test-model"
                }
                with patch('llm_eval.evaluate_trade', return_value=eval_result):
                    # With min_probability=0.75, 0.70 should block
                    should_block, result = llm_eval.should_block_trade(signal_info, min_probability=0.75)
                    assert should_block is True
                    
                    # With min_probability=0.65, 0.70 should not block
                    should_block, result = llm_eval.should_block_trade(signal_info, min_probability=0.65)
                    assert should_block is False


class TestLLMEvalParseResponse:
    """Test LLM response parsing."""

    def test_parse_valid_response(self):
        """Should parse valid JSON response correctly."""
        content = '{"probability_of_net_profit": 0.7, "confidence_hold_to_expiry": 0.8, "reasoning": "test"}'
        result = llm_eval._parse_llm_response(content)
        assert result is not None
        assert result["probability_of_net_profit"] == 0.7
        assert result["confidence_hold_to_expiry"] == 0.8
        assert result["reasoning"] == "test"

    def test_parse_response_with_code_fences(self):
        """Should handle markdown code fences."""
        content = '```json\n{"probability_of_net_profit": 0.75, "confidence_hold_to_expiry": 0.85, "reasoning": "test"}\n```'
        result = llm_eval._parse_llm_response(content)
        assert result is not None
        assert result["probability_of_net_profit"] == 0.75
        assert result["confidence_hold_to_expiry"] == 0.85

    def test_parse_invalid_json_returns_none(self):
        """Should return None for invalid JSON."""
        content = "not valid json"
        result = llm_eval._parse_llm_response(content)
        assert result is None

    def test_parse_extra_keys_raises_error(self):
        """Should reject responses with extra keys."""
        content = '{"probability_of_net_profit": 0.7, "confidence_hold_to_expiry": 0.8, "extra_key": "not_allowed"}'
        result = llm_eval._parse_llm_response(content)
        assert result is None

    def test_parse_clamped_values(self):
        """Should clamp probability and confidence to [0, 1]."""
        content = '{"probability_of_net_profit": 1.5, "confidence_hold_to_expiry": -0.2}'
        result = llm_eval._parse_llm_response(content)
        assert result is not None
        assert result["probability_of_net_profit"] == 1.0
        assert result["confidence_hold_to_expiry"] == 0.0


class TestLLMEvalConfig:
    """Test LLM eval configuration."""

    def test_min_probability_config(self):
        """LLM_EVAL_MIN_PROBABILITY should be configurable via min_probability parameter."""
        with patch.object(config, 'LLM_EVAL_MIN_CONFIDENCE', 0.45):
            signal_info = {"ticker": "TEST"}
            eval_result = {
                "probability_of_net_profit": 0.75,  # Below 0.8
                "confidence_hold_to_expiry": 0.80,
                "layer": "test",
                "model": "test-model"
            }
            with patch('llm_eval.evaluate_trade', return_value=eval_result):
                with patch.object(config, 'LLM_EVAL_FAIL_OPEN', True):
                    # Pass min_probability=0.8 explicitly (function uses parameter, not config)
                    should_block, result = llm_eval.should_block_trade(signal_info, min_probability=0.8)
                    assert should_block is True

    def test_min_confidence_config(self):
        """LLM_EVAL_MIN_CONFIDENCE should be configurable."""
        with patch.object(config, 'LLM_EVAL_MIN_PROBABILITY', 0.65):
            with patch.object(config, 'LLM_EVAL_MIN_CONFIDENCE', 0.60):
                signal_info = {"ticker": "TEST"}
                eval_result = {
                    "probability_of_net_profit": 0.80,
                    "confidence_hold_to_expiry": 0.55,  # Below 0.60
                    "layer": "test",
                    "model": "test-model"
                }
                with patch('llm_eval.evaluate_trade', return_value=eval_result):
                    with patch.object(config, 'LLM_EVAL_FAIL_OPEN', True):
                        should_block, result = llm_eval.should_block_trade(signal_info)
                        assert should_block is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])