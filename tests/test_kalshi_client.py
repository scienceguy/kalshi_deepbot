"""Tests for kalshi_client.py — Kalshi REST client authentication and request handling.

These tests mock the Kalshi API responses to avoid making real network calls.
"""
import pytest
from unittest.mock import MagicMock, patch
from kalshi_client import KalshiClient, KalshiAPIError
import config


def _make_mock_private_key():
    """Create a mock private key whose .sign() returns bytes."""
    key = MagicMock()
    key.sign.return_value = b"fake_signature_bytes"
    return key


class TestKalshiClientInit:
    """Tests for KalshiClient initialization."""

    def test_init_sets_attributes(self, monkeypatch):
        """Client stores key_id, base_url, and creates session."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "KALSHI_BASE_URL", "https://external-api.kalshi.com/trade-api/v2")
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            client = KalshiClient()
            assert client.key_id == "test_key_id"
            assert client.base_url == "https://external-api.kalshi.com/trade-api/v2"
            assert client._verified is False  # Not verified until first request


class TestAuthHeader:
    """Tests for auth header generation."""

    def test_auth_header_has_required_fields(self, monkeypatch):
        """Auth header contains key_id, timestamp, and signature."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_sig"
            client = KalshiClient()

        headers = client._auth_header("GET", "/markets")
        assert "KALSHI-ACCESS-KEY" in headers
        assert "KALSHI-ACCESS-TIMESTAMP" in headers
        assert "KALSHI-ACCESS-SIGNATURE" in headers
        assert headers["KALSHI-ACCESS-KEY"] == "test_key_id"
        assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()


class TestGetMarkets:
    """Tests for get_markets method."""

    def test_get_markets_returns_list(self, monkeypatch):
        """get_markets returns a list of market dicts."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_sig"
            client = KalshiClient()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"markets": [{"ticker": "KXBTC15M"}]}
        mock_resp.status_code = 200

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client.get_markets()
            assert isinstance(result, list)
            assert len(result) == 1
            assert result[0]["ticker"] == "KXBTC15M"

    def test_get_markets_caches_results(self, monkeypatch):
        """get_markets caches results per (series_ticker, status, limit)."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_sig"
            client = KalshiClient()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"markets": [{"ticker": "KXBTC15M"}]}
        mock_resp.status_code = 200

        with patch.object(client.session, "request", return_value=mock_resp) as mock_request:
            client.get_markets(series_ticker="KXBTC15M", status="open", limit=50)
            client.get_markets(series_ticker="KXBTC15M", status="open", limit=50)
            # Second call with same params should use cache
            assert mock_request.call_count == 1

            # Different params should trigger new request
            client.get_markets(series_ticker="KXETH15M", status="open", limit=50)
            assert mock_request.call_count == 2


class TestPlaceOrder:
    """Tests for place_order method."""

    def test_place_order_valid_input(self, monkeypatch):
        """place_order validates price_cents range and builds correct payload."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "DRY_RUN", True)
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_sig"
            client = KalshiClient()

        result = client.place_order("KXBTC15M", "yes", 10, 50, "test_coid", "limit")
        assert result["dry_run"] is True
        assert result["order_id"] == "test_coid"

    def test_place_order_invalid_price(self, monkeypatch):
        """place_order rejects price_cents outside 1-99 range."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_sig"
            client = KalshiClient()

        result = client.place_order("KXBTC15M", "yes", 10, 0, "test_coid")
        assert result is None
        result = client.place_order("KXBTC15M", "yes", 10, 100, "test_coid")
        assert result is None

    def test_place_order_invalid_contracts(self, monkeypatch):
        """place_order rejects contracts <= 0."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_sig"
            client = KalshiClient()

        result = client.place_order("KXBTC15M", "yes", 0, 50, "test_coid")
        assert result is None
        result = client.place_order("KXBTC15M", "yes", -1, 50, "test_coid")
        assert result is None


class TestGetMarketsCaching:
    """Tests for market caching behavior."""

    def test_get_markets_cache_key(self, monkeypatch):
        """Cache key includes series_ticker, status, and limit."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_sig"
            client = KalshiClient()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"markets": []}
        mock_resp.status_code = 200

        with patch.object(client.session, "request", return_value=mock_resp) as mock_request:
            client.get_markets(series_ticker="KXBTC15M", status="open", limit=50)
            client.get_markets(series_ticker="KXBTC15M", status="open", limit=50)
            # Second call with same params should use cache
            assert mock_request.call_count == 1

            # Different params should trigger new request
            client.get_markets(series_ticker="KXETH15M", status="open", limit=50)
            assert mock_request.call_count == 2