"""
llm_eval.py — LLM trade evaluation with a 7-layer fallback across free providers.

Layers are tried in order; a layer is skipped when its API key is unset or its
circuit breaker is open:

  1. NVIDIA  (NVIDIA_API_KEY, integrate.api.nvidia.com)
  2. OpenRouter free models (OPENROUTER_API_KEY)
  3. Groq free tier (GROQ_API_KEY)
  4. DeepSeek (DEEPSEEK_API_KEY)
  5. OpenAI (OPENAI_API_KEY)
  6. Gemini (GEMINI_API_KEY)
  7. DSH our-free-model local gateway (http://127.0.0.1:18900, no key)

Ollama local is deliberately not a layer.

Each call returns a dict:
  {"probability_of_net_profit": float (0-1), "confidence_hold_to_expiry": float (0-1),
   "reasoning": str, "layer": str, "model": str}

Returns None if every layer fails (caller decides fail-open vs fail-safe).
"""

import json
import logging
import os
import re
import stat
import time
from typing import Optional, Dict, Any

import requests

import config

log = logging.getLogger("llm_eval")

# ── Timeout and retry config ─────────────────────────────────────────────────
# Uses config._env_int / config._env_float (deduped — do not redefine here).
REQUEST_TIMEOUT = config._env_int("LLM_REQUEST_TIMEOUT", 20)
MAX_RETRIES_PER_LAYER = config._env_int("LLM_MAX_RETRIES", 2)
BACKOFF_BASE = config._env_float("LLM_BACKOFF_BASE", 1.0)


def _resolve_api_key(env_key: str) -> str:
    """Return the API key from env, or from a protected file via <ENV_KEY>_FILE.

    Keeps secrets out of `.env`/source: set NVIDIA_API_KEY_FILE to a chmod-600
    file instead of pasting the key inline. Refuses to read files with
    world-readable permissions (mode & 0o077), matching supabase_client.py.
    """
    key = os.environ.get(env_key, "")
    if key:
        return key
    path = os.environ.get(f"{env_key}_FILE", "")
    if path:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode & 0o077:
                log.warning(
                    "%s_FILE %s has insecure permissions (mode %o) — "
                    "refusing to read. Set chmod 600.", env_key, path, mode,
                )
                return ""
            return open(path).read().strip()
        except OSError as e:
            log.debug("Could not read %s_FILE (%s): %s", env_key, path, e)
    return ""

# ── Layer configurations ─────────────────────────────────────────────────────
# Each layer: (name, env_key, base_url, model, chat_path, auth_header_name)

