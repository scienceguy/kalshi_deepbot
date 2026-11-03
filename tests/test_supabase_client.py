"""Tests for supabase_client.py — Supabase REST client.

Tests mock the HTTP requests to avoid making real network calls.
"""
import time

import pytest
from unittest.mock import MagicMock, patch
from supabase_client import SupabaseClient, get_supabase
import supabase_client
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

class TestSupabaseClientSetBotStateBatch:
    """Tests for SupabaseClient.set_bot_state_batch method.

    Posts all keys in a single batch upsert (one POST with array of rows).
    """

    def test_batch_all_keys_succeed(self, monkeypatch):
        """set_bot_state_batch posts all keys in one batch and returns True on success."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        monkeypatch.delenv("SUPABASE_KEY_FILE", raising=False)
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()

        calls = []

        def fake_request(method, path, data=None, params=None, extra_headers=None, max_retries=3):
            calls.append((method, path, data))
            return {"data": [{"key": row["key"]} for row in data]}

        with patch.object(client, "_request", side_effect=fake_request):
            result = client.set_bot_state_batch({"session_pnl": 42.5, "cycles": 3})
            assert result is True
            assert len(calls) == 1
            assert calls[0][0] == "POST"
            assert calls[0][1] == "bot_state"
            # Single batch with both keys
            assert len(calls[0][2]) == 2
            keys = {row["key"] for row in calls[0][2]}
            assert keys == {"session_pnl", "cycles"}

    def test_batch_stops_on_first_failure(self, monkeypatch):
        """set_bot_state_batch returns False when batch fails (no partial writes)."""
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        monkeypatch.delenv("SUPABASE_KEY_FILE", raising=False)
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()

        calls = []

        def fake_request(method, path, data=None, params=None, extra_headers=None, max_retries=3):
            calls.append((method, path, data))
            # Simulate batch failure (e.g., constraint violation)
            return None

        with patch.object(client, "_request", side_effect=fake_request):
            result = client.set_bot_state_batch({"good_key": 1.0, "bad_key": 2.0, "never_posted": 3.0})
            assert result is False
            assert len(calls) == 1  # single batch attempt
            # All keys in the single batch
            assert len(calls[0][2]) == 3
            keys = {row["key"] for row in calls[0][2]}
            assert keys == {"good_key", "bad_key", "never_posted"}


class TestCircuitBreakerReset:
    """The circuit breaker must reset its counter when the cooldown expires.

    Previously _supabase_circuit_open() reset _SUPABASE_FAILURES without a
    `global` declaration, so the reset hit a local. The module counter stayed at
    the threshold and one later failure immediately re-opened the circuit,
    permanently suppressing audit writes after the first outage.
    """

    def test_counter_reset_after_cooldown(self, monkeypatch):
        monkeypatch.setattr(supabase_client, "_SUPABASE_FAILURES", 5)
        monkeypatch.setattr(supabase_client, "_SUPABASE_SKIP_UNTIL", time.time() - 1)
        assert supabase_client._supabase_circuit_open() is False
        assert supabase_client._SUPABASE_FAILURES == 0
        # one subsequent failure must NOT reopen the circuit
        supabase_client._supabase_record_failure()
        assert supabase_client._supabase_circuit_open() is False
