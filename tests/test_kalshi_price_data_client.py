"""Tests for kalshi_price_data_client.py — Optional wrapper for kalshi-price-data."""

import pytest
from unittest.mock import MagicMock, patch


class TestIsAvailable:
    """Tests for is_available function."""

    def test_is_available_returns_bool(self):
        """is_available returns a boolean."""
        from kalshi_price_data_client import is_available
        result = is_available()
        assert isinstance(result, bool)


class TestKalshiPriceDataClientInit:
    """Tests for KalshiPriceDataClient initialization."""

    def test_client_created(self):
        """Client can be instantiated even without SDK."""
        from kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        assert isinstance(client, KalshiPriceDataClient)
        assert isinstance(client.available, bool)


class TestGetClient:
    """Tests for get_client factory function."""

    def test_get_client_returns_client(self):
        """get_client returns a KalshiPriceDataClient instance."""
        from kalshi_price_data_client import get_client, KalshiPriceDataClient
        client = get_client()
        assert isinstance(client, KalshiPriceDataClient)


class TestHealthCheck:
    """Tests for health_check method."""

    def test_health_check_returns_bool(self):
        """health_check returns a boolean."""
        from kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        result = client.health_check()
        assert isinstance(result, bool)


class TestGetHistoricalSnapshots:
    """Tests for get_historical_snapshots method."""

    def test_get_historical_snapshots_returns_list(self, monkeypatch):
        """get_historical_snapshots returns a list of snapshot dicts."""
        from kalshi_price_data_client import KalshiPriceDataClient

        client = KalshiPriceDataClient()
        client._available = True

        mock_client = MagicMock()
        mock_client.get_historical_snapshots.return_value = [
            {"series_ticker": "KXBTC15M", "start_time": "2026-01-01T00:00:00Z"},
            {"series_ticker": "KXBTC15M", "start_time": "2026-01-02T00:00:00Z"},
        ]
        client._client = mock_client

        result = client.get_historical_snapshots(
            series_ticker="KXBTC15M",
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-02T00:00:00Z",
        )
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["series_ticker"] == "KXBTC15M"

    def test_get_historical_snapshots_returns_list_not_dict(self, monkeypatch):
        """get_historical_snapshots raises ValueError if SDK returns non-list."""
        from kalshi_price_data_client import KalshiPriceDataClient

        client = KalshiPriceDataClient()
        client._available = True

        mock_client = MagicMock()
        mock_client.get_historical_snapshots.return_value = {"error": "not found"}
        client._client = mock_client

        try:
            client.get_historical_snapshots(
                series_ticker="KXBTC15M",
                start_time="2026-01-01T00:00:00Z",
                end_time="2026-01-02T00:00:00Z",
            )
            assert False, "Expected ValueError for non-list return"
        except ValueError:
            pass


class TestGetMarketSnapshots:
    """Tests for get_market_snapshots method."""

    def test_get_market_snapshots_returns_list(self, monkeypatch):
        """get_market_snapshots returns a list of snapshot dicts."""
        from kalshi_price_data_client import KalshiPriceDataClient

        client = KalshiPriceDataClient()
        client._available = True

        mock_client = MagicMock()
        mock_client.get_market_snapshots.return_value = [
            {"market_ticker": "KXBTC15M-26NOV011145-45"},
            {"market_ticker": "KXBTC15M-26NOV011146-45"},
        ]
        client._client = mock_client

        result = client.get_market_snapshots(
            market_ticker="KXBTC15M-26NOV011145-45",
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-02T00:00:00Z",
        )
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["market_ticker"] == "KXBTC15M-26NOV011145-45"

    def test_get_market_snapshots_returns_list_not_dict(self, monkeypatch):
        """get_market_snapshots raises ValueError if SDK returns non-list."""
        from kalshi_price_data_client import KalshiPriceDataClient

        client = KalshiPriceDataClient()
        client._available = True

        mock_client = MagicMock()
        mock_client.get_market_snapshots.return_value = {"error": "not found"}
        client._client = mock_client

        try:
            client.get_market_snapshots(
                market_ticker="KXBTC15M-26NOV011145-45",
                start_time="2026-01-01T00:00:00Z",
                end_time="2026-01-02T00:00:00Z",
            )
            assert False, "Expected ValueError for non-list return"
        except ValueError:
            pass
