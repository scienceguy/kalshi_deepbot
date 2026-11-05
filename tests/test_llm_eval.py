"""Tests for llm_eval.py — 7-layer provider fallback and response parsing.

Every provider call is mocked; no network is touched.
"""
from unittest.mock import MagicMock, patch

import llm_eval


class TestLayers:
    def test_seven_layers_in_fallback_order(self):
        names = [l["name"] for l in llm_eval.LAYERS]
        assert names == [
            "nvidia", "openrouter", "groq", "deepseek", "openai", "gemini",
            "dsh_our_free_model",
        ]

    def test_nvidia_is_first_and_ollama_is_gone(self):
        first = llm_eval.LAYERS[0]
        assert first["env_key"] == "NVIDIA_API_KEY"
        assert first["base_url"] == "https://integrate.api.nvidia.com/v1"
        assert "ollama" not in [l["name"] for l in llm_eval.LAYERS]
        assert not any("11434" in l["base_url"] for l in llm_eval.LAYERS)

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

    def test_without_provider_keys_only_the_local_gateway_is_tried(self):
        """No key means no remote call — only the local layer is reachable."""
        resp = MagicMock()
        resp.status_code = 500
        env = llm_eval.os.environ
        with patch.dict("os.environ", {}, clear=False), \
             patch.object(llm_eval.requests, "post", return_value=resp) as post:
            for name in ("NVIDIA", "OPENROUTER", "GROQ", "DEEPSEEK", "OPENAI", "GEMINI"):
                env.pop(f"{name}_API_KEY", None)
                env.pop(f"{name}_API_KEY_FILE", None)
            result = llm_eval.evaluate_trade({"ticker": "KXBTC15M-T1"})

        assert result is None
        urls = {c[0][0] for c in post.call_args_list}
        assert urls == {"http://127.0.0.1:18900/v1/chat/completions"}

    def test_fail_open_allows_the_trade_when_every_layer_fails(self, monkeypatch):
        monkeypatch.setattr(llm_eval.config, "LLM_EVAL_FAIL_OPEN", True)
        with patch.object(llm_eval, "evaluate_trade", return_value=None):
            blocked, result = llm_eval.should_block_trade({})
        assert blocked is False
        assert result is None

    def test_fail_closed_blocks_the_trade_when_configured(self, monkeypatch):
        monkeypatch.setattr(llm_eval.config, "LLM_EVAL_FAIL_OPEN", False)
        with patch.object(llm_eval, "evaluate_trade", return_value=None):
            blocked, _ = llm_eval.should_block_trade({})
        assert blocked is True

    def test_min_probability_argument_is_honoured(self):
        verdict = {"probability_of_net_profit": 0.5,
                   "confidence_hold_to_expiry": 0.9}
        with patch.object(llm_eval, "evaluate_trade", return_value=verdict):
            lenient, _ = llm_eval.should_block_trade({}, min_probability=0.4)
            strict, _ = llm_eval.should_block_trade({}, min_probability=0.6)
        assert lenient is False
        assert strict is True

    def test_should_block_when_probability_low(self):
        with patch.object(llm_eval, "evaluate_trade",
                          return_value={"probability_of_net_profit": 0.1}):
            blocked, result = llm_eval.should_block_trade({}, min_probability=0.35)
        assert blocked is True
        assert result["probability_of_net_profit"] == 0.1

    def test_content_null_handled_gracefully(self):
        """content: null in response should not crash; returns None to try next model."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": None  # reasoning-only models return null
                }
            }]
        }
        with patch.object(llm_eval, "REQUEST_TIMEOUT", 1), \
             patch.dict("os.environ", {"NVIDIA_API_KEY": "test-key"}), \
             patch.object(llm_eval.requests, "post", return_value=resp):
            result = llm_eval.evaluate_trade({"ticker": "KXBTC15M-T1"})

        assert result is None  # falls back / returns None instead of crashing
