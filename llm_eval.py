"""
llm_eval.py — LLM trade evaluation with 7-layer fallback from free model providers.

Layers (tried in order, each with independent retry):
  1. OpenRouter free models (OPENROUTER_API_KEY)
  2. Groq free tier (GROQ_API_KEY)
  3. DeepSeek free tier (DEEPSEEK_API_KEY)
  4. OpenAI free tier (OPENAI_API_KEY)
  5. Gemini free tier (GEMINI_API_KEY)
  6. DSH our-free-model local gateway (http://127.0.0.1:18900)
  7. Ollama local (http://127.0.0.1:11434)

Each layer returns a dict:
  {"probability_of_net_profit": float (0-1), "confidence_hold_to_expiry": float (0-1),
   "reasoning": str, "layer": str}

Returns None if all layers fail.
"""

import json
import logging
import os
import re
import time
from typing import Optional, Dict, Any

import requests

import config

log = logging.getLogger("llm_eval")

# ── Timeout and retry config ─────────────────────────────────────────────────
def _env_int(name, default):
    """Read an integer from the environment, falling back to default on any error."""
    try:
        return int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default

def _env_float(name, default):
    """Read a float from the environment, falling back to default on any error."""
    try:
        return float(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default

REQUEST_TIMEOUT = _env_int("LLM_REQUEST_TIMEOUT", 8)
MAX_RETRIES_PER_LAYER = _env_int("LLM_MAX_RETRIES", 2)
BACKOFF_BASE = _env_float("LLM_BACKOFF_BASE", 1.0)

# ── Layer configurations ─────────────────────────────────────────────────────
# Each layer: (name, env_key, base_url, model, chat_path, auth_header_name)

LAYERS = [
    {
        "name": "openrouter",
        "env_key": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "model": os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4"),
        "chat_path": "/chat/completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_models": [
            "openrouter/auto", "openrouter/free", "openrouter/openrouter-free-router",
            "meta-llama/llama-3.3-70b-instruct:free",
            "google/gemma-2-9b-it:free",
        ],
    },
    {
        "name": "groq",
        "env_key": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "model": os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant"),
        "chat_path": "/chat/completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_models": [
            "llama-3.1-8b-instant", "gemma2-9b-it", "mixtral-8x7b-32768",
        ],
    },
    {
        "name": "deepseek",
        "env_key": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/v1",
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "chat_path": "/chat/completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_models": ["deepseek-chat"],
    },
    {
        "name": "openai",
        "env_key": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "chat_path": "/chat/completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_models": ["gpt-4o-mini", "gpt-3.5-turbo"],
    },
    {
        "name": "gemini",
        "env_key": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "model": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        "chat_path": "/openai/chat/completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_models": ["gemini-2.5-flash", "gemini-1.5-flash"],
    },
    {
        "name": "dsh_our_free_model",
        "env_key": None,  # No API key needed for local gateway
        "base_url": "http://127.0.0.1:18900",
        "model": os.environ.get("DSH_MODEL", "free-model"),
        "chat_path": "/v1/chat/completions",
        "auth_header": None,
        "auth_prefix": None,
        "free_models": ["free-model", "kimi-k3-dsh", "deepseek-v4.1-flash-free"],
    },
    {
        "name": "ollama",
        "env_key": None,
        "base_url": "http://127.0.0.1:11434",
        "model": os.environ.get("OLLAMA_MODEL", "gemma3:1b"),
        "chat_path": "/api/chat",
        "auth_header": None,
        "auth_prefix": None,
        "free_models": ["gemma3:1b", "llama3.2", "qwen2.5"],
    },
]

# ── Circuit breaker for failing LLM layers ──
CIRCUIT_BREAKER_THRESHOLD = 3   # consecutive failures before skipping a layer
CIRCUIT_BREAKER_COOLDOWN = 60.0  # seconds to skip a layer after threshold
_layer_failures: dict[str, int] = {}
_layer_skip_until: dict[str, float] = {}


def _is_circuit_open(layer_name: str) -> bool:
    """Return True if the layer is currently skipped due to repeated failures."""
    skip_until = _layer_skip_until.get(layer_name, 0.0)
    if skip_until <= 0:
        return False
    if time.time() >= skip_until:
        # Cooldown expired — reset and allow retry
        _layer_skip_until.pop(layer_name, None)
        _layer_failures.pop(layer_name, None)
        return False
    return True


def _record_layer_failure(layer_name: str) -> None:
    """Increment failure count; open circuit if threshold reached."""
    _layer_failures[layer_name] = _layer_failures.get(layer_name, 0) + 1
    if _layer_failures[layer_name] >= CIRCUIT_BREAKER_THRESHOLD:
        _layer_skip_until[layer_name] = time.time() + CIRCUIT_BREAKER_COOLDOWN
        log.warning(
            "LLM layer %s circuit opened — skipping for %.0fs after %d failures",
            layer_name, CIRCUIT_BREAKER_COOLDOWN, _layer_failures[layer_name],
        )


def _record_layer_success(layer_name: str) -> None:
    """Reset failure count on a successful call."""
    if layer_name in _layer_failures:
        _layer_failures.pop(layer_name, None)
    if layer_name in _layer_skip_until:
        _layer_skip_until.pop(layer_name, None)


def _build_messages(system_prompt: str, user_prompt: str) -> list:
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?", re.MULTILINE)


def _strip_fences(text: str) -> str:
    """Remove markdown code fences (```json ... ``` or ``` ... ```)."""
    lines = text.splitlines()
    if not lines:
        return text
    # Strip opening fence on first line
    if _FENCE_RE.match(lines[0]):
        lines = lines[1:]
    # Strip closing fence on last line
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _extract_json_object(text: str) -> str:
    """Find the first '{' and last '}' and return the substring between them."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ""
    return text[start:end + 1]


def _parse_llm_response(content: str) -> Optional[Dict[str, Any]]:
    """Parse LLM response text into evaluation dict.

    Strategy:
      1. Strip markdown code fences.
      2. Try json.loads on the whole cleaned content.
      3. Fall back to brace-finding only if step 2 fails.
    """
    try:
        cleaned = _strip_fences(content)

        # 1. Try parsing the entire cleaned content as JSON.
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # 2. Fall back to extracting the first { … last } block.
            snippet = _extract_json_object(cleaned)
            if not snippet:
                return None
            data = json.loads(snippet)

        prob = float(data.get("probability_of_net_profit", data.get("probability", 0.0)))
        conf = float(data.get("confidence_hold_to_expiry", data.get("confidence", 0.0)))
        reasoning = data.get("reasoning", data.get("reason", ""))
        return {
            "probability_of_net_profit": max(0.0, min(1.0, prob)),
            "confidence_hold_to_expiry": max(0.0, min(1.0, conf)),
            "reasoning": reasoning,
        }
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        log.debug("LLM response parse failed: %s — content: %s", e, content[:200])
        return None


def _call_layer(layer: dict, messages: list, deadline: float | None = None) -> Optional[Dict[str, Any]]:
    """Call a single LLM layer. Returns parsed result or None."""
    api_key = None
    if layer["env_key"]:
        api_key = os.environ.get(layer["env_key"], "")
        if not api_key:
            log.debug("Layer %s skipped: no API key", layer["name"])
            return None

    base_url = layer["base_url"].rstrip("/")
    chat_url = base_url + layer["chat_path"]

    headers = {"Content-Type": "application/json"}
    if layer["auth_header"] and api_key:
        headers[layer["auth_header"]] = layer["auth_prefix"] + api_key

    # Try primary model, then free models
    models_to_try = [layer["model"]] + layer.get("free_models", [])

    for model in models_to_try:
        # Check deadline before each model attempt
        if deadline and time.time() >= deadline:
            log.debug("Layer %s deadline reached, skipping model %s", layer["name"], model)
            return None

        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 256,
        }

        # Ollama uses different payload format
        if layer["name"] == "ollama":
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": 0.1},
            }

        for attempt in range(MAX_RETRIES_PER_LAYER):
            # Check deadline before each retry attempt
            if deadline and time.time() >= deadline:
                log.debug("Layer %s deadline reached during retries", layer["name"])
                return None
            try:
                resp = requests.post(
                    chat_url, headers=headers, json=payload,
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", "1"))
                    log.debug("Layer %s rate limited, waiting %.1fs", layer["name"], wait)
                    time.sleep(min(wait, 5))
                    continue
                if resp.status_code != 200:
                    log.debug("Layer %s model %s: HTTP %s",
                              layer["name"], model, resp.status_code)
                    break  # Try next model
                data = resp.json()
                if layer["name"] == "ollama":
                    content = data.get("message", {}).get("content", "")
                else:
                    choices = data.get("choices", [])
                    if not choices:
                        continue
                    content = choices[0].get("message", {}).get("content", "")
                result = _parse_llm_response(content)
                if result:
                    result["layer"] = layer["name"]
                    result["model"] = model
                    return result
            except requests.RequestException as e:
                log.debug("Layer %s model %s attempt %d failed: %s",
                          layer["name"], model, attempt + 1, e)
                time.sleep(BACKOFF_BASE * (attempt + 1))
                continue

    return None


def evaluate_trade(signal_info: dict) -> Optional[Dict[str, Any]]:
    """
    Evaluate a trade signal using LLM with 7-layer fallback.

    Args:
        signal_info: dict with keys like ticker, direction, entry_price,
                     contracts, dollar_risk, cash, exposure, total_value,
                     mid, skew, trend, minutes_left, source, strength

    Returns:
        dict with probability_of_net_profit, confidence_hold_to_expiry,
        reasoning, layer, model — or None if all layers fail.
    """
    system_prompt = (
        "You are a disciplined Kalshi 15-minute market trading analyst. "
        "Return ONLY valid JSON with exactly these keys: "
        "\"probability_of_net_profit\" (number 0-1) and "
        "\"confidence_hold_to_expiry\" (number 0-1). "
        "Optionally include \"reasoning\" (string) explaining the verdict. "
        "Consider: market skew, trend, entry price vs mid, time remaining in "
        "the 15-minute window, position sizing vs cash, and Kalshi's 7% "
        "quadratic taker fee. Be conservative — most 15-minute binary markets "
        "are close to coin flips at entry. Do not include any other keys or "
        "text in your response."
    )

    user_prompt = json.dumps({
        "ticker": signal_info.get("ticker", ""),
        "direction": signal_info.get("direction", ""),
        "entry_price_cents": signal_info.get("entry_price", 0),
        "contracts": signal_info.get("contracts", 0),
        "dollar_risk": signal_info.get("dollar_risk", 0.0),
        "cash": signal_info.get("cash", 0.0),
        "exposure": signal_info.get("exposure", 0.0),
        "total_value": signal_info.get("total_value", 0.0),
        "mid_cents": signal_info.get("mid", 50),
        "skew_cents": signal_info.get("skew", 0),
        "trend_cents": signal_info.get("trend", 0),
        "minutes_left": signal_info.get("minutes_left", 0),
        "signal_source": signal_info.get("source", ""),
        "signal_strength": signal_info.get("strength", 0),
    })

    messages = _build_messages(system_prompt, user_prompt)

    last_error = None
    deadline = time.time() + config.LLM_EVAL_DEADLINE_SECONDS
    layer_idx = 0

    for layer in LAYERS:
        # Stop evaluating layers once we're close to the deadline so a slow
        # LLM can't blow the cycle timeout.
        if time.time() >= deadline:
            log.warning(
                "LLM evaluation deadline (%.0fs) reached — stopping after %d layers",
                config.LLM_EVAL_DEADLINE_SECONDS, layer_idx,
            )
            break
        layer_idx += 1
        layer_name = layer["name"]
        if _is_circuit_open(layer_name):
            log.debug("Layer %s skipped: circuit breaker open", layer_name)
            continue
        try:
            result = _call_layer(layer, messages, deadline)
            if result:
                _record_layer_success(layer_name)
                log.info(
                    "LLM eval via %s (model=%s): prob=%.2f conf=%.2f",
                    result["layer"], result.get("model", "?"),
                    result["probability_of_net_profit"],
                    result["confidence_hold_to_expiry"],
                )
                return result
            else:
                _record_layer_failure(layer_name)
        except Exception as e:
            last_error = e
            _record_layer_failure(layer_name)
            log.debug("Layer %s failed: %s", layer_name, e)
            continue

    log.warning("All 7 LLM layers failed — last error: %s", last_error)
    return None


def should_block_trade(signal_info: dict, min_probability: float = 0.35) -> tuple:
    """
    Determine if a trade should be blocked based on LLM evaluation.

    Returns (should_block: bool, eval_result: dict or None).
    """
    import config

    result = evaluate_trade(signal_info)
    if not result:
        if config.LLM_EVAL_FAIL_OPEN:
            log.warning("All LLM layers failed — fail-open, allowing trade")
            return False, None
        return True, None  # Can't evaluate — block by default (fail-safe)
    prob = result["probability_of_net_profit"]
    if prob < min_probability:
        return True, result
    return False, result