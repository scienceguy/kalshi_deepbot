"""Tests for supabase_client.py — Supabase REST client.

Tests mock the HTTP requests to avoid making real network calls.
"""
import json
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

    Each key is delegated to set_bot_state, which PATCHes by key and only
    INSERTs (with an explicit id) for a key that does not exist yet.
    """

    def _make_client(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        monkeypatch.delenv("SUPABASE_KEY_FILE", raising=False)
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        client = supabase_client.SupabaseClient()
        return client

    def test_batch_existing_keys_are_patched(self, monkeypatch):
        """Existing keys are PATCHed, one request per key, no INSERT."""
        client = self._make_client(monkeypatch)
        calls = []

        def fake_request(method, path, data=None, params=None,
                         extra_headers=None, max_retries=3):
            calls.append((method, path, data))
            if method == "PATCH":
                return [{"key": path.rsplit("eq.", 1)[1]}]  # row matched
            return {}

        with patch.object(client, "_request", side_effect=fake_request):
            result = client.set_bot_state_batch({"session_pnl": 42.5, "cycles": 3})

        assert result is True
        assert [c[0] for c in calls] == ["PATCH", "PATCH"]
        assert {c[1] for c in calls} == {"bot_state?key=eq.session_pnl",
                                         "bot_state?key=eq.cycles"}

    def test_batch_new_key_inserts_with_explicit_id(self, monkeypatch):
        """A key that PATCH misses is INSERTed with id = max(id)+1.

        New keys cannot rely on the id default (DEFAULT 1) — that collides on
        the PK, which is exactly what raised the 409 and disabled the client.
        """
        client = self._make_client(monkeypatch)
        calls = []

        def fake_request(method, path, data=None, params=None,
                         extra_headers=None, max_retries=3):
            calls.append((method, path, data))
            if method == "PATCH":
                return []                                   # no row matched
            if method == "GET":
                return [{"id": 102}]                        # current max
            return [{"id": 103, "key": data["key"]}]        # INSERT

        with patch.object(client, "_request", side_effect=fake_request):
            result = client.set_bot_state_batch({"brand_new": 1.0})

        assert result is True
        assert [c[0] for c in calls] == ["PATCH", "GET", "POST"]
        post = calls[-1]
        assert post[2]["id"] == 103
        assert post[2]["key"] == "brand_new"
        assert json.loads(post[2]["value_json"]) == 1.0

    def test_batch_reports_failure_but_tries_every_key(self, monkeypatch):
        """set_bot_state_batch returns False when a key fails, after trying all."""
        client = self._make_client(monkeypatch)
        calls = []

        def fake_request(method, path, data=None, params=None,
                         extra_headers=None, max_retries=3):
            calls.append((method, path, data))
            if method == "PATCH":
                return [] if "bad_key" in path else [{"key": path}]
            if method == "GET":
                return [{"id": 0}]
            return None if "bad_key" in data.get("key", "") else [{"id": 1}]

        with patch.object(client, "_request", side_effect=fake_request):
            result = client.set_bot_state_batch(
                {"good_key": 1.0, "bad_key": 2.0, "never_posted": 3.0})

        assert result is False
        # PATCH carries the key in the path; POST carries it in the body.
        attempted = {c[1].rsplit("eq.", 1)[-1] if c[0] == "PATCH" else c[2]["key"]
                     for c in calls if c[0] in ("PATCH", "POST")}
        assert attempted == {"good_key", "bad_key", "never_posted"}


class TestSupabaseClientSetBotState:
    """Tests for SupabaseClient.set_bot_state single-key upsert.

    Contract: PATCH bot_state?key=eq.<key> when the key exists; otherwise
    SELECT max(id) and INSERT with id = max(id)+1. It never issues the plain
    POST that collided on bot_state_pkey and disabled the client.
    """

    def _make_client(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test_key")
        monkeypatch.delenv("SUPABASE_KEY_FILE", raising=False)
        import importlib
        import supabase_client
        importlib.reload(supabase_client)
        return supabase_client.SupabaseClient()

    def test_existing_key_is_patched(self, monkeypatch):
        """A key that exists is updated with a single PATCH, no INSERT."""
        client = self._make_client(monkeypatch)
        calls = []

        def fake_request(method, path, data=None, params=None,
                         extra_headers=None, max_retries=3):
            calls.append((method, path, data))
            return [{"id": 100, "key": "session_pnl"}]  # row matched

        with patch.object(client, "_request", side_effect=fake_request):
            result = client.set_bot_state("session_pnl", 42.5)

        assert result is True
        assert len(calls) == 1
        method, path, data = calls[0]
        assert method == "PATCH"
        assert path == "bot_state?key=eq.session_pnl"
        assert json.loads(data["value_json"]) == 42.5
        assert "ts" in data
        assert "id" not in data        # never mints an id on the update path

    def test_new_key_inserts_with_explicit_id(self, monkeypatch):
        """A missing key is INSERTed with id = max(id)+1, not the default 1."""
        client = self._make_client(monkeypatch)
        calls = []

        def fake_request(method, path, data=None, params=None,
                         extra_headers=None, max_retries=3):
            calls.append((method, path, data))
            if method == "PATCH":
                return []
            if method == "GET":
                return [{"id": 102}]
            return [{"id": 103, "key": "new_key"}]

        with patch.object(client, "_request", side_effect=fake_request):
            result = client.set_bot_state("new_key", 7)

        assert result is True
        assert [c[0] for c in calls] == ["PATCH", "GET", "POST"]
        assert calls[1][1] == "bot_state?select=id&order=id.desc&limit=1"
        assert calls[2][2]["id"] == 103
        assert calls[2][2]["key"] == "new_key"

    def test_repeated_write_is_idempotent(self, monkeypatch):
        """Writing the same key twice PATCHes twice — no duplicate row."""
        client = self._make_client(monkeypatch)
        calls = []

        def fake_request(method, path, data=None, params=None,
                         extra_headers=None, max_retries=3):
            calls.append((method, path, data))
            return [{"id": 100}]

        with patch.object(client, "_request", side_effect=fake_request):
            assert client.set_bot_state("session_pnl", 100.0) is True
            assert client.set_bot_state("session_pnl", 200.0) is True

        assert [c[0] for c in calls] == ["PATCH", "PATCH"]
        assert json.loads(calls[0][2]["value_json"]) == 100.0
        assert json.loads(calls[1][2]["value_json"]) == 200.0

    def test_returns_false_when_insert_fails(self, monkeypatch):
        """set_bot_state returns False when the INSERT returns nothing."""
        client = self._make_client(monkeypatch)

        def fake_request(method, path, data=None, params=None,
                         extra_headers=None, max_retries=3):
            return [] if method in ("PATCH", "GET") else None

        with patch.object(client, "_request", side_effect=fake_request):
            assert client.set_bot_state("bad_key", 1.0) is False


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


def test_parse_raw_key_file_kv_shape(tmp_path):
    """KEY=VALUE protected files yield only the recognised key's value."""
    from supabase_client import _parse_raw_key_file
    f = tmp_path / "supabase_key"
    f.write_text("SUPABASE_SERVICE_KEY=sb_secret_example123\n")
    f.chmod(0o600)
    assert _parse_raw_key_file(str(f)) == "sb_secret_example123"


def test_parse_raw_key_file_bare_key(tmp_path):
    """A bare single-line key is returned as-is."""
    from supabase_client import _parse_raw_key_file
    f = tmp_path / "supabase_key"
    f.write_text("sb_secret_barekey456\n")
    f.chmod(0o600)
    assert _parse_raw_key_file(str(f)) == "sb_secret_barekey456"


def test_parse_raw_key_file_kv_without_known_key_refuses(tmp_path):
    from supabase_client import _parse_raw_key_file
    f = tmp_path / "supabase_key"
    f.write_text("PREFERENCE_TOKEN=xyz\n")
    f.chmod(0o600)
    assert _parse_raw_key_file(str(f)) == ""
