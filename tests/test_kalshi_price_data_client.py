"""Tests for kalshi_price_data_client.py — Optional wrapper for kalshi-price-data."""

import logging
import sys
import types
from unittest.mock import MagicMock

import pytest


def _fake_sdk_module(client_factory=None):
    """Build a fake ``kalshi_price_data`` module for sys.modules injection."""
    mod = types.ModuleType("kalshi_price_data")

    class _FakeSDKClient:
        def __init__(self, api_key=None):
            self.api_key = api_key

    mod.Client = client_factory or _FakeSDKClient
    return mod


class TestIsAvailable:
    """Tests for is_available function."""

    def test_is_available_returns_bool(self):
        """is_available returns a boolean."""
        from legacy.kalshi_price_data_client import is_available
        result = is_available()
        assert isinstance(result, bool)


class TestKalshiPriceDataClientInit:
    """Tests for KalshiPriceDataClient initialization."""

    def test_client_created(self):
        """Client can be instantiated even without SDK."""
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        assert isinstance(client, KalshiPriceDataClient)
        assert isinstance(client.available, bool)


class TestGetClient:
    """Tests for get_client factory function."""

    def test_get_client_returns_client(self):
        """get_client returns a KalshiPriceDataClient instance."""
        from legacy.kalshi_price_data_client import get_client, KalshiPriceDataClient
        client = get_client()
        assert isinstance(client, KalshiPriceDataClient)


class TestHealthCheck:
    """Tests for health_check method."""

    def test_health_check_returns_bool(self):
        """health_check returns a boolean."""
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        result = client.health_check()
        assert isinstance(result, bool)


class TestGetHistoricalSnapshots:
    """Tests for get_historical_snapshots method."""

    def test_get_historical_snapshots_returns_list(self, monkeypatch):
        """get_historical_snapshots returns a list of snapshot dicts."""
        from legacy.kalshi_price_data_client import KalshiPriceDataClient

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
        from legacy.kalshi_price_data_client import KalshiPriceDataClient

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
        from legacy.kalshi_price_data_client import KalshiPriceDataClient

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
        from legacy.kalshi_price_data_client import KalshiPriceDataClient

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


