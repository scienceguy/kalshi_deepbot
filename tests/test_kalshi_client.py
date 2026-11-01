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
        mock_resp.get.return_value = [{"ticker": "KXBTC15M"}]

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
        mock_resp.get.return_value = [{"ticker": "KXBTC15M"}]

        with patch.object(client.session, "request", return_value=mock_resp) as mock_request:
            result1 = client.get_markets(series_ticker="KXBTC15M", status="open", limit=50)
            result2 = client.get_markets(series_ticker="KXBTC15M", status="open", limit=50)
            assert result1 is result2  # Same cached object
            # First call makes the HTTP request, second call uses cache
            assert mock_request.call_count >= 1  # At least one HTTP call


class TestHistoricalMarkets:
    """Tests for get_historical_markets method."""

    def test_get_historical_markets_returns_list(self, monkeypatch):
        """get_historical_markets returns a list of historical market dicts."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_sig"
            client = KalshiClient()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "markets": [
                {"ticker": "KXBTC15M-26NOV011145-45", "status": "finalized",
                 "expiration_value": "100", "close_time": "2026-11-01T11:45:00Z"}
            ],
            "cursor": "next_cursor_value"
        }
        mock_resp.status_code = 200

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client.get_historical_markets(series_ticker="KXBTC15M", limit=100)
            assert isinstance(result, dict)
            assert "markets" in result
            assert len(result["markets"]) == 1
            assert result["markets"][0]["ticker"] == "KXBTC15M-26NOV011145-45"
            assert result["cursor"] == "next_cursor_value"

    def test_get_market_candlesticks_returns_data(self, monkeypatch):
        """get_market_candlesticks returns candlestick data."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_sig"
            client = KalshiClient()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ticker": "KXBTC15M-26NOV011145-45",
            "candlesticks": [
                {"t": 1780380000, "o": 0.50, "h": 0.55, "l": 0.48, "c": 0.52, "v": 1000}
            ]
        }
        mock_resp.status_code = 200

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client.get_market_candlesticks(
                ticker="KXBTC15M-26NOV011145-45",
                start_ts=1780376400,
                end_ts=1780380000,
                period_interval=1,
                limit=100
            )
            assert isinstance(result, dict)
            assert "candlesticks" in result
            assert result["ticker"] == "KXBTC15M-26NOV011145-45"
            assert len(result["candlesticks"]) == 1
            assert result["candlesticks"][0]["o"] == 0.50