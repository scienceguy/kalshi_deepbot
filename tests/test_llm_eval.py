"""Tests for llm_eval.py — NVIDIA-only evaluation path.

Covers the response parser and that evaluate_trade() uses exactly one
provider (NVIDIA), with a mocked HTTP call so no network is touched.
"""
from unittest.mock import MagicMock, patch

import llm_eval


class TestLayers:
    def test_single_nvidia_layer(self):
        """Exactly one layer, and it is the NVIDIA endpoint."""
        assert len(llm_eval.LAYERS) == 1
        layer = llm_eval.LAYERS[0]
        assert layer["name"] == "nvidia"
        assert layer["env_key"] == "NVIDIA_API_KEY"
        assert layer["base_url"] == "https://integrate.api.nvidia.com/v1"

    def test_primary_model_not_duplicated_in_fallbacks(self):
        """The primary model must not also appear in free_models (wasted retry)."""
        layer = llm_eval.LAYERS[0]
        assert layer["model"] not in layer.get("free_models", [])


class TestParseResponse:
    def test_parses_plain_json(self):
        r = llm_eval._parse_llm_response(
            '{"probability_of_net_profit": 0.7, "confidence_hold_to_expiry": 0.8}'
        )
        assert r["probability_of_net_profit"] == 0.7
        assert r["confidence_hold_to_expiry"] == 0.8

    def test_parses_fenced_json(self):
        r = llm_eval._parse_llm_response(
            '```json\n{"probability_of_net_profit": 0.2, "confidence_hold_to_expiry": 0.3}\n```'
        )
        assert r["probability_of_net_profit"] == 0.2

    def test_clamps_out_of_range(self):
        r = llm_eval._parse_llm_response(
            '{"probability_of_net_profit": 5, "confidence_hold_to_expiry": -2}'
        )
        assert r["probability_of_net_profit"] == 1.0
        assert r["confidence_hold_to_expiry"] == 0.0

    def test_garbage_returns_none(self):
        assert llm_eval._parse_llm_response("not json at all") is None


class TestEvaluateTrade:
    def test_uses_nvidia_and_returns_result(self):
        """evaluate_trade hits the NVIDIA URL once and returns the parsed verdict."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"probability_of_net_profit": 0.6, "confidence_hold_to_expiry": 0.5}'
                }
            }]
        }
        with patch.object(llm_eval, "REQUEST_TIMEOUT", 1), \
             patch.dict("os.environ", {"NVIDIA_API_KEY": "test-key"}), \
             patch.object(llm_eval.requests, "post", return_value=resp) as post:
            result = llm_eval.evaluate_trade({"ticker": "KXBTC15M-T1", "entry_price": 80})

        assert result is not None
        assert result["layer"] == "nvidia"
        assert result["probability_of_net_profit"] == 0.6
        assert post.call_args[0][0] == "https://integrate.api.nvidia.com/v1/chat/completions"

    def test_no_key_returns_none(self):
        """With no NVIDIA key, evaluation fails cleanly instead of hitting the net."""
        with patch.dict("os.environ", {}, clear=False), \
             patch.object(llm_eval.requests, "post") as post:
            llm_eval.os.environ.pop("NVIDIA_API_KEY", None)
            result = llm_eval.evaluate_trade({"ticker": "KXBTC15M-T1"})

        assert result is None
        post.assert_not_called()

    def test_should_block_when_probability_low(self):
        with patch.object(llm_eval, "evaluate_trade",
                          return_value={"probability_of_net_profit": 0.1}):
            blocked, result = llm_eval.should_block_trade({}, min_probability=0.35)
        assert blocked is True
        assert result["probability_of_net_profit"] == 0.1
