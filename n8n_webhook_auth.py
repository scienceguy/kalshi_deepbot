#!/usr/bin/env python3
"""n8n_webhook_auth.py — shared HMAC envelope for SuperBot webhooks (N1).

Both sides of every webhook call use this module:

- Sender (``n8n_client.py``): signs each request with
  ``HMAC-SHA256(secret, "{timestamp_ms}.{nonce}.{body}")`` and sends the
  static shared secret in ``X-Superbot-Key`` (the n8n webhook headerAuth
  credential is the possession gate).
- Receiver (n8n workflow Code node, or the test receiver stub): verifies
  freshness (60-second window), replay (nonce store) and authenticity
  (constant-time signature comparison).

Body canonicalization: the sender signs
``json.dumps(data, separators=(",", ":"), ensure_ascii=False)`` — byte-equal
to JavaScript ``JSON.stringify`` for all content except U+2028/U+2029, which
JSON.stringify escapes. Receivers verifying a re-parsed body should try both
that form and the raw body string.

Headers:
    X-Superbot-Key        static shared secret (possession gate)
    X-Superbot-Timestamp  milliseconds since epoch
    X-Superbot-Nonce      random hex, unique per request
    X-Superbot-Signature  hex HMAC-SHA256 of "{timestamp}.{nonce}.{body}"

The secret comes from ``SUPERBOT_WEBHOOK_SECRET`` (env / config), falling
back to ``~/.secrets/superbot_webhook_secret`` (mode 0600). It is never
logged and never written anywhere by this module.
"""

import hashlib
import hmac
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger("n8n_webhook_auth")

# Validity window for a signed request (spec: 60 seconds).
SIGNATURE_WINDOW_MS = 60_000

# Replay cache lifetime: a nonce may be remembered for at least the window.
_NONCE_TTL_MS = 2 * SIGNATURE_WINDOW_MS


def _secret_bytes() -> bytes:
    """Resolve the webhook secret from config/env, then ~/.secrets.

    Raises RuntimeError when nothing is configured. Never logs the secret.
    """
    secret = getattr(config, "SUPERBOT_WEBHOOK_SECRET", "") or os.environ.get(
        "SUPERBOT_WEBHOOK_SECRET", "")
    if not secret:
        secret_file = Path(
            os.environ.get(
                "SUPERBOT_WEBHOOK_SECRET_FILE",
                os.path.expanduser("~/.secrets/superbot_webhook_secret"),
            )
        )
        try:
            mode = secret_file.stat().st_mode & 0o777
            if mode & 0o077:
                log.warning("%s has insecure permissions (mode %o); refusing it",
                            secret_file, mode)
            else:
                secret = secret_file.read_text().strip()
        except OSError:
            secret = ""
    if not secret:
        raise RuntimeError(
            "SUPERBOT_WEBHOOK_SECRET is not set and ~/.secrets/superbot_webhook_secret "
            "is unreadable — refusing to sign/verify unsigned traffic")
    return secret.encode()


def sign_body(timestamp_ms, nonce: str, body: str) -> str:
    """HMAC-SHA256 over ``{timestamp_ms}.{nonce}.{body}`` (hex)."""
    message = f"{int(timestamp_ms)}.{nonce}.{body}".encode()
    return hmac.new(_secret_bytes(), message, hashlib.sha256).hexdigest()


def gate_key() -> str:
    """H2: value for the X-Superbot-Key header — never the HMAC key.

    The gate key travels in plaintext on every request and is stored in n8n's
    header-auth credential, so it must not be the key that signs bodies.
    Resolution order: ``SUPERBOT_GATE_KEY`` env / ``SUPERBOT_GATE_KEY_FILE``,
    else a value derived from the webhook secret (compatible with existing
    installs, but then it cannot be rotated independently — set the explicit
    key to get that).

    Returns "" when unconfigured (the receiver rejects it); never raises,
    never logs the value.
    """
    explicit = os.environ.get("SUPERBOT_GATE_KEY") or getattr(
        config, "SUPERBOT_GATE_KEY", "")
    if not explicit:
        gate_file = Path(os.environ.get(
            "SUPERBOT_GATE_KEY_FILE",
            os.path.expanduser("~/.secrets/superbot_gate_key")))
        try:
            mode = gate_file.stat().st_mode & 0o777
            if mode & 0o077:
                log.warning("%s has insecure permissions (mode %o); refusing it",
                            gate_file, mode)
            else:
                explicit = gate_file.read_text().strip()
        except OSError:
            explicit = ""
    if explicit:
        return explicit
    try:
        secret = _secret_bytes()
    except RuntimeError:
        return ""
    # Derived value: distinct from the HMAC key, so a leaked header cannot
    # forge a signature. Not independently rotatable — set SUPERBOT_GATE_KEY.
    return hmac.new(secret, b"superbot-gate-key-v1", hashlib.sha256).hexdigest()


def static_key() -> str:
    """Back-compat alias for :func:`gate_key` (the header value)."""
    return gate_key()


class NonceStore:
    """Bounded set of seen nonces with TTL (replay protection).

    H2: insertion-ordered so TTL pruning and cap eviction are O(1) per entry
    instead of re-sorting the whole store on every insert (a flood of junk
    made every mark quadratic).
    """

    def __init__(self, ttl_ms: int = _NONCE_TTL_MS, max_entries: int = 4096):
        self._ttl_ms = ttl_ms
        self._max = max_entries
        self._seen: "OrderedDict[str, int]" = OrderedDict()

    def check_and_mark(self, nonce: str, now_ms: Optional[int] = None) -> bool:
        """True when the nonce is fresh; False if replayed or malformed."""
        if not nonce or len(nonce) > 256 or any(c not in "0123456789abcdefABCDEF" for c in nonce):
            return False
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        # Expire entries past the TTL before checking, so an old nonce may be
        # reused once any signature window that could still carry it has shut.
        cutoff = now_ms - self._ttl_ms
        while self._seen:
            oldest_ts = next(iter(self._seen.values()))
            if oldest_ts >= cutoff:
                break
            self._seen.popitem(last=False)
        if nonce in self._seen:
            return False
        self._seen[nonce] = now_ms
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return True


def verify_request(timestamp_ms, nonce: str, body: str, signature: str,
                   *, now_ms: Optional[int] = None,
                   store: Optional[NonceStore] = None):
    """Receiver-side verification. Returns (ok: bool, reason: str).

    Checks, in order: presence of signed fields, integer timestamp,
    freshness window (±60s), signature, then nonce freshness (if a store is
    supplied). Never raises. H2: the signature is checked before the nonce is
    marked, so unsigned junk cannot fill (and evict) the replay store.
    """
    if timestamp_ms is None or not body or not nonce or not signature:
        return False, "missing signed fields"
    try:
        ts = int(timestamp_ms)
    except (TypeError, ValueError):
        return False, "bad timestamp"
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    if abs(now_ms - ts) > SIGNATURE_WINDOW_MS:
        return False, f"timestamp outside {SIGNATURE_WINDOW_MS}ms window"
    try:
        expected = sign_body(ts, nonce, body)
    except RuntimeError:
        return False, "no webhook secret configured"
    if not hmac.compare_digest(expected, signature):
        return False, "signature mismatch"
    if store is not None and not store.check_and_mark(nonce, now_ms):
        return False, "replayed or malformed nonce"
    return True, "ok"
