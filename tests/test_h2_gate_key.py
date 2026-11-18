"""H2 — gate key separated from the HMAC key; verify before marking the nonce.

Three properties:
  1. The X-Superbot-Key value is never the HMAC signing key (explicit
     SUPERBOT_GATE_KEY overrides; otherwise a derived, distinct value).
  2. The client refuses plaintext http:// to a non-loopback host.
  3. A request with a bad signature does not consume its nonce, and a flood
     of junk cannot evict a legitimate nonce or make marking quadratic.
"""

import time

import pytest

import config
import n8n_client
import n8n_webhook_auth
from n8n_webhook_auth import NonceStore, sign_body, verify_request


def test_gate_key_differs_from_hmac_secret(monkeypatch):
    monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "shared-secret")
    monkeypatch.delenv("SUPERBOT_GATE_KEY", raising=False)
    monkeypatch.setenv("SUPERBOT_GATE_KEY_FILE", "/nonexistent/gate_key")
    key = n8n_webhook_auth.gate_key()
    assert key and key != "shared-secret"
    assert n8n_webhook_auth.static_key() == key  # back-compat alias


def test_explicit_gate_key_wins(monkeypatch):
    monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "shared-secret")
    monkeypatch.setenv("SUPERBOT_GATE_KEY", "rotatable-gate-key")
    assert n8n_webhook_auth.gate_key() == "rotatable-gate-key"


def test_client_refuses_plaintext_non_loopback():
    with pytest.raises(ValueError, match="plaintext"):
        n8n_client.N8nClient(base_url="http://n8n.example.com:5678", api_key="x")


def test_client_allows_loopback_and_https(monkeypatch):
    assert n8n_client.N8nClient(
        base_url="http://127.0.0.1:5678", api_key="x").base_url
    assert n8n_client.N8nClient(
        base_url="http://localhost:5678", api_key="x").base_url
    assert n8n_client.N8nClient(
        base_url="https://n8n.example.com", api_key="x").base_url


def test_bad_signature_does_not_consume_nonce(monkeypatch):
    monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "s")
    ts = str(int(time.time() * 1000))
    store = NonceStore()
    ok, reason = verify_request(ts, "ab12", '{"a":1}', "deadbeef", store=store)
    assert ok is False and "signature" in reason
    good = sign_body(ts, "ab12", '{"a":1}')
    assert verify_request(ts, "ab12", '{"a":1}', good, store=store)[0] is True


def test_junk_requests_do_not_evict_legit_nonce(monkeypatch):
    """10,000 unsigned junk requests never reach the store (signature first)."""
    monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "s")
    ts = str(int(time.time() * 1000))
    store = NonceStore()
    legit = sign_body(ts, "c0ffee", '{"a":1}')
    assert verify_request(ts, "c0ffee", '{"a":1}', legit, store=store)[0] is True
    for i in range(10_000):
        assert verify_request(ts, f"{i:08x}", '{"a":1}', "deadbeef",
                              store=store)[0] is False
    assert len(store._seen) == 1
    # The legitimate nonce is still remembered (replay).
    assert verify_request(ts, "c0ffee", '{"a":1}', legit, store=store)[0] is False


def test_nonce_store_cap_prune_is_bounded():
    store = NonceStore(max_entries=4096)
    t0 = time.monotonic()
    for i in range(10_000):
        store.check_and_mark(f"{i:08x}", now_ms=1_000_000)
    assert time.monotonic() - t0 < 2.0  # O(1) prune, no per-insert sort
    assert len(store._seen) <= 4096
