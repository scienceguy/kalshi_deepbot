"""
llm_eval.py — LLM trade evaluation with an 8-layer fallback across providers.

Layers are tried in order; a layer is skipped when its API key / ADC is unset or
its circuit breaker is open:

  1. Vertex AI (Google Cloud ADC at ~/.config/gcloud/application_default_credentials.json)
  2. NVIDIA  (NVIDIA_API_KEY, integrate.api.nvidia.com)
  3. OpenRouter free models (OPENROUTER_API_KEY)
  4. Groq free tier (GROQ_API_KEY)
  5. DeepSeek (DEEPSEEK_API_KEY)
  6. OpenAI (OPENAI_API_KEY)
  7. Gemini (GEMINI_API_KEY)
  8. DSH our-free-model local gateway (http://127.0.0.1:18900, no key)

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
REQUEST_TIMEOUT = config._env_int("LLM_REQUEST_TIMEOUT", 5)
MAX_RETRIES_PER_LAYER = config._env_int("LLM_MAX_RETRIES", 1)
BACKOFF_BASE = config._env_float("LLM_BACKOFF_BASE", 0.5)


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
    from pathlib import Path
    bw_file = Path.home() / ".secrets" / "bw" / f"{env_key.lower()}.json"
    if bw_file.exists():
        try:
            mode = stat.S_IMODE(os.stat(bw_file).st_mode)
            if not (mode & 0o077):
                data = json.loads(bw_file.read_text())
                val = data.get("value", "").strip()
                if val:
                    return val
        except (OSError, ValueError):
            pass
    return ""


# Aliases for backward compatibility and test harnesses
_load_key = _resolve_api_key
load_key = _resolve_api_key


# ── Vertex AI OAuth2 ADC token cache ─────────────────────────────────────────
_vertex_token_cache: dict[str, Any] = {"token": "", "expires_at": 0.0}
VERTEX_DEFAULT_ADC_PATH = os.path.expanduser(
    "~/.config/gcloud/application_default_credentials.json"
)
VERTEX_DEFAULT_PROJECT = "project-ba3748e7-0d3a-4268-b7c"
VERTEX_DEFAULT_LOCATION = "global"


def _resolve_vertex_token() -> str:
    """Return a valid OAuth2 access token for Vertex AI using ADC refresh token.

    Reads GOOGLE_APPLICATION_CREDENTIALS or VERTEX_ADC_FILE (falling back to
    ~/.config/gcloud/application_default_credentials.json), exchanges the
    authorized_user refresh_token at https://oauth2.googleapis.com/token, and
    caches the short-lived access_token until 60s before expiry.
    """
    now = time.time()
    cached = _vertex_token_cache.get("token", "")
    if cached and now < _vertex_token_cache.get("expires_at", 0.0):
        return cached

    env_token = os.environ.get("VERTEX_ACCESS_TOKEN", "")
    if env_token:
        return env_token

    adc_path = (
        os.environ.get("VERTEX_ADC_FILE")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or VERTEX_DEFAULT_ADC_PATH
    )
    if not os.path.exists(adc_path):
        return ""

    try:
        mode = stat.S_IMODE(os.stat(adc_path).st_mode)
        if mode & 0o077:
            log.warning(
                "Vertex ADC file %s has insecure permissions (mode %o) — "
                "refusing to read. Set chmod 600.", adc_path, mode,
            )
            return ""
        with open(adc_path) as f:
            adc = json.load(f)
    except (OSError, ValueError) as e:
        log.debug("Could not read Vertex ADC (%s): %s", adc_path, e)
        return ""

    client_id = adc.get("client_id", "")
    client_secret = adc.get("client_secret", "")
    refresh_token = adc.get("refresh_token", "")
    if not (client_id and client_secret and refresh_token):
        return ""

    try:
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=min(10, REQUEST_TIMEOUT),
        )
        if resp.status_code != 200:
            log.debug("Vertex OAuth token refresh HTTP %s", resp.status_code)
            return ""
        data = resp.json()
        token = data.get("access_token", "")
        expires_in = float(data.get("expires_in", 3600))
        if token:
            _vertex_token_cache["token"] = token
            _vertex_token_cache["expires_at"] = now + max(60.0, expires_in - 60.0)
        return token
    except (requests.RequestException, ValueError) as e:
        log.debug("Vertex OAuth token refresh failed: %s", e)
        return ""


def _vertex_base_url() -> str:
    explicit = os.environ.get("VERTEX_BASE_URL", "")
    if explicit:
        return explicit
    project = os.environ.get("VERTEX_PROJECT_ID", VERTEX_DEFAULT_PROJECT)
    location = os.environ.get("VERTEX_LOCATION", VERTEX_DEFAULT_LOCATION)
    host = (
        "https://aiplatform.googleapis.com"
        if location == "global"
        else f"https://{location}-aiplatform.googleapis.com"
    )
    return f"{host}/v1beta1/projects/{project}/locations/{location}/endpoints/openapi"


# ── Layer configurations ─────────────────────────────────────────────────────
# Each layer: (name, env_key, base_url, model, chat_path, auth_header_name)

LAYERS = [
    {
        "name": "nvidia_bolt",
        "env_key": "NVIDIA_API_KEY",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "nvidia/nemotron-3.5-bolt-e4b",
        "chat_path": "/chat/completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "max_tokens": 256,
        "free_models": [
            "nvidia/nemotron-3.5-bolt-e4b",
        ],
    },
    {
        "name": "groq_qwen",
        "env_key": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "qwen/qwen3.8-27b",
        "chat_path": "/chat/completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "max_tokens": 256,
        "free_models": [
            "qwen/qwen3.8-27b",
        ],
    },
    {
        "name": "groq_allam",
        "env_key": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "allam-2-7b",
        "chat_path": "/chat/completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "max_tokens": 256,
        "free_models": [
            "allam-2-7b",
        ],
    },
    {
        "name": "nvidia_lightning",
        "env_key": "NVIDIA_API_KEY",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "nvidia/nemotron-3.5-lightning-30b-a3b",
        "chat_path": "/chat/completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "max_tokens": 600,
        "free_models": [
            "nvidia/nemotron-3.5-lightning-30b-a3b",
        ],
    },
]

# ── Circuit breaker for failing LLM layers ──
CIRCUIT_BREAKER_THRESHOLD = 3   # consecutive failures before skipping a layer
# Cooldown should be <= LLM_EVAL_DEADLINE_SECONDS so a skipped layer can
# retry on the next cycle. Default 25s.
CIRCUIT_BREAKER_COOLDOWN = 25.0
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
    if not text or not isinstance(text, str):
        return ""
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
    if not content or not isinstance(content, str):
        return None
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
    if layer.get("auth_type") == "vertex_adc":
        api_key = _resolve_vertex_token()
        if not api_key:
            log.debug("Layer %s skipped: no Vertex AI ADC token", layer["name"])
            return None
    elif layer["env_key"]:
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
    max_tokens = layer.get("max_tokens", 256)
    extra_payload = layer.get("extra_payload", {})

    for model in models_to_try:
        # Check deadline before each model attempt
        if deadline and time.time() >= deadline:
            log.debug("Layer %s deadline reached, skipping model %s", layer["name"], model)
            return None

        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": max_tokens,
            **extra_payload,
        }

        for attempt in range(MAX_RETRIES_PER_LAYER):
            # Check deadline before each retry attempt
            if deadline and time.time() >= deadline:
                log.debug("Layer %s deadline reached during retries", layer["name"])
                return None
            # Use remaining deadline as request timeout to prevent a single slow
            # request from blowing the overall deadline
            req_timeout = REQUEST_TIMEOUT
            if deadline:
                remaining = deadline - time.time()
                if remaining < req_timeout:
                    req_timeout = max(1, int(remaining))
            try:
                resp = requests.post(
                    chat_url, headers=headers, json=payload,
                    timeout=req_timeout,
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
                msg = choices[0].get("message", {})
                content = (
                    msg.get("content")
                    or msg.get("reasoning_content")
                    or msg.get("reasoning")
                    or ""
                )
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
        "You are a disciplined Kalshi 15-minute binary crypto market trading analyst. "
        "The strategy being evaluated is a GATED FAVOURITE: take liquidity on the favourite side "
        "(YES or NO at 75-99c) only when spot momentum and the fair-value model confirm positive EV "
        "after Kalshi's taker fee (0.07 x price x (1-price)). The position is held to settlement. "
        "Grade the SPECIFIC candidate signal: analyze its entry price, skew, trend, underlying spot momentum, "
        "and time left in the 15-minute window. Evaluate whether the favourite's momentum is genuine or an "
        "exhaustion/mean-reversion trap that will fail before expiry. "
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
    evals = []
    quorum = getattr(config, "LLM_CONSENSUS_QUORUM", 2)

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
                evals.append(result)
                log.info(
                    "LLM eval [%s/%s]: prob=%.2f conf=%.2f",
                    result["layer"], result.get("model", "?"),
                    result["probability_of_net_profit"],
                    result["confidence_hold_to_expiry"],
                )
                if len(evals) >= quorum:
                    break
            else:
                _record_layer_failure(layer_name)
        except Exception as e:
            last_error = e
            _record_layer_failure(layer_name)
            log.debug("Layer %s failed: %s", layer_name, e)
            continue

    if evals:
        avg_prob = sum(e["probability_of_net_profit"] for e in evals) / len(evals)
        avg_conf = sum(e["confidence_hold_to_expiry"] for e in evals) / len(evals)
        layers_used = "+".join(e["layer"] for e in evals)
        models_used = "+".join(e.get("model", "?") for e in evals)
        reasonings = [f"[{e['layer']}]: {e.get('reasoning', '')}" for e in evals if e.get("reasoning")]
        log.info(
            "LLM consensus (%d models: %s): avg_prob=%.2f avg_conf=%.2f",
            len(evals), layers_used, avg_prob, avg_conf,
        )
        return {
            "probability_of_net_profit": round(avg_prob, 4),
            "confidence_hold_to_expiry": round(avg_conf, 4),
            "reasoning": " | ".join(reasonings) if reasonings else "",
            "layer": f"consensus({layers_used})" if len(evals) > 1 else evals[0]["layer"],
            "model": models_used if len(evals) > 1 else evals[0].get("model", "?"),
            "evals_count": len(evals),
        }

    log.warning("LLM eval failed across all layers — last error: %s", last_error)
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