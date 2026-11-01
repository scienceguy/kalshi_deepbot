"""Tests for kalshi_price_data_client.py — Optional wrapper for kalshi-price-data."""

import pytest


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
