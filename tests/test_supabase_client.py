"""Tests for supabase_client.py — Supabase REST client.

Tests mock the HTTP requests to avoid making real network calls.
"""
import pytest
from unittest.mock import MagicMock, patch
from supabase_client import SupabaseClient, get_supabase
import config


class TestSupabaseClientInit:
    """Tests for SupabaseClient initialization."""

    def test_init_stores_url_and_key(self, monkeypatch):
        """Client stores url and key from env vars."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_secret_key")
        monkeypatch.delenv("SUPABASE_KEY_FILE", raising=False)
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
        monkeypatch.delenv("SUPABASE_KEY_FILE", raising=False)
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()
        assert client.enabled is False


class TestSupabaseClientRequest:
    """Tests for SupabaseClient._request method."""

    def test_request_success(self, monkeypatch):
        """_request returns parsed JSON on 200 response."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        monkeypatch.delenv("SUPABASE_KEY_FILE", raising=False)
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"data": [{"id": 1}]}'
        mock_resp.json.return_value = {"data": [{"id": 1}]}

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client._request("GET", "signals?limit=1")
            assert result == {"data": [{"id": 1}]}

    def test_request_returns_none_on_non_2xx(self, monkeypatch):
        """_request returns None on non-2xx non-retryable error."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        monkeypatch.delenv("SUPABASE_KEY_FILE", raising=False)
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.content = b'Bad request'

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client._request("POST", "signals", data={"ts": "2024-01-01"})
            assert result is None


class TestSupabaseClientGetBotState:
    """Tests for SupabaseClient.get_bot_state method."""

    def test_get_bot_state_returns_value(self, monkeypatch):
        """get_bot_state returns parsed value."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        monkeypatch.delenv("SUPABASE_KEY_FILE", raising=False)
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()

        with patch.object(client, "_request", return_value=[{"value_json": "42.5"}]):
            result = client.get_bot_state("session_pnl")
            assert result == 42.5

    def test_get_bot_state_returns_none_when_empty(self, monkeypatch):
        """get_bot_state returns None when no rows found."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        monkeypatch.delenv("SUPABASE_KEY_FILE", raising=False)
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()

        with patch.object(client, "_request", return_value=[]):
            result = client.get_bot_state("session_pnl")
            assert result is None