"""Tests for supabase_client.py — Supabase REST client.

These tests mock the HTTP requests to avoid making real network calls.
"""
import pytest
from unittest.mock import MagicMock, patch
from supabase_client import SupabaseClient, get_supabase, is_enabled
import config


class TestSupabaseClientInit:
    """Tests for SupabaseClient initialization."""

    def test_init_stores_url_and_key(self, monkeypatch):
        """Client stores url and key from environment variables."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_secret_key")
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()
        assert client.url == "https://test.supabase.co"
        assert client.key == "test_secret_key"
        assert client.enabled is True

    def test_init_disabled_when_no_credentials(self, monkeypatch):
        """Client is disabled when no credentials available."""
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()
        assert client.enabled is False
        assert client.url == ""
        assert client.key == ""


class TestSupabaseClientRequest:
    """Tests for SupabaseClient._request method."""

    def test_request_success(self, monkeypatch):
        """_request returns parsed JSON on 200 response."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"data": [{"id": 1}]}'

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client._request("GET", "signals?limit=1")
            assert result == {"data": [{"id": 1}]}

    def test_request_returns_none_on_non_2xx(self, monkeypatch):
        """_request returns None on non-2xx non-retryable error."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad request"

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client._request("POST", "signals", data={"ts": "2024-01-01"})
            assert result is None


class TestSupabaseClientInsertSignal:
    """Tests for SupabaseClient.insert_signal method."""

    def test_insert_signal_success(self, monkeypatch):
        """insert_signal returns True on successful insert."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'[{"id": 1}]'

        with patch.object(client, "_request", return_value=mock_resp):
            result = client.insert_signal({
                "ts": "2024-01-01T00:00:00Z",
                "ticker": "KXBTC15M",
                "side": "yes",
                "price_cents": 50,
                "contracts": 10,
            })
            assert result is True

    def test_insert_signal_missing_fields(self, monkeypatch):
        """insert_signal returns False when required fields missing."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()

        result = client.insert_signal({"ts": "2024-01-01T00:00:00Z"})
        assert result is False


class TestSupabaseClientSetBotState:
    """Tests for SupabaseClient.set_bot_state method."""

    def test_set_bot_state_success(self, monkeypatch):
        """set_bot_state returns True on successful upsert."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'[{"id": 1}]'

        with patch.object(client, "_request", return_value=mock_resp):
            result = client.set_bot_state("session_pnl", 42.5)
            assert result is True

    def test_set_bot_state_failure(self, monkeypatch):
        """set_bot_state returns False when update fails."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()

        with patch.object(client, "_request", return_value=None):
            result = client.set_bot_state("session_pnl", 42.5)
            assert result is False


class TestSupabaseClientGetBotState:
    """Tests for SupabaseClient.get_bot_state method."""

    def test_get_bot_state_returns_value(self, monkeypatch):
        """get_bot_state returns parsed value."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'[{"value_json": "42.5"}]'

        with patch.object(client, "_request", return_value=mock_resp):
            result = client.get_bot_state("session_pnl")
            assert result == 42.5

    def test_get_bot_state_returns_none_when_empty(self, monkeypatch):
        """get_bot_state returns None when no rows found."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"[]"

        with patch.object(client, "_request", return_value=mock_resp):
            result = client.get_bot_state("session_pnl")
            assert result is None