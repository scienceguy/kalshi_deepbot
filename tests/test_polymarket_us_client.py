"""
Tests for polymarket_us_client.py — read-only Polymarket US client.

All tests mock the polymarket_us SDK; no real network or auth is required.
"""

import json
import os
import tempfile
from pathlib import Path

import pytest
from unittest.mock import MagicMock

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


def test_cancel_order_auto_resolves_slug(monkeypatch):
    """cancel_order() auto-resolves marketSlug from open orders when slug is omitted."""
    monkeypatch.setenv("POLYMARKET_KEY_ID", "dummy-key-id")
    monkeypatch.setenv("POLYMARKET_SECRET_KEY", "dummy-secret-key")

    mock_client = MagicMock()
    mock_client.orders.list.return_value = {
        "orders": [{"id": "ORD-123", "marketSlug": "test-slug-vwh"}]
    }
    monkeypatch.setattr(polymarket_us_client, "_get_authenticated_client", lambda: mock_client)

    # Cancel without market_slug
    ok = polymarket_us_client.cancel_order("ORD-123")
    assert ok is True
    mock_client.orders.cancel.assert_called_once_with("ORD-123", {"marketSlug": "test-slug-vwh"})


def test_place_maker_order_preview_and_create(monkeypatch):
    """place_maker_order() previews then creates with participateDontInitiate."""
    monkeypatch.setenv("POLYMARKET_KEY_ID", "dummy-key-id")
    monkeypatch.setenv("POLYMARKET_SECRET_KEY", "dummy-secret-key")

    mock_client = MagicMock()
    mock_client.orders.preview.return_value = {"order": {"state": "ORDER_STATE_PENDING_NEW"}}
    mock_client.orders.create.return_value = {"id": "ORD-999"}
    monkeypatch.setattr(polymarket_us_client, "_get_authenticated_client", lambda: mock_client)

    resp = polymarket_us_client.place_maker_order("test-slug", "yes", 0.05, 10)
    assert resp == {"id": "ORD-999"}
    assert mock_client.orders.preview.called
    assert mock_client.orders.create.called


def test_health_check(monkeypatch):
    """health_check returns True on success and False on error."""
    mock_client = MagicMock()
    mock_client.get.return_value = {"markets": [{"slug": "m1"}]}
    monkeypatch.setattr(polymarket_us_client, "_get_client", lambda: mock_client)

    assert polymarket_us_client.health_check() is True

    mock_client.get.side_effect = Exception("Network error")
    assert polymarket_us_client.health_check() is False


def test_public_aliases_and_exceptions():
    """Verify public aliases and custom exception classes."""
    assert polymarket_us_client.get_client is polymarket_us_client._get_client
    assert polymarket_us_client.get_authenticated_client is polymarket_us_client._get_authenticated_client
    assert issubclass(polymarket_us_client.PolymarketAPIError, Exception)


# ---------------------------------------------------------------------------
# (g) NO-side price conversion (long-side price.value rule)
# ---------------------------------------------------------------------------

def test_poly_price_yes_is_literal():
    """YES side price passes straight through as price.value."""
    assert polymarket_us_client._poly_price("yes", 0.05) == 0.05
    assert polymarket_us_client._poly_price("yes", 0.95) == 0.95
    assert polymarket_us_client._poly_price("yes", 0.99) == 0.99


def test_poly_price_no_is_complement():
    """NO side price maps to 1 - price (long side's quote)."""
    assert polymarket_us_client._poly_price("no", 0.05) == 0.95   # buy NO @ 0.05 → YES quoted at 0.95
    assert polymarket_us_client._poly_price("no", 0.95) == 0.05   # buy NO @ 0.95 → YES quoted at 0.05
    assert polymarket_us_client._poly_price("no", 0.99) == 0.01   # buy NO @ 0.99 → YES quoted at 0.01


def test_place_maker_order_no_side_sends_complement(monkeypatch):
    """place_maker_order() sends 1-price for NO side (long-side price.value rule)."""
    monkeypatch.setenv("POLYMARKET_KEY_ID", "dummy-key-id")
    monkeypatch.setenv("POLYMARKET_SECRET_KEY", "dummy-secret-key")

    captured = {}

    def fake_preview(params):
        captured["price"] = params["request"]["price"]
        return {"order": {"state": "ORDER_STATE_PENDING_NEW"}}

    def fake_create(params):
        captured["create_price"] = params["price"]
        return {"id": "ORD-NO"}

    mock_client = MagicMock()
    mock_client.orders.preview.side_effect = fake_preview
    mock_client.orders.create.side_effect = fake_create
    monkeypatch.setattr(polymarket_us_client, "_get_authenticated_client", lambda: mock_client)

    resp = polymarket_us_client.place_maker_order("test-slug", "no", 0.99, 1)
    assert resp == {"id": "ORD-NO"}
    # price.value must be 1 - 0.99 = 0.01 (the YES side's quote for NO @ 0.99)
    assert captured["price"]["value"] == "0.01"
    assert captured["create_price"]["value"] == "0.01"


def test_preview_order_no_side_sends_complement(monkeypatch):
    """preview_order() sends 1-price for NO side (long-side price.value rule)."""
    monkeypatch.setenv("POLYMARKET_KEY_ID", "dummy-key-id")
    monkeypatch.setenv("POLYMARKET_SECRET_KEY", "dummy-secret-key")

    captured = {}

    def fake_preview(params):
        captured["price"] = params["request"]["price"]
        return {"order": {"state": "ORDER_STATE_PENDING_NEW"}}

    mock_client = MagicMock()
    mock_client.orders.preview.side_effect = fake_preview
    monkeypatch.setattr(polymarket_us_client, "_get_authenticated_client", lambda: mock_client)

    resp = polymarket_us_client.preview_order("test-slug", "no", 0.99, 1)
    assert resp == {"order": {"state": "ORDER_STATE_PENDING_NEW"}}
    assert captured["price"]["value"] == "0.01"


# ---------------------------------------------------------------------------
# (h) get_market_settlement retry behavior
# ---------------------------------------------------------------------------

def test_get_market_settlement_retries_on_error(monkeypatch):
    """get_market_settlement() retries on non-429 errors (e.g. 404 not-settled-yet)
    before returning None, instead of giving up on the first failure."""
    import time as _time

    monkeypatch.setattr(polymarket_us_client, "_enforce_rate_limit", lambda: None)
    monkeypatch.setattr(_time, "sleep", lambda s: None)

    class _SettlementError(Exception):
        pass

    mock_client = MagicMock()
    mock_client.markets.settlement.side_effect = _SettlementError("Settlement not found")
    monkeypatch.setattr(polymarket_us_client, "_get_client", lambda: mock_client)

    res = polymarket_us_client.get_market_settlement("test-slug")

    assert res is None
    # 3 attempts total (initial + 2 retries)
    assert mock_client.markets.settlement.call_count == 3


def test_get_market_settlement_returns_value_on_success(monkeypatch):
    """get_market_settlement() returns the settlement dict on first success."""
    monkeypatch.setattr(polymarket_us_client, "_enforce_rate_limit", lambda: None)

    mock_client = MagicMock()
    mock_client.markets.settlement.return_value = {"settlement": 1.0}
    monkeypatch.setattr(polymarket_us_client, "_get_client", lambda: mock_client)

    res = polymarket_us_client.get_market_settlement("test-slug")

    assert res == {"slug": "test-slug", "settlement": 1.0}
    assert mock_client.markets.settlement.call_count == 1