LAYERS = [
    {
        "name": "nvidia",
        "env_key": "NVIDIA_API_KEY",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3.5-bolt-e4b"),
        "chat_path": "/chat/completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        # Fallback on the same NVIDIA endpoint (different model = different quota).
        # Must return a normal message.content: reasoning-only models (e.g.
        # nemotron-3-nano-omni) send content=null and never parse.
        "free_models": [
            "meta/llama-3.2-90b-vision-instruct",
        ],
    },
    {
        "name": "openrouter",
        "env_key": "OPENROUTER_API_KEY",
        "base_url": os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        "model": os.environ.get("OPENROUTER_MODEL", "openrouter/free"),
        "chat_path": "/chat/completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_models": [
            "openrouter/free", "openrouter/auto",
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
        "free_models": ["llama-3.1-8b-instant", "gemma2-9b-it"],
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
        "free_models": ["gpt-4o-mini"],
    },
    {
        "name": "gemini",
        "env_key": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "model": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        "chat_path": "/openai/chat/completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_models": ["gemini-2.5-flash"],
    },
    {
        "name": "dsh_our_free_model",
        "env_key": None,  # local gateway, no key
        "base_url": os.environ.get("DSH_BASE_URL", "http://127.0.0.1:18900"),
        "model": os.environ.get("DSH_MODEL", "free-model"),
        "chat_path": "/v1/chat/completions",
        "auth_header": None,
        "auth_prefix": None,
        "free_models": ["free-model", "kimi-k3-dsh", "deepseek-v4.1-flash-free"],
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

        prob = float(data.get("probability_of_net_profit", 0.0))
        conf = float(data.get("confidence_hold_to_expiry", 0.0))
        reasoning = data.get("reasoning", data.get("reason", ""))

        # Validate that ONLY documented keys are present. The system prompt
        # says "exactly these keys" — extra keys indicate a malformed
        # response that should not be trusted.
        expected_keys = {"probability_of_net_profit", "confidence_hold_to_expiry", "reasoning"}
        extra_keys = set(data.keys()) - expected_keys
        if extra_keys:
            raise ValueError(
                f"LLM response contained undocumented keys: "
                f"{', '.join(sorted(extra_keys))}. "
                f"System prompt requires exactly: {', '.join(sorted(expected_keys))}"
            )

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
        api_key = _resolve_api_key(layer["env_key"])
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
                choices = data.get("choices", [])
                if not choices:
                    continue
                content = choices[0].get("message", {}).get("content") or ""
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
    Evaluate a trade signal using the NVIDIA LLM API.

    Args:
        signal_info: dict with keys like ticker, direction, entry_price,
                     contracts, dollar_risk, cash, exposure, total_value,
                     mid, skew, trend, minutes_left, source, strength

    Returns:
        dict with probability_of_net_profit, confidence_hold_to_expiry,
        reasoning, layer, model — or None if the API call fails.
    """
    system_prompt = (
        "You are a disciplined Kalshi 15-minute crypto market trading analyst. "
        "The strategy you are grading is a FAVOURITE MAKER: post a maker limit order on the favourite side, "
        "roughly 1c inside the spread, 6-11 minutes into the 15-minute window, in a 15-55c entry band. "
        "Maker orders pay no fee on these series; a taker order pays 7% x price x (1-price). "
        "The position is held to settlement unless an early exit fires at +20c take-profit or -8c stop. "
        "Grade the SPECIFIC signal you are given, using its own price, skew, trend, source and minutes_left. "
        "Note there is no edge in simply buying a favourite: an entry at 52c needs a win rate above 52% to break even. "
        "Weigh whether the signal's trend and skew justify a win rate above its own entry price, and be sceptical of weak trends. "
        "Return ONLY valid JSON with exactly these keys: "
        "\"probability_of_net_profit\" (number 0-1) and "
        "\"confidence_hold_to_expiry\" (number 0-1). "
        "Optionally include \"reasoning\" (string). No other keys, no extra text."
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
                "LLM evaluation deadline (%.0fs) reached — stopping after %d completed layers",
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

    log.warning("NVIDIA LLM eval failed — last error: %s", last_error)
    return None


def should_block_trade(signal_info: dict, min_probability: float = 0.65, min_confidence: float = 0.45) -> tuple:
    """
    Determine if a trade should be blocked based on LLM evaluation.
    
    Returns (should_block: bool, eval_result: dict or None).
    Blocks if probability < min_probability OR confidence < min_confidence.
    """
    
    result = evaluate_trade(signal_info)
    if not result:
        if config.LLM_EVAL_FAIL_OPEN:
            log.warning("NVIDIA LLM eval failed — fail-open, allowing trade")
            return False, None
        return True, None  # Can't evaluate — block by default (fail-safe)
    
    prob = result["probability_of_net_profit"]
    conf = result.get("confidence_hold_to_expiry", 0.0)

    # Use the caller's thresholds. Reading config here made the arguments
    # no-ops, so a caller that tightened them silently changed nothing.
    should_block = prob < min_probability or conf < min_confidence
    if should_block:
        log.info(
            "LLM BLOCK %s — prob=%.2f conf=%.2f (min_prob=%.2f min_conf=%.2f)",
            signal_info.get("ticker", "?"), prob, conf,
            min_probability, config.LLM_EVAL_MIN_CONFIDENCE
        )
    return should_block, result