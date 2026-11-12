"""
Tests for polymarket_us_client.py — read-only Polymarket US client.

All tests mock the polymarket_us SDK; no real network or auth is required.
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

import polymarket_us_client
from polymarket_us import PolymarketUS


# ---------------------------------------------------------------------------
# Helpers: temporary BW mirror files
# ---------------------------------------------------------------------------


def _write_mirror_key(key_file: Path, value: str) -> None:
    """Write a mock BW mirror key file with a 'value' field."""
    data = {"name": "polymarket_api_key", "value": value}
    key_file.write_text(json.dumps(data, indent=2))


def _write_mirror_secret(secret_file: Path, value: str) -> None:
    """Write a mock BW mirror secret file with a 'value' field."""
    data = {"name": "polymarket_secret_key", "value": value}
    secret_file.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module():
    """Reset state between tests — each test reads fresh env vars."""
    # No persistent module-level client to reset in this design.
    yield


# ---------------------------------------------------------------------------
# (a) creds load from env when set
# ---------------------------------------------------------------------------


def test_creds_from_env(monkeypatch):
    """POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY from env are used."""
    key_id = "test-key-id-36-char-long-xyz1234"
    secret_key = "test-secret-key-88-char-base64-xyz1234567890ab"

    monkeypatch.setenv("POLYMARKET_KEY_ID", key_id)
    monkeypatch.setenv("POLYMARKET_SECRET_KEY", secret_key)

    # The module reads these dynamically via os.environ.get,
    # so they should reflect the monkeypatched values.
    assert polymarket_us_client._load_key_id() == key_id
    assert polymarket_us_client._load_secret_key() == secret_key

    client = polymarket_us_client._get_client()
    assert client is not None


# ---------------------------------------------------------------------------
# (b) creds load from bw mirror when env empty
# ---------------------------------------------------------------------------


def test_creds_from_bw_mirror(monkeypatch, tmp_path):
    """When env vars are empty, creds are loaded from BW mirror files."""
    # Create temporary mirror files with fake but valid-looking values
    key_file = tmp_path / "polymarket_api_key.json"
    secret_file = tmp_path / "polymarket_secret_key.json"

    # 36-char UUID-like key + 88-char base64-like secret
    fake_key = "a" * 36
    fake_secret = "b" * 88

    _write_mirror_key(key_file, fake_key)
    _write_mirror_secret(secret_file, fake_secret)

    # Temporarily redirect BW_DIR to our temp directory
    monkeypatch.setenv("BW_DIR", str(tmp_path))

    # Clear any previously set env vars to be clean
    monkeypatch.delenv("POLYMARKET_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_SECRET_KEY", raising=False)

    # Env vars empty → should fall back to mirror
    assert polymarket_us_client._load_key_id() == fake_key
    assert polymarket_us_client._load_secret_key() == fake_secret

    client = polymarket_us_client._get_client()
    assert client is not None
    # Verify the SDK was initialized with the correct keys
    assert client.key_id == fake_key
    assert client.secret_key == fake_secret


# ---------------------------------------------------------------------------
# (c) portfolio/balances/market calls invoke SDK with authenticated flag
# ---------------------------------------------------------------------------


def test_portfolio_positions_call(monkeypatch):
    """get_portfolio_positions() calls SDK get() with authenticated=True."""
    key_id = "test-key-id-36-char-long-xyz1234"
    secret_key = "test-secret-key-88-char-base64-xyz1234567890ab"
    monkeypatch.setenv("POLYMARKET_KEY_ID", key_id)
    monkeypatch.setenv("POLYMARKET_SECRET_KEY", secret_key)

    # Monkeypatch PolymarketUS.get to verify the authenticated flag
    original_get = PolymarketUS.get
    call_kwargs = {}

    def mock_get(self, path, *, query=None, authenticated=False):
        call_kwargs["authenticated"] = authenticated
        call_kwargs["path"] = path
        # Return a minimal dict to avoid network calls
        return {"markets": [], "cursor": None}

    PolymarketUS.get = mock_get

    try:
        positions = polymarket_us_client.get_portfolio_positions()
        assert call_kwargs["authenticated"] is True, (
            f"Expected authenticated=True, got {call_kwargs['authenticated']}"
        )
        assert call_kwargs["path"] == "/v1/portfolio/positions"
    finally:
        PolymarketUS.get = original_get


def test_account_balances_call(monkeypatch):
    """get_account_balances() calls SDK get() with authenticated=True."""
    key_id = "test-key-id-36-char-long-xyz1234"
    secret_key = "test-secret-key-88-char-base64-xyz1234567890ab"
    monkeypatch.setenv("POLYMARKET_KEY_ID", key_id)
    monkeypatch.setenv("POLYMARKET_SECRET_KEY", secret_key)

    original_get = PolymarketUS.get
    call_kwargs = {}

    def mock_get(self, path, *, query=None, authenticated=False):
        call_kwargs["authenticated"] = authenticated
        call_kwargs["path"] = path
        return {"balances": [], "total": 0.0}

    PolymarketUS.get = mock_get

    try:
        balances = polymarket_us_client.get_account_balances()
        assert call_kwargs["authenticated"] is True, (
            f"Expected authenticated=True, got {call_kwargs['authenticated']}"
        )
        assert call_kwargs["path"] == "/v1/account/balances"
    finally:
        PolymarketUS.get = original_get


def test_market_by_slug_call(monkeypatch):
    """get_market_by_slug() calls SDK get() with authenticated=False."""
    key_id = "test-key-id-36-char-long-xyz1234"
    secret_key = "test-secret-key-88-char-base64-xyz1234567890ab"
    monkeypatch.setenv("POLYMARKET_KEY_ID", key_id)
    monkeypatch.setenv("POLYMARKET_SECRET_KEY", secret_key)

    original_get = PolymarketUS.get
    call_kwargs = {}

    def mock_get(self, path, *, query=None, authenticated=False):
        call_kwargs["authenticated"] = authenticated
        call_kwargs["path"] = path
        return {"market": {"slug": path.split("/")[-1], "active": True}}

    PolymarketUS.get = mock_get

    try:
        result = polymarket_us_client.get_market_by_slug("test-market")
        assert call_kwargs["authenticated"] is False, (
            f"Expected authenticated=False, got {call_kwargs['authenticated']}"
        )
        assert call_kwargs["path"] == "/v1/market/slug/test-market"
        assert result["market"]["slug"] == "test-market"
    finally:
        PolymarketUS.get = original_get


def test_market_book_call(monkeypatch):
    """get_market_book() calls SDK get() with authenticated=False."""
    key_id = "test-key-id-36-char-long-xyz1234"
    secret_key = "test-secret-key-88-char-base64-xyz1234567890ab"
    monkeypatch.setenv("POLYMARKET_KEY_ID", key_id)
    monkeypatch.setenv("POLYMARKET_SECRET_KEY", secret_key)

    original_get = PolymarketUS.get
    call_kwargs = {}

    def mock_get(self, path, *, query=None, authenticated=False):
        call_kwargs["authenticated"] = authenticated
        call_kwargs["path"] = path
        # Return BBO-like structure
        return {"bbo": {"yes_bid": "0.85", "yes_ask": "0.87", "no_bid": "0.13", "no_ask": "0.15"}}

    PolymarketUS.get = mock_get

    try:
        result = polymarket_us_client.get_market_book("test-market")
        assert call_kwargs["authenticated"] is False, (
            f"Expected authenticated=False, got {call_kwargs['authenticated']}"
        )
        assert "bbo" in result
    finally:
        PolymarketUS.get = original_get


# ---------------------------------------------------------------------------
# (d) missing creds raise on authenticated call but public market call still works
# ---------------------------------------------------------------------------


def test_missing_creds_raise_on_authenticated(monkeypatch, tmp_path):
    """RuntimeError raised on authenticated call when no creds are configured."""
    # Ensure no env vars and no BW mirror
    monkeypatch.delenv("POLYMARKET_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_SECRET_KEY", raising=False)

    # Temporarily redirect BW_DIR away so mirror isn't found
    monkeypatch.setenv("BW_DIR", str(tmp_path / "nonexistent"))

    # Public market call should still work (no auth needed)
    # Mock the SDK get for unauthenticated calls
    original_get = PolymarketUS.get
    call_kwargs = {}

    def mock_get(self, path, *, query=None, authenticated=False):
        call_kwargs["authenticated"] = authenticated
        # Return data for public call
        if "book" in path or "bbo" in path:
            return {"bbo": {"yes_bid": "0.85", "yes_ask": "0.87"}}
        if "market" in path:
            return {"market": {"slug": "test", "active": True}}
        return {}

    PolymarketUS.get = mock_get

    try:
        # (d1) public market call works without creds
        market = polymarket_us_client.get_market_by_slug("test-market")
        assert call_kwargs["authenticated"] is False

        # (d2) authenticated call raises RuntimeError
        try:
            polymarket_us_client.get_portfolio_positions()
            assert False, "Expected RuntimeError for missing creds"
        except RuntimeError as e:
            assert "not configured" in str(e).lower() or "missing creds" in str(e).lower()

    finally:
        PolymarketUS.get = original_get


def test_public_market_call_without_creds(monkeypatch, tmp_path):
    """get_market_by_slug() works without any creds configured."""
    # Ensure no env vars and no BW mirror
    monkeypatch.delenv("POLYMARKET_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_SECRET_KEY", raising=False)
    monkeypatch.setenv("BW_DIR", str(tmp_path / "nonexistent"))

    # Mock the SDK
    original_get = PolymarketUS.get
    call_args = {}

    def mock_get(self, path, *, query=None, authenticated=False):
        call_args["authenticated"] = authenticated
        call_args["path"] = path
        if "book" in path or "bbo" in path:
            return {"bbo": {"yes_bid": "0.85", "yes_ask": "0.87"}}
        if "market" in path:
            return {"market": {"slug": "test", "active": True}}
        return {}

    PolymarketUS.get = mock_get

    try:
        # Public call should succeed
        market = polymarket_us_client.get_market_by_slug("test-market")
        book = polymarket_us_client.get_market_book("test-market")
        assert call_args["authenticated"] is False
        assert "bbo" in book
    finally:
        PolymarketUS.get = original_get