class TestTryImport:
    """Tests for _try_import / SDK presence detection."""

    def test_import_succeeds_with_fake_sdk(self, monkeypatch):
        """When the SDK import works, the client is marked available."""
        monkeypatch.setitem(sys.modules, "kalshi_price_data", _fake_sdk_module())
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient(api_key="k")
        assert client.available is True
        assert client._client is not None
        assert client._client.api_key == "k"

    def test_import_fails_when_sdk_missing(self, monkeypatch):
        """sys.modules entry of None forces ImportError -> available False."""
        monkeypatch.setitem(sys.modules, "kalshi_price_data", None)
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        assert client.available is False
        assert client._client is None

    def test_import_fails_when_sdk_client_constructor_raises_import_error(self, monkeypatch):
        """An ImportError from Client() itself is treated as 'SDK unavailable'."""
        def _boom(api_key=None):
            raise ImportError("broken install")

        monkeypatch.setitem(sys.modules, "kalshi_price_data",
                            _fake_sdk_module(client_factory=_boom))
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        assert client.available is False

    def test_api_key_defaults_to_none(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        assert client.api_key is None


class TestIsAvailableFunction:
    """is_available() must reflect the SDK import outcome."""

    def test_is_available_true_with_fake_sdk(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "kalshi_price_data", _fake_sdk_module())
        from legacy.kalshi_price_data_client import is_available
        assert is_available() is True

    def test_is_available_false_without_sdk(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "kalshi_price_data", None)
        from legacy.kalshi_price_data_client import is_available
        assert is_available() is False


class TestGetClientFactory:
    """get_client() is a process-wide lazy singleton."""

    def test_get_client_passes_api_key_on_first_call(self, monkeypatch):
        import legacy.kalshi_price_data_client as mod
        monkeypatch.setattr(mod, "_client", None)
        client = mod.get_client(api_key="my-key")
        assert client.api_key == "my-key"

    def test_get_client_returns_same_instance(self, monkeypatch):
        import legacy.kalshi_price_data_client as mod
        monkeypatch.setattr(mod, "_client", None)
        first = mod.get_client()
        second = mod.get_client()
        assert first is second

    def test_get_client_ignores_api_key_after_creation(self, monkeypatch):
        import legacy.kalshi_price_data_client as mod
        monkeypatch.setattr(mod, "_client", None)
        first = mod.get_client(api_key="first")
        second = mod.get_client(api_key="second")
        assert second is first
        assert second.api_key == "first"


class TestHealthCheckDeep:
    """health_check() across every branch of the implementation."""

    def test_health_check_false_when_sdk_missing(self, monkeypatch):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = False
        assert client.health_check() is False

    def test_health_check_true_when_sdk_health_check_succeeds(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.health_check.return_value = True
        client._client = mock_client
        assert client.health_check() is True
        mock_client.health_check.assert_called_once_with()

    def test_health_check_false_when_sdk_lacks_health_check(self, caplog):
        """An SDK without health_check logs a warning and returns False."""
        from legacy.kalshi_price_data_client import KalshiPriceDataClient

        class _NoHealthCheck:
            pass

        client = KalshiPriceDataClient()
        client._available = True
        client._client = _NoHealthCheck()
        with caplog.at_level(logging.WARNING, logger="kalshi_price_data_client"):
            assert client.health_check() is False
        assert "no health_check method" in caplog.text

    def test_health_check_false_when_sdk_health_check_raises(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.health_check.side_effect = RuntimeError("api down")
        client._client = mock_client
        assert client.health_check() is False

    def test_health_check_false_when_sdk_health_check_raises_value_error(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.health_check.side_effect = ValueError("bad config")
        client._client = mock_client
        assert client.health_check() is False


class TestGetHistoricalSnapshotsDeep:
    """Error paths, argument passing and edge cases."""

    def test_raises_import_error_when_sdk_missing(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = False
        with pytest.raises(ImportError, match="pip install kalshi-price-data"):
            client.get_historical_snapshots(
                series_ticker="KXBTC15M",
                start_time="2026-01-01T00:00:00Z",
                end_time="2026-01-02T00:00:00Z",
            )

    def test_passes_all_arguments_to_sdk(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.get_historical_snapshots.return_value = []
        client._client = mock_client
        result = client.get_historical_snapshots(
            series_ticker="KXBTC15M",
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-02T00:00:00Z",
            market_ticker="KXBTC15M-26NOV011145-45",
        )
        assert result == []
        mock_client.get_historical_snapshots.assert_called_once_with(
            series_ticker="KXBTC15M",
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-02T00:00:00Z",
            market_ticker="KXBTC15M-26NOV011145-45",
        )

    def test_market_ticker_defaults_to_none(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.get_historical_snapshots.return_value = []
        client._client = mock_client
        client.get_historical_snapshots(
            series_ticker="KXBTC15M",
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-02T00:00:00Z",
        )
        assert mock_client.get_historical_snapshots.call_args.kwargs["market_ticker"] is None

    def test_sdk_exception_propagates(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.get_historical_snapshots.side_effect = RuntimeError("sdk exploded")
        client._client = mock_client
        with pytest.raises(RuntimeError, match="sdk exploded"):
            client.get_historical_snapshots(
                series_ticker="KXBTC15M",
                start_time="2026-01-01T00:00:00Z",
                end_time="2026-01-02T00:00:00Z",
            )

    def test_non_list_none_raises_value_error(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.get_historical_snapshots.return_value = None
        client._client = mock_client
        with pytest.raises(ValueError, match="NoneType"):
            client.get_historical_snapshots(
                series_ticker="KXBTC15M",
                start_time="2026-01-01T00:00:00Z",
                end_time="2026-01-02T00:00:00Z",
            )

    def test_non_list_tuple_raises_value_error(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.get_historical_snapshots.return_value = ({"a": 1},)
        client._client = mock_client
        with pytest.raises(ValueError, match="tuple"):
            client.get_historical_snapshots(
                series_ticker="KXBTC15M",
                start_time="2026-01-01T00:00:00Z",
                end_time="2026-01-02T00:00:00Z",
            )

    def test_non_list_string_raises_value_error_with_type_name(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.get_historical_snapshots.return_value = "oops"
        client._client = mock_client
        with pytest.raises(ValueError, match="str"):
            client.get_historical_snapshots(
                series_ticker="KXBTC15M",
                start_time="2026-01-01T00:00:00Z",
                end_time="2026-01-02T00:00:00Z",
            )

    def test_empty_list_is_valid(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.get_historical_snapshots.return_value = []
        client._client = mock_client
        assert client.get_historical_snapshots(
            series_ticker="KXBTC15M",
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-02T00:00:00Z",
        ) == []


class TestGetMarketSnapshotsDeep:
    """Error paths, argument passing and edge cases."""

    def test_raises_import_error_when_sdk_missing(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = False
        with pytest.raises(ImportError, match="pip install kalshi-price-data"):
            client.get_market_snapshots(
                market_ticker="KXBTC15M-26NOV011145-45",
                start_time="2026-01-01T00:00:00Z",
                end_time="2026-01-02T00:00:00Z",
            )

    def test_passes_all_arguments_to_sdk(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.get_market_snapshots.return_value = []
        client._client = mock_client
        client.get_market_snapshots(
            market_ticker="KXBTC15M-26NOV011145-45",
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-02T00:00:00Z",
        )
        mock_client.get_market_snapshots.assert_called_once_with(
            market_ticker="KXBTC15M-26NOV011145-45",
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-02T00:00:00Z",
        )

    def test_sdk_exception_propagates(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.get_market_snapshots.side_effect = ConnectionError("network down")
        client._client = mock_client
        with pytest.raises(ConnectionError, match="network down"):
            client.get_market_snapshots(
                market_ticker="KXBTC15M-26NOV011145-45",
                start_time="2026-01-01T00:00:00Z",
                end_time="2026-01-02T00:00:00Z",
            )

    def test_non_list_none_raises_value_error(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.get_market_snapshots.return_value = None
        client._client = mock_client
        with pytest.raises(ValueError, match="NoneType"):
            client.get_market_snapshots(
                market_ticker="KXBTC15M-26NOV011145-45",
                start_time="2026-01-01T00:00:00Z",
                end_time="2026-01-02T00:00:00Z",
            )

    def test_non_list_dict_raises_value_error(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.get_market_snapshots.return_value = {"error": "not found"}
        client._client = mock_client
        with pytest.raises(ValueError, match="dict"):
            client.get_market_snapshots(
                market_ticker="KXBTC15M-26NOV011145-45",
                start_time="2026-01-01T00:00:00Z",
                end_time="2026-01-02T00:00:00Z",
            )

    def test_non_list_int_raises_value_error(self):
        from legacy.kalshi_price_data_client import KalshiPriceDataClient
        client = KalshiPriceDataClient()
        client._available = True
        mock_client = MagicMock()
        mock_client.get_market_snapshots.return_value = 42
        client._client = mock_client
        with pytest.raises(ValueError, match="int"):
            client.get_market_snapshots(
                market_ticker="KXBTC15M-26NOV011145-45",
                start_time="2026-01-01T00:00:00Z",
                end_time="2026-01-02T00:00:00Z",
            )
