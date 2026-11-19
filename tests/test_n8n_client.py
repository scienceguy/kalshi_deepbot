"""Tests for n8n_client.py — n8n REST API client.

These tests mock the n8n API responses to avoid making real network calls.
"""
import json
import logging
import time

import pytest
import requests
from unittest.mock import MagicMock, patch

import config
import n8n_client
import n8n_webhook_auth
import rate_limit
from n8n_client import (
    CRITICAL_WEBHOOK_TIMEOUT,
    NORMAL_WEBHOOK_TIMEOUT,
    TELEMETRY_WEBHOOK_TIMEOUT,
    N8nClient,
    DEFAULT_BASE_URL,
    _CircuitBreaker,
    _deliver_outbox_row,
    _drain_telemetry,
    _enqueue_telemetry,
    _lane_timeout,
    _read_api_key_file,
    _sign_body,
    _start_outbox_sender,
    _write_outbox_sidecar,
    _execute_command_enabled,
    drain_outbox,
    get_client,
    load_api_key,
    n8n_startup_check,
    outbox_metrics,
)


@pytest.fixture(autouse=True)
def _reset_outbox_state(monkeypatch):
    """Isolate module-level outbox/breaker/telemetry/singleton state per test."""
    monkeypatch.setattr(n8n_client, "_client", None)
    # Prevent fire_and_forget tests from spawning real sender threads.
    monkeypatch.setattr(n8n_client, "_outbox_sender_started", True)
    breaker = n8n_client._outbox_breaker
    old_breaker = (breaker._consecutive_failures, breaker._opened_at)
    breaker._consecutive_failures, breaker._opened_at = 0, 0.0
    old_dropped = n8n_client._telemetry_dropped
    n8n_client._telemetry_pending.clear()
    n8n_client._telemetry_dropped = 0
    yield
    breaker._consecutive_failures, breaker._opened_at = old_breaker
    n8n_client._telemetry_pending.clear()
    n8n_client._telemetry_dropped = old_dropped


def _ok_resp(payload=None):
    """A 200 MagicMock response carrying a JSON payload."""
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"{}" if payload is None else json.dumps(payload).encode()
    resp.json.return_value = {} if payload is None else payload
    return resp


def _err_resp(status, message="error"):
    resp = MagicMock()
    resp.status_code = status
    err = requests.HTTPError(message)
    err.response = resp
    resp.raise_for_status.side_effect = err
    return resp


class TestLoadApiKey:
    """Tests for API key loading."""

    def test_load_from_env(self, monkeypatch):
        """API key is loaded from N8N_API_KEY environment variable."""
        monkeypatch.setenv("N8N_API_KEY", "test_env_key")
        assert load_api_key() == "test_env_key"

    def test_load_from_file(self, monkeypatch, tmp_path):
        """API key is loaded from n8n_api_key.txt file."""
        monkeypatch.delenv("N8N_API_KEY", raising=False)
        key_file = tmp_path / "n8n_api_key.txt"
        key_file.write_text("file_key_value")
        # The loader refuses key files readable by group/other (a deliberate
        # security check), so the fixture must be owner-only.
        key_file.chmod(0o600)
        from pathlib import Path as _RealPath
        monkeypatch.setattr(
            "n8n_client.Path",
            lambda p: key_file if "n8n_api_key" in str(p) else _RealPath(p))
        assert load_api_key() == "file_key_value"


class TestN8nClientInit:
    """Tests for N8nClient initialization."""

    def test_init_stores_api_key(self, monkeypatch):
        """Client stores the provided API key."""
        client = N8nClient(api_key="test_key_123")
        assert client.api_key == "test_key_123"
        assert client.base_url == DEFAULT_BASE_URL

    def test_init_without_api_key_logs_warning(self, monkeypatch, caplog):
        """Client logs warning when no API key is found."""
        monkeypatch.delenv("N8N_API_KEY", raising=False)
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch("n8n_client.Path", return_value=mock_path):
            client = N8nClient()
            assert client.api_key is None
            assert "No n8n API key found" in caplog.text


class TestExecuteWebhook:
    """Tests for execute_webhook method."""

    def test_execute_webhook_success(self, monkeypatch):
        """execute_webhook returns parsed JSON response (sync mode)."""
        client = N8nClient(api_key="test_key")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok"}
        mock_resp.status_code = 200
        mock_resp.content = b'{"status": "ok"}'

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client.execute_webhook("superbot-trade", {"action": "settlement"}, fire_and_forget=False)
            assert result == {"status": "ok"}

    def test_execute_webhook_raises_on_error(self, monkeypatch):
        """execute_webhook raises exception on non-2xx response (sync mode)."""
        client = N8nClient(api_key="test_key")
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = Exception("Server error")

        with patch.object(client.session, "request", return_value=mock_resp):
            with pytest.raises(Exception):
                client.execute_webhook("superbot-trade", {"action": "settlement"}, fire_and_forget=False)

    def test_execute_webhook_empty_response(self, monkeypatch):
        """execute_webhook returns empty dict when response has no content (sync mode)."""
        client = N8nClient(api_key="test_key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b""

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client.execute_webhook("superbot-trade", {"action": "settlement"}, fire_and_forget=False)
            assert result == {}

    def test_execute_webhook_retries_on_5xx(self, monkeypatch):
        """execute_webhook retries on 5xx and returns the successful response (sync mode)."""
        import requests
        client = N8nClient(api_key="test_key")
        fail_resp = MagicMock()
        fail_resp.status_code = 500
        http_error = requests.HTTPError("Server error")
        http_error.response = fail_resp
        fail_resp.raise_for_status.side_effect = http_error
        ok_resp = MagicMock()
        ok_resp.json.return_value = {"status": "ok"}
        ok_resp.status_code = 200
        ok_resp.content = b'{"status": "ok"}'

        with patch.object(client.session, "request", side_effect=[fail_resp, ok_resp]) as mock_req:
            result = client.execute_webhook("superbot-trade", {"action": "settlement"}, fire_and_forget=False)
            assert result == {"status": "ok"}
            assert mock_req.call_count == 2

    def test_execute_webhook_no_retry_on_4xx(self, monkeypatch):
        """execute_webhook does not retry on 4xx client errors (sync mode)."""
        import requests
        client = N8nClient(api_key="test_key")
        bad_resp = MagicMock()
        bad_resp.status_code = 400
        http_error = requests.HTTPError("Bad request")
        http_error.response = bad_resp
        bad_resp.raise_for_status.side_effect = http_error

        with patch.object(client.session, "request", return_value=bad_resp) as mock_req:
            with pytest.raises(Exception):
                client.execute_webhook("superbot-trade", {"action": "settlement"}, fire_and_forget=False)
            assert mock_req.call_count == 1

    def test_execute_webhook_fire_and_forget_returns_immediately(self, monkeypatch):
        """fire_and_forget=True returns {} immediately; the durable outbox holds the row."""
        client = N8nClient(api_key="test_key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"status": "ok"}'

        class _FakeDB:
            def __init__(self):
                self.rows = []

            def insert_outbox_event(self, **kwargs):
                self.rows.append(kwargs)
                return 1

        fake_db = _FakeDB()
        monkeypatch.setattr("local_db.get_local_db", lambda: fake_db)
        with patch.object(client.session, "request", return_value=mock_resp) as mock_req:
            result = client.execute_webhook(
                "superbot-trade",
                {"action": "settlement", "event_type": "settlement"},
                fire_and_forget=True)
            assert result == {}
            # N2: no network I/O in the calling thread; the row is durable
            assert mock_req.call_count == 0
            assert fake_db.rows and fake_db.rows[0]["event_type"] == "settlement"


class TestRunBacktest:
    """Tests for run_backtest convenience method."""

    def test_run_backtest_calls_execute_webhook(self, monkeypatch):
        """run_backtest calls execute_webhook with superbot-trade path."""
        client = N8nClient(api_key="test_key")
        with patch.object(client, "execute_webhook", return_value={"status": "ok"}) as mock_exec:
            result = client.run_backtest({"test": "data"})
            mock_exec.assert_called_once_with("superbot-backtest", {"test": "data"})
            assert result == {"status": "ok"}

    def test_run_data_collect_calls_execute_webhook(self, monkeypatch):
        """run_data_collect calls execute_webhook with superbot-collect path."""
        client = N8nClient(api_key="test_key")
        with patch.object(client, "execute_webhook", return_value={"status": "ok"}) as mock_exec:
            result = client.run_data_collect({"series": "KXBTC15M"})
            mock_exec.assert_called_once_with("superbot-collect", {"series": "KXBTC15M"})
            assert result == {"status": "ok"}

    def test_run_trade_calls_execute_webhook(self, monkeypatch):
        """run_trade calls execute_webhook with superbot-trade path."""
        client = N8nClient(api_key="test_key")
        with patch.object(client, "execute_webhook", return_value={"status": "ok"}) as mock_exec:
            result = client.run_trade({"action": "settlement"})
            mock_exec.assert_called_once_with("superbot-trade", {"action": "settlement"})
            assert result == {"status": "ok"}

    def test_run_monitor_calls_execute_webhook(self, monkeypatch):
        """run_monitor calls execute_webhook with superbot-monitor path."""
        client = N8nClient(api_key="test_key")
        with patch.object(client, "execute_webhook", return_value={"status": "ok"}) as mock_exec:
            result = client.run_monitor({})
            mock_exec.assert_called_once_with("superbot-monitor", {})
            assert result == {"status": "ok"}


class TestN8nHealthCheck:
    """Tests for n8n_health_check function."""

    def test_n8n_health_check_returns_true_on_success(self, monkeypatch):
        """n8n_health_check returns True when webhook succeeds."""
        from n8n_client import n8n_health_check, get_client

        # Get the global client instance and mock its execute_webhook
        client = get_client()
        with patch.object(client, "execute_webhook", return_value={}):
            assert n8n_health_check() is True

    def test_n8n_health_check_returns_false_on_error(self, monkeypatch):
        """n8n_health_check returns False when webhook raises."""
        from n8n_client import n8n_health_check, get_client

        client = get_client()
        with patch.object(client, "execute_webhook", side_effect=Exception("Connection error")):
            assert n8n_health_check() is False

def test_load_api_key_parses_kv_file(monkeypatch, tmp_path):
    """A KEY=VALUE n8n access file must yield just the token, never the
    whole multi-line file (the raw file is not a valid HTTP header value)."""
    monkeypatch.delenv("N8N_API_KEY", raising=False)
    key_file = tmp_path / "n8n_access"
    key_file.write_text(
        "n8n_local_access_token=tok123\n"
        "n8n_url=http://127.0.0.1:5678\n"
        "login_note: never expose\n"
    )
    key_file.chmod(0o600)
    monkeypatch.setenv("N8N_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("N8N_PROGRAMMATIC_API_KEY_FILE", str(tmp_path / "missing"))
    assert load_api_key() == "tok123"


def test_load_api_key_kv_file_without_known_key_refuses(monkeypatch, tmp_path):
    monkeypatch.delenv("N8N_API_KEY", raising=False)
    key_file = tmp_path / "n8n_access"
    key_file.write_text("some_random_key=abc\nother=def\n")
    key_file.chmod(0o600)
    monkeypatch.setenv("N8N_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("N8N_PROGRAMMATIC_API_KEY_FILE", str(tmp_path / "missing"))
    assert load_api_key() is None


class TestCircuitBreaker:
    """_CircuitBreaker: consecutive-failure breaker for the outbox senders."""

    def test_initial_state_allows_requests(self):
        from n8n_client import _CircuitBreaker
        breaker = _CircuitBreaker(threshold=3, cooldown_s=60.0)
        assert breaker.allow() is True
        assert breaker.is_open is False

    def test_failures_below_threshold_keep_breaker_closed(self):
        from n8n_client import _CircuitBreaker
        breaker = _CircuitBreaker(threshold=3, cooldown_s=60.0)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.allow() is True
        assert breaker.is_open is False

    def test_breaker_opens_exactly_at_threshold(self):
        from n8n_client import _CircuitBreaker
        breaker = _CircuitBreaker(threshold=3, cooldown_s=60.0)
        for _ in range(2):
            breaker.record_failure()
        assert breaker.allow() is True
        breaker.record_failure()
        assert breaker.allow() is False
        assert breaker.is_open is True

    def test_breaker_stays_open_during_cooldown(self):
        import time
        from n8n_client import _CircuitBreaker
        breaker = _CircuitBreaker(threshold=2, cooldown_s=60.0)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.allow() is False
        # Even with time passing inside the cooldown window it stays open.
        breaker._opened_at = time.time() - 30.0
        assert breaker.allow() is False

    def test_breaker_allows_again_after_cooldown_elapses(self):
        import time
        from n8n_client import _CircuitBreaker
        breaker = _CircuitBreaker(threshold=2, cooldown_s=60.0)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.allow() is False
        breaker._opened_at = time.time() - 61.0
        assert breaker.allow() is True
        assert breaker.is_open is False

    def test_success_resets_failure_count(self):
        from n8n_client import _CircuitBreaker
        breaker = _CircuitBreaker(threshold=3, cooldown_s=60.0)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        breaker.record_failure()
        # Two failures after the reset must not trip a threshold of 3.
        assert breaker.allow() is True

    def test_success_closes_open_breaker(self):
        from n8n_client import _CircuitBreaker
        breaker = _CircuitBreaker(threshold=1, cooldown_s=60.0)
        breaker.record_failure()
        assert breaker.is_open is True
        breaker.record_success()
        assert breaker.allow() is True
        assert breaker.is_open is False

    def test_trip_event_emitted_once_per_open(self, monkeypatch):
        from n8n_client import _CircuitBreaker
        emitted = []
        monkeypatch.setattr("emit.emit_event",
                            lambda *a, **k: emitted.append(a))
        breaker = _CircuitBreaker(threshold=2, cooldown_s=600.0)
        breaker.record_failure()
        breaker.record_failure()  # opens -> trip
        breaker.record_failure()  # already open -> no second trip
        trips = [a for a in emitted if len(a) > 1 and a[1] == "breaker.trip"]
        assert len(trips) == 1
        assert trips[0][0] == "n8n_outbox"

    def test_success_after_open_emits_clear_event(self, monkeypatch):
        from n8n_client import _CircuitBreaker
        emitted = []
        monkeypatch.setattr("emit.emit_event",
                            lambda *a, **k: emitted.append(a))
        breaker = _CircuitBreaker(threshold=1, cooldown_s=60.0)
        breaker.record_failure()
        breaker.record_success()
        types = [a[1] for a in emitted if len(a) > 1]
        assert "breaker.trip" in types
        assert "breaker.clear" in types

    def test_success_without_prior_open_emits_nothing(self, monkeypatch):
        from n8n_client import _CircuitBreaker
        emitted = []
        monkeypatch.setattr("emit.emit_event",
                            lambda *a, **k: emitted.append(a))
        breaker = _CircuitBreaker(threshold=5, cooldown_s=60.0)
        breaker.record_failure()
        breaker.record_success()
        assert emitted == []

    def test_breaker_event_emit_failure_is_non_fatal(self, monkeypatch):
        from n8n_client import _CircuitBreaker

        def _boom(*a, **k):
            raise RuntimeError("emit down")

        monkeypatch.setattr("emit.emit_event", _boom)
        breaker = _CircuitBreaker(threshold=1, cooldown_s=60.0)
        breaker.record_failure()  # must not raise even if emit fails
        breaker.record_success()  # must not raise even if emit fails


class TestTransportSafety:
    """_assert_transport_safe: the H2 gate against plaintext off-loopback."""

    def test_http_loopback_allowed(self):
        from n8n_client import N8nClient
        client = N8nClient(base_url="http://127.0.0.1:5678", api_key="k")
        assert client.base_url == "http://127.0.0.1:5678"

    def test_http_localhost_allowed(self):
        from n8n_client import N8nClient
        client = N8nClient(base_url="http://localhost:5678", api_key="k")
        assert client.base_url == "http://localhost:5678"

    def test_http_non_loopback_refused(self):
        from n8n_client import N8nClient
        with pytest.raises(ValueError, match="refusing plaintext"):
            N8nClient(base_url="http://192.168.0.10:5678", api_key="k")

    def test_http_public_host_refused(self):
        from n8n_client import N8nClient
        with pytest.raises(ValueError, match="refusing plaintext"):
            N8nClient(base_url="http://n8n.example.com", api_key="k")

    def test_https_non_loopback_allowed_with_warning(self, caplog):
        from n8n_client import N8nClient
        with caplog.at_level("WARNING", logger="n8n_client"):
            client = N8nClient(base_url="https://n8n.example.com", api_key="k")
        assert client.base_url == "https://n8n.example.com"
        assert "not loopback" in caplog.text

    def test_trailing_slash_stripped(self):
        from n8n_client import N8nClient
        client = N8nClient(base_url="http://127.0.0.1:5678/", api_key="k")
        assert client.base_url == "http://127.0.0.1:5678"


class TestSessionIsolation:
    """The webhook session must never carry the API key (HMAC only)."""

    def test_session_carries_api_key_webhook_session_does_not(self):
        from n8n_client import N8nClient
        client = N8nClient(api_key="secret-key-123")
        assert client.session.headers.get("X-N8N-API-KEY") == "secret-key-123"
        assert "X-N8N-API-KEY" not in client._webhook_session.headers

    def test_webhook_session_is_distinct(self):
        from n8n_client import N8nClient
        client = N8nClient(api_key="k")
        assert client._webhook_session is not client.session


class TestReadApiKeyFile:
    """_read_api_key_file permission and content handling."""

    def test_insecure_permissions_refused(self, tmp_path):
        from n8n_client import _read_api_key_file
        key_file = tmp_path / "key"
        key_file.write_text("secret-token")
        key_file.chmod(0o644)
        assert _read_api_key_file(key_file) is None

    def test_empty_file_returns_none(self, tmp_path):
        from n8n_client import _read_api_key_file
        key_file = tmp_path / "key"
        key_file.write_text("   \n")
        key_file.chmod(0o600)
        assert _read_api_key_file(key_file) is None

    def test_plain_text_file_returns_token(self, tmp_path):
        from n8n_client import _read_api_key_file
        key_file = tmp_path / "key"
        key_file.write_text("plain-token\n")
        key_file.chmod(0o600)
        assert _read_api_key_file(key_file) == "plain-token"

    def test_kv_file_with_quoted_value_strips_quotes(self, tmp_path):
        from n8n_client import _read_api_key_file
        key_file = tmp_path / "key"
        key_file.write_text('n8n_local_access_token="quoted-token"\n')
        key_file.chmod(0o600)
        assert _read_api_key_file(key_file) == "quoted-token"

    def test_kv_file_without_known_key_refused(self, tmp_path):
        from n8n_client import _read_api_key_file
        key_file = tmp_path / "key"
        key_file.write_text("unrelated=thing\n")
        key_file.chmod(0o600)
        assert _read_api_key_file(key_file) is None

    def test_missing_file_returns_none(self, tmp_path):
        from n8n_client import _read_api_key_file
        assert _read_api_key_file(tmp_path / "does-not-exist") is None


class TestLoadApiKeyFallback:
    """load_api_key falls back to the programmatic key file."""

    def test_programmatic_file_used_when_primary_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("N8N_API_KEY", raising=False)
        monkeypatch.setenv("N8N_API_KEY_FILE", str(tmp_path / "missing-primary"))
        fallback = tmp_path / "programmatic"
        fallback.write_text("fallback-token")
        fallback.chmod(0o600)
        monkeypatch.setenv("N8N_PROGRAMMATIC_API_KEY_FILE", str(fallback))
        assert load_api_key() == "fallback-token"

    def test_returns_none_when_nothing_configured(self, monkeypatch, tmp_path):
        monkeypatch.delenv("N8N_API_KEY", raising=False)
        monkeypatch.setenv("N8N_API_KEY_FILE", str(tmp_path / "missing-primary"))
        monkeypatch.setenv("N8N_PROGRAMMATIC_API_KEY_FILE", str(tmp_path / "missing-fallback"))
        assert load_api_key() is None


class TestExecuteWebhookSigned:
    """_execute_webhook_signed: HMAC headers, byte body, retry behavior."""

    @pytest.fixture(autouse=True)
    def _webhook_secret(self, monkeypatch):
        # Hermetic conftest blanks SUPERBOT_WEBHOOK_SECRET; signing needs one.
        monkeypatch.setattr("config.SUPERBOT_WEBHOOK_SECRET",
                            "test-webhook-secret-for-tests", raising=False)

    def test_sends_hmac_headers_and_byte_body(self):
        import n8n_webhook_auth
        from n8n_client import N8nClient
        client = N8nClient(api_key="k")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"ok": true}'
        mock_resp.json.return_value = {"ok": True}

        with patch.object(client._webhook_session, "request", return_value=mock_resp) as req:
            result = client._execute_webhook_signed(
                "superbot-monitor", {"action": "health_check"}, "POST", (1, 3), 1)

        assert result == {"ok": True}
        kwargs = req.call_args.kwargs
        body = kwargs["data"]
        headers = kwargs["headers"]
        assert isinstance(body, bytes)
        body_text = body.decode("utf-8")
        # The signature must verify with the receiver-side verifier.
        ok, reason = n8n_webhook_auth.verify_request(
            headers["X-Superbot-Timestamp"], headers["X-Superbot-Nonce"],
            body_text, headers["X-Superbot-Signature"])
        assert ok, reason
        # The gate key rides in X-Superbot-Key and must not be empty.
        assert headers["X-Superbot-Key"]
        # Body is the canonical compact JSON form.
        assert body_text == '{"action":"health_check"}'

    def test_signed_webhook_nonce_is_unique(self):
        from n8n_client import N8nClient
        client = N8nClient(api_key="k")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b""

        nonces = []
        with patch.object(client._webhook_session, "request", return_value=mock_resp) as req:
            for _ in range(2):
                client._execute_webhook_signed("p", {}, "POST", (1, 3), 1)
            for call in req.call_args_list:
                nonces.append(call.kwargs["headers"]["X-Superbot-Nonce"])
        assert len(set(nonces)) == 2

    def test_signed_webhook_retries_on_connection_error(self):
        import requests
        from n8n_client import N8nClient
        client = N8nClient(api_key="k")
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.content = b'{"status": "ok"}'
        ok_resp.json.return_value = {"status": "ok"}

        with patch.object(client._webhook_session, "request",
                          side_effect=[requests.ConnectionError("down"), ok_resp]) as req, \
             patch("n8n_client.time.sleep"):
            result = client._execute_webhook_signed("p", {}, "POST", (1, 3), 2)
        assert result == {"status": "ok"}
        assert req.call_count == 2

    def test_signed_webhook_429_records_rejection(self, monkeypatch):
        import requests
        from n8n_client import N8nClient
        fake_limiter = MagicMock()
        monkeypatch.setattr("n8n_client.rate_limit.default_limiter", lambda: fake_limiter)
        client = N8nClient(api_key="k")
        resp = MagicMock()
        resp.status_code = 429
        http_error = requests.HTTPError("rate limited")
        http_error.response = resp
        resp.raise_for_status.side_effect = http_error

        with patch.object(client._webhook_session, "request", return_value=resp):
            with pytest.raises(Exception):
                client._execute_webhook_signed("p", {}, "POST", (1, 3), 1)
        fake_limiter.record_rejection.assert_called_once_with("n8n_webhook")

    def test_signed_webhook_5xx_retries_then_raises(self, monkeypatch):
        import requests
        from n8n_client import N8nClient
        monkeypatch.setattr("n8n_client.time.sleep", lambda *_: None)
        client = N8nClient(api_key="k")
        resp = MagicMock()
        resp.status_code = 503
        http_error = requests.HTTPError("unavailable")
        http_error.response = resp
        resp.raise_for_status.side_effect = http_error

        with patch.object(client._webhook_session, "request", return_value=resp) as req:
            with pytest.raises(Exception):
                client._execute_webhook_signed("p", {}, "POST", (1, 3), 2)
        assert req.call_count == 2

    def test_signed_webhook_4xx_does_not_retry(self):
        import requests
        from n8n_client import N8nClient
        client = N8nClient(api_key="k")
        resp = MagicMock()
        resp.status_code = 403
        http_error = requests.HTTPError("forbidden")
        http_error.response = resp
        resp.raise_for_status.side_effect = http_error

        with patch.object(client._webhook_session, "request", return_value=resp) as req:
            with pytest.raises(Exception):
                client._execute_webhook_signed("p", {}, "POST", (1, 3), 3)
        assert req.call_count == 1

    def test_signed_webhook_non_json_response_returns_raw(self):
        from n8n_client import N8nClient
        client = N8nClient(api_key="k")
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"plain-ack"
        resp.json.side_effect = ValueError("no json")
        resp.text = "plain-ack"

        with patch.object(client._webhook_session, "request", return_value=resp):
            result = client._execute_webhook_signed("p", {}, "POST", (1, 3), 1)
        assert result == {"raw": "plain-ack"}


class TestWorkflowHelpers:
    """get_workflow_by_name / ensure_workflow_active."""

    def test_get_workflow_by_name_returns_match(self):
        from n8n_client import N8nClient
        client = N8nClient(api_key="k")
        workflows = {"data": [{"name": "a", "id": "1"}, {"name": "b", "id": "2"}]}
        with patch.object(client, "list_workflows", return_value=workflows):
            assert client.get_workflow_by_name("b") == {"name": "b", "id": "2"}

    def test_get_workflow_by_name_returns_none_when_absent(self):
        from n8n_client import N8nClient
        client = N8nClient(api_key="k")
        with patch.object(client, "list_workflows", return_value={"data": []}):
            assert client.get_workflow_by_name("missing") is None

    def test_ensure_workflow_active_activates_inactive_workflow(self):
        from n8n_client import N8nClient
        client = N8nClient(api_key="k")
        with patch.object(client, "get_workflow", return_value={"id": "1", "active": False}), \
             patch.object(client, "activate_workflow",
                          return_value={"id": "1", "active": True}) as activate:
            result = client.ensure_workflow_active("1")
        activate.assert_called_once_with("1")
        assert result["active"] is True

    def test_ensure_workflow_active_leaves_active_workflow_alone(self):
        from n8n_client import N8nClient
        client = N8nClient(api_key="k")
        with patch.object(client, "get_workflow", return_value={"id": "1", "active": True}), \
             patch.object(client, "activate_workflow") as activate:
            result = client.ensure_workflow_active("1")
        activate.assert_not_called()
        assert result["active"] is True


class TestExecuteCommandEnabled:
    """_execute_command_enabled: H5 gate for n8n's Execute Command node."""

    def test_defaults_to_disabled(self, monkeypatch):
        from n8n_client import _execute_command_enabled
        monkeypatch.delenv("NODES_INCLUDE", raising=False)
        monkeypatch.delenv("NODES_EXCLUDE", raising=False)
        assert _execute_command_enabled() is False

    def test_nodes_include_enables_it(self, monkeypatch):
        from n8n_client import _execute_command_enabled
        monkeypatch.setenv("NODES_INCLUDE", "n8n-nodes-base.executeCommand")
        monkeypatch.delenv("NODES_EXCLUDE", raising=False)
        assert _execute_command_enabled() is True

    def test_exclude_list_without_it_means_enabled(self, monkeypatch):
        from n8n_client import _execute_command_enabled
        import json as _json
        monkeypatch.delenv("NODES_INCLUDE", raising=False)
        monkeypatch.setenv("NODES_EXCLUDE", _json.dumps(["n8n-nodes-base.other"]))
        assert _execute_command_enabled() is True

    def test_exclude_list_with_it_means_disabled(self, monkeypatch):
        from n8n_client import _execute_command_enabled
        import json as _json
        monkeypatch.delenv("NODES_INCLUDE", raising=False)
        monkeypatch.setenv("NODES_EXCLUDE",
                           _json.dumps(["n8n-nodes-base.executeCommand"]))
        assert _execute_command_enabled() is False

    def test_malformed_exclude_means_disabled(self, monkeypatch):
        from n8n_client import _execute_command_enabled
        monkeypatch.delenv("NODES_INCLUDE", raising=False)
        monkeypatch.setenv("NODES_EXCLUDE", "not-json")
        assert _execute_command_enabled() is False


# ── Transport safety / configuration ──────────────────────────────────

class TestTransportSafety:
    """H2: plaintext http is refused off-loopback; https off-loopback warns."""

    def test_http_non_loopback_refused(self):
        with pytest.raises(ValueError, match="refusing plaintext"):
            N8nClient(base_url="http://192.168.1.10:5678", api_key="k")

    def test_https_non_loopback_warns_but_allowed(self, caplog):
        with caplog.at_level(logging.WARNING, logger="n8n_client"):
            client = N8nClient(base_url="https://n8n.example.com", api_key="k")
        assert client.base_url == "https://n8n.example.com"
        assert "not loopback" in caplog.text

    def test_loopback_variants_allowed(self):
        for url in ("http://127.0.0.1:5678", "http://127.0.0.2:5678",
                    "http://localhost:5678", "http://[::1]:5678",
                    "http://0.0.0.0:5678"):
            client = N8nClient(base_url=url, api_key="k")
            assert client.base_url.rstrip("/") == url

    def test_trailing_slash_stripped(self):
        client = N8nClient(base_url="http://127.0.0.1:5678/", api_key="k")
        assert client.base_url == "http://127.0.0.1:5678"


class TestSessionConfiguration:
    """The API session carries the gate key; the webhook session never does."""

    def test_api_session_carries_key_and_json_headers(self):
        client = N8nClient(base_url="http://127.0.0.1:5678", api_key="secret-key")
        assert client.session.headers["X-N8N-API-KEY"] == "secret-key"
        assert client.session.headers["Content-Type"] == "application/json"
        assert client.session.headers["Accept"] == "application/json"

    def test_webhook_session_has_no_api_key(self):
        client = N8nClient(base_url="http://127.0.0.1:5678", api_key="secret-key")
        assert "X-N8N-API-KEY" not in client._webhook_session.headers
        assert client._webhook_session.headers["Content-Type"] == "application/json"

    def test_adapters_mounted_for_http_and_https(self):
        client = N8nClient(base_url="http://127.0.0.1:5678", api_key="k")
        assert "http://" in client.session.adapters
        assert "https://" in client.session.adapters
        assert "http://" in client._webhook_session.adapters
        assert "https://" in client._webhook_session.adapters


# ── REST API wrapper (_request) ─────────────────────────────────────

class TestRestApiRequest:
    def test_list_workflows_sends_limit_and_skip(self):
        client = N8nClient(api_key="k")
        resp = _ok_resp({"data": []})
        with patch.object(client.session, "request", return_value=resp) as mock_req:
            result = client.list_workflows(limit=5, skip=10)
        assert result == {"data": []}
        args, kwargs = mock_req.call_args
        assert args[0] == "GET"
        assert args[1].endswith("/api/v1/workflows")
        assert kwargs["params"] == {"limit": 5, "skip": 10}

    def test_get_workflow_url(self):
        client = N8nClient(api_key="k")
        with patch.object(client.session, "request", return_value=_ok_resp({"id": "abc"})) as mock_req:
            assert client.get_workflow("abc") == {"id": "abc"}
        assert mock_req.call_args[0][1].endswith("/api/v1/workflows/abc")

    def test_create_workflow_posts_data(self):
        client = N8nClient(api_key="k")
        with patch.object(client.session, "request", return_value=_ok_resp({"id": "new"})) as mock_req:
            assert client.create_workflow({"name": "wf"}) == {"id": "new"}
        args, kwargs = mock_req.call_args
        assert args[0] == "POST"
        assert kwargs["json"] == {"name": "wf"}

    def test_activate_workflow_url(self):
        client = N8nClient(api_key="k")
        with patch.object(client.session, "request", return_value=_ok_resp({"active": True})) as mock_req:
            assert client.activate_workflow("wf1") == {"active": True}
        assert mock_req.call_args[0][1].endswith("/api/v1/workflows/wf1/activate")

    def test_empty_content_returns_empty_dict(self):
        client = N8nClient(api_key="k")
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b""
        with patch.object(client.session, "request", return_value=resp):
            assert client.get_workflow("x") == {}

    def test_retries_on_5xx_then_succeeds(self, no_sleep):
        client = N8nClient(api_key="k")
        fail_resp = _err_resp(500)
        ok_resp = _ok_resp({"data": [1]})
        with patch.object(client.session, "request", side_effect=[fail_resp, ok_resp]) as mock_req:
            assert client.list_workflows() == {"data": [1]}
            assert mock_req.call_count == 2

    def test_4xx_not_retried(self, no_sleep):
        client = N8nClient(api_key="k")
        bad_resp = _err_resp(400, "bad request")
        with patch.object(client.session, "request", return_value=bad_resp) as mock_req:
            with pytest.raises(requests.HTTPError):
                client.list_workflows()
            assert mock_req.call_count == 1

    def test_connection_error_not_retried(self, no_sleep):
        """Connection errors carry no HTTP response: _request re-raises
        immediately (only 5xx responses are retried)."""
        client = N8nClient(api_key="k")
        with patch.object(client.session, "request",
                          side_effect=requests.ConnectionError("boom")) as mock_req:
            with pytest.raises(requests.ConnectionError):
                client.list_workflows()
            assert mock_req.call_count == 1

    def test_get_workflow_by_name_found_and_missing(self):
        client = N8nClient(api_key="k")
        page = {"data": [{"name": "a", "id": "1"}, {"name": "b", "id": "2"}]}
        with patch.object(client, "list_workflows", return_value=page):
            assert client.get_workflow_by_name("b")["id"] == "2"
            assert client.get_workflow_by_name("zzz") is None

    def test_ensure_workflow_active_activates_only_when_inactive(self):
        client = N8nClient(api_key="k")
        with patch.object(client, "get_workflow", return_value={"id": "w", "active": True}):
            with patch.object(client, "activate_workflow") as mock_act:
                assert client.ensure_workflow_active("w")["active"] is True
                mock_act.assert_not_called()
        with patch.object(client, "get_workflow", return_value={"id": "w", "active": False}):
            with patch.object(client, "activate_workflow",
                              return_value={"id": "w", "active": True}) as mock_act:
                assert client.ensure_workflow_active("w")["active"] is True
                mock_act.assert_called_once_with("w")


# ── HMAC-signed webhooks ──────────────────────────────────────────────

class TestSignedWebhook:
    def test_signature_headers_and_body(self, monkeypatch):
        monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "n8n-client-test-secret")
        client = N8nClient(api_key="k")
        resp = _ok_resp({"status": "ok"})
        payload = {"action": "settlement", "n": 1}
        with patch.object(client._webhook_session, "request", return_value=resp) as mock_req:
            out = client._execute_webhook_signed("superbot-trade", payload)
        assert out == {"status": "ok"}
        args, kwargs = mock_req.call_args
        headers = kwargs["headers"]
        assert "X-N8N-API-KEY" not in headers
        assert headers["X-Superbot-Key"]  # gate key present
        assert headers["X-Superbot-Timestamp"].isdigit()
        assert len(headers["X-Superbot-Nonce"]) == 32
        body = kwargs["data"]
        assert isinstance(body, bytes)  # byte-safe Content-Length
        expected = n8n_webhook_auth.sign_body(
            headers["X-Superbot-Timestamp"], headers["X-Superbot-Nonce"], body.decode())
        assert headers["X-Superbot-Signature"] == expected

    def test_non_ascii_body_signed_byte_exact(self, monkeypatch):
        monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "n8n-client-test-secret")
        client = N8nClient(api_key="k")
        resp = _ok_resp({"ok": True})
        payload = {"note": "r\u00e9sum\u00e9"}
        with patch.object(client._webhook_session, "request", return_value=resp) as mock_req:
            client._execute_webhook_signed("superbot-trade", payload)
        kwargs = mock_req.call_args.kwargs
        expected_body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        assert kwargs["data"] == expected_body.encode("utf-8")

    def test_raw_text_response_returns_raw_dict(self, monkeypatch):
        monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "n8n-client-test-secret")
        client = N8nClient(api_key="k")
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"plain text"
        resp.json.side_effect = ValueError("no json")
        resp.text = "plain text"
        with patch.object(client._webhook_session, "request", return_value=resp):
            assert client._execute_webhook_signed("p", {}) == {"raw": "plain text"}

    def test_retries_on_connection_error(self, monkeypatch, no_sleep):
        monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "n8n-client-test-secret")
        client = N8nClient(api_key="k")
        ok = _ok_resp({"ok": True})
        with patch.object(client._webhook_session, "request",
                          side_effect=[requests.ConnectionError("boom"), ok]) as mock_req:
            assert client._execute_webhook_signed("p", {}) == {"ok": True}
            assert mock_req.call_count == 2

    def test_429_records_rate_limit_rejection(self, monkeypatch, no_sleep):
        monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "n8n-client-test-secret")
        client = N8nClient(api_key="k")
        limiter = MagicMock()
        monkeypatch.setattr(rate_limit, "default_limiter", lambda: limiter)
        bad = _err_resp(429, "too many")
        with patch.object(client._webhook_session, "request", return_value=bad):
            with pytest.raises(requests.HTTPError):
                client._execute_webhook_signed("p", {}, max_retries=1)
        limiter.record_rejection.assert_called_once_with("n8n_webhook")
        limiter.acquire.assert_called_once_with("n8n_webhook")

    def test_4xx_not_retried(self, monkeypatch, no_sleep):
        monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "n8n-client-test-secret")
        client = N8nClient(api_key="k")
        bad = _err_resp(400, "bad request")
        with patch.object(client._webhook_session, "request", return_value=bad) as mock_req:
            with pytest.raises(requests.HTTPError):
                client._execute_webhook_signed("p", {}, max_retries=3)
            assert mock_req.call_count == 1

    def test_sign_body_backward_compat(self, monkeypatch):
        monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "n8n-client-test-secret")
        assert _sign_body("123", "body") == n8n_webhook_auth.sign_body("123", "", "body")


class TestSyncWebhook:
    """_execute_webhook_sync: API-key session, retry and rate-limit behavior."""

    def test_raw_text_response_returns_raw_dict(self):
        client = N8nClient(api_key="k")
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"plain"
        resp.json.side_effect = ValueError("no json")
        resp.text = "plain"
        with patch.object(client.session, "request", return_value=resp):
            assert client._execute_webhook_sync("p", {}) == {"raw": "plain"}

    def test_retries_on_connection_error(self, no_sleep):
        client = N8nClient(api_key="k")
        ok = _ok_resp({"ok": True})
        with patch.object(client.session, "request",
                          side_effect=[requests.Timeout("slow"), ok]) as mock_req:
            assert client._execute_webhook_sync("p", {}) == {"ok": True}
            assert mock_req.call_count == 2

    def test_429_records_rate_limit_rejection(self, monkeypatch, no_sleep):
        client = N8nClient(api_key="k")
        limiter = MagicMock()
        monkeypatch.setattr(rate_limit, "default_limiter", lambda: limiter)
        bad = _err_resp(429, "too many")
        with patch.object(client.session, "request", return_value=bad):
            with pytest.raises(requests.HTTPError):
                client._execute_webhook_sync("p", {}, max_retries=1)
        limiter.record_rejection.assert_called_once_with("n8n_webhook")


# ── execute_webhook: fire_and_forget vs synchronous ──────────────────

class _FakeOutboxDB:
    def __init__(self, fail_insert=False):
        self.rows = []
        self.fail_insert = fail_insert

    def insert_outbox_event(self, **kwargs):
        if self.fail_insert:
            raise RuntimeError("db down")
        self.rows.append(kwargs)
        return len(self.rows)


class TestExecuteWebhookFireAndForget:
    def _patch_db(self, monkeypatch, db):
        monkeypatch.setattr("local_db.get_local_db", lambda: db)

    def test_normal_event_enqueued_in_outbox(self, monkeypatch):
        client = N8nClient(api_key="k")
        db = _FakeOutboxDB()
        self._patch_db(monkeypatch, db)
        result = client.execute_webhook("superbot-trade",
                                        {"type": "settlement", "ticker": "KXBTC"},
                                        fire_and_forget=True)
        assert result == {}
        assert len(db.rows) == 1
        row = db.rows[0]
        assert row["event_type"] == "settlement"
        assert row["delivery_class"] == "normal"
        assert row["venue"] == "kalshi"
        assert row["mode"] == "dry"  # conftest pins DRY_RUN=true
        assert row["webhook_path"] == "superbot-trade"
        assert json.loads(row["payload_json"])["ticker"] == "KXBTC"

    def test_event_type_read_from_envelope_type(self, monkeypatch):
        client = N8nClient(api_key="k")
        db = _FakeOutboxDB()
        self._patch_db(monkeypatch, db)
        client.execute_webhook("superbot-trade", {"type": "risk.halt"}, fire_and_forget=True)
        assert db.rows[0]["delivery_class"] == "critical"

    def test_event_type_read_from_legacy_event_type_field(self, monkeypatch):
        client = N8nClient(api_key="k")
        db = _FakeOutboxDB()
        self._patch_db(monkeypatch, db)
        client.execute_webhook("superbot-trade", {"event_type": "settlement"},
                               fire_and_forget=True)
        assert db.rows[0]["event_type"] == "settlement"

    def test_event_type_defaults_to_webhook_path(self, monkeypatch):
        client = N8nClient(api_key="k")
        db = _FakeOutboxDB()
        self._patch_db(monkeypatch, db)
        client.execute_webhook("superbot-trade", {}, fire_and_forget=True)
        assert db.rows[0]["event_type"] == "superbot.trade"

    def test_unknown_event_type_defaults_to_critical_lane(self, monkeypatch):
        client = N8nClient(api_key="k")
        db = _FakeOutboxDB()
        self._patch_db(monkeypatch, db)
        client.execute_webhook("superbot-trade", {"type": "totally.unknown"},
                               fire_and_forget=True)
        assert db.rows[0]["delivery_class"] == "critical"

    def test_telemetry_never_touches_db(self, monkeypatch):
        client = N8nClient(api_key="k")
        db = _FakeOutboxDB()
        self._patch_db(monkeypatch, db)
        result = client.execute_webhook("superbot-monitor", {"type": "heartbeat"},
                                        fire_and_forget=True)
        assert result == {}
        assert db.rows == []
        assert "superbot-monitor|kalshi" in n8n_client._telemetry_pending

    def test_outbox_sender_started_on_enqueue(self, monkeypatch):
        client = N8nClient(api_key="k")
        db = _FakeOutboxDB()
        self._patch_db(monkeypatch, db)
        calls = []
        monkeypatch.setattr(n8n_client, "_start_outbox_sender", lambda: calls.append(1))
        client.execute_webhook("superbot-trade", {"type": "settlement"},
                               fire_and_forget=True)
        assert calls == [1]

    def test_critical_insert_failure_falls_back_to_sync_delivery(self, monkeypatch):
        client = N8nClient(api_key="k")
        db = _FakeOutboxDB(fail_insert=True)
        self._patch_db(monkeypatch, db)
        synced = []

        def fake_sync(*args, **kwargs):
            synced.append((args, kwargs))
            return {"ok": True}

        monkeypatch.setattr(client, "_execute_webhook_sync", fake_sync)
        result = client.execute_webhook("superbot-trade", {"type": "risk.halt"},
                                        fire_and_forget=True)
        assert result == {}
        assert len(synced) == 1

    def test_critical_insert_and_sync_failure_writes_sidecar(self, monkeypatch, tmp_path):
        client = N8nClient(api_key="k")
        db = _FakeOutboxDB(fail_insert=True)
        self._patch_db(monkeypatch, db)
        monkeypatch.setattr(client, "_execute_webhook_sync",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("dead")))
        monkeypatch.setattr(config, "SUPERBOT_DATA_DIR", str(tmp_path))
        monkeypatch.setattr("emit.emit_event", lambda *a, **kw: None)
        result = client.execute_webhook("superbot-trade", {"type": "risk.halt"},
                                        fire_and_forget=True)
        assert result == {}
        files = list((tmp_path / "outbox_failed").glob("*.json"))
        assert len(files) == 1
        rec = json.loads(files[0].read_text())
        assert rec["type"] == "risk.halt"
        assert rec["webhook_path"] == "superbot-trade"
        assert "dead" in rec["error"]

    def test_normal_insert_failure_no_sync_fallback_no_sidecar(self, monkeypatch, tmp_path):
        client = N8nClient(api_key="k")
        db = _FakeOutboxDB(fail_insert=True)
        self._patch_db(monkeypatch, db)
        synced = []
        monkeypatch.setattr(client, "_execute_webhook_sync",
                            lambda *a, **kw: synced.append(1))
        monkeypatch.setattr(config, "SUPERBOT_DATA_DIR", str(tmp_path))
        result = client.execute_webhook("superbot-trade", {"type": "settlement"},
                                        fire_and_forget=True)
        assert result == {}
        assert synced == []
        assert not (tmp_path / "outbox_failed").exists()

    def test_mode_is_live_when_dry_run_disabled(self, monkeypatch):
        client = N8nClient(api_key="k")
        db = _FakeOutboxDB()
        self._patch_db(monkeypatch, db)
        monkeypatch.setattr(config, "DRY_RUN", False)
        client.execute_webhook("superbot-trade", {"type": "settlement"},
                               fire_and_forget=True)
        assert db.rows[0]["mode"] == "live"


# ── Telemetry coalescing ──────────────────────────────────────────────

class TestTelemetryCoalescing:
    def test_same_key_latest_wins(self):
        item1 = ("client", "superbot-monitor", {"seq": 1}, "POST")
        item2 = ("client", "superbot-monitor", {"seq": 2}, "POST")
        _enqueue_telemetry("superbot-monitor|kalshi", item1)
        _enqueue_telemetry("superbot-monitor|kalshi", item2)
        assert n8n_client._telemetry_pending["superbot-monitor|kalshi"][2]["seq"] == 2
        assert len(n8n_client._telemetry_pending) == 1

    def test_drop_oldest_and_count_when_full(self, monkeypatch):
        monkeypatch.setattr(n8n_client, "MAX_TELEMETRY_QUEUE", 2)
        for key in ("a|kalshi", "b|kalshi", "c|kalshi"):
            _enqueue_telemetry(key, ("client", "p", {"k": key}, "POST"))
        assert "a|kalshi" not in n8n_client._telemetry_pending
        assert set(n8n_client._telemetry_pending) == {"b|kalshi", "c|kalshi"}
        assert n8n_client._telemetry_dropped_get() == 1

    def test_updating_existing_key_does_not_trigger_eviction(self, monkeypatch):
        monkeypatch.setattr(n8n_client, "MAX_TELEMETRY_QUEUE", 1)
        _enqueue_telemetry("a|kalshi", ("client", "p", {"seq": 1}, "POST"))
        _enqueue_telemetry("a|kalshi", ("client", "p", {"seq": 2}, "POST"))
        assert n8n_client._telemetry_dropped_get() == 0
        assert n8n_client._telemetry_pending["a|kalshi"][2]["seq"] == 2

    def test_drain_telemetry_delivers_all_and_clears(self):
        delivered = []

        class _FakeClient:
            def _execute_webhook_signed(self, path, data, method, timeout, retries):
                delivered.append((path, data, timeout))

        fake = _FakeClient()
        _enqueue_telemetry("m|kalshi", (fake, "superbot-monitor", {"n": 1}, "POST"))
        _enqueue_telemetry("m|poly", (fake, "superbot-monitor", {"n": 2}, "POST"))
        count = _drain_telemetry()
        assert count == 2
        assert n8n_client._telemetry_pending == {}
        assert all(t == TELEMETRY_WEBHOOK_TIMEOUT for _, _, t in delivered)

    def test_drain_telemetry_swallows_delivery_errors(self):
        class _BoomClient:
            def _execute_webhook_signed(self, *a, **kw):
                raise RuntimeError("n8n down")

        _enqueue_telemetry("m|kalshi", (_BoomClient(), "p", {"n": 1}, "POST"))
        assert _drain_telemetry() == 1  # counted, not raised


# ── Circuit breaker ───────────────────────────────────────────────────

class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = _CircuitBreaker(threshold=3, cooldown_s=60)
        assert cb.allow() is True
        assert cb.is_open is False

    def test_opens_after_threshold_consecutive_failures(self):
        cb = _CircuitBreaker(threshold=3, cooldown_s=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.allow() is True
        cb.record_failure()
        assert cb.allow() is False
        assert cb.is_open is True

    def test_success_resets_consecutive_failures(self):
        cb = _CircuitBreaker(threshold=3, cooldown_s=60)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        assert cb.allow() is True

    def test_open_breaker_allows_after_cooldown(self):
        cb = _CircuitBreaker(threshold=1, cooldown_s=60)
        cb.record_failure()
        assert cb.allow() is False
        cb._opened_at = time.time() - 120  # simulate cooldown elapsed
        assert cb.allow() is True

    def test_reopens_after_cooldown_still_failing(self):
        cb = _CircuitBreaker(threshold=1, cooldown_s=60)
        cb.record_failure()
        cb._opened_at = time.time() - 120
        cb.record_failure()  # failures >= threshold and cooldown passed -> reopen
        assert cb.allow() is False

    def test_trip_emits_breaker_trip_once(self, monkeypatch):
        events = []
        monkeypatch.setattr("emit.emit_event", lambda *a, **kw: events.append(a))
        cb = _CircuitBreaker(threshold=2, cooldown_s=60)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()  # already open; must not re-emit
        trips = [e for e in events if e[1] == "breaker.trip"]
        assert len(trips) == 1

    def test_recovery_emits_breaker_clear(self, monkeypatch):
        events = []
        monkeypatch.setattr("emit.emit_event", lambda *a, **kw: events.append(a))
        cb = _CircuitBreaker(threshold=1, cooldown_s=60)
        cb.record_failure()
        cb.record_success()
        assert any(e[1] == "breaker.clear" for e in events)


# ── _deliver_outbox_row / _lane_timeout ───────────────────────────────

def _outbox_row(**over):
    row = {"id": 101, "event_id": "evt-1", "type": "risk.halt",
           "delivery_class": "critical",
           "payload_json": json.dumps({"reason": "halt"}),
           "webhook_path": "superbot-critical",
           "venue": "kalshi", "mode": "dry",
           "created_at": "2026-11-19 00:00:00", "attempts": 0,
           "next_attempt_at": "2026-11-19 00:00:00", "last_error": None,
           "status": "pending"}
    row.update(over)
    return row


class _DeliveryRecorderDB:
    def __init__(self):
        self.sent = []
        self.dead = []

    def mark_outbox_sent(self, event_id):
        self.sent.append(event_id)
        return True

    def mark_outbox_dead(self, event_id, last_error, max_attempts=10):
        self.dead.append((event_id, last_error, max_attempts))
        return True


class TestDeliverOutboxRow:
    def test_success_marks_sent_and_resets_breaker(self, monkeypatch):
        client = N8nClient(api_key="k")
        monkeypatch.setattr(client, "_execute_webhook_signed",
                            lambda *a, **kw: {"ok": True})
        monkeypatch.setattr(n8n_client, "get_client", lambda: client)
        db = _DeliveryRecorderDB()
        n8n_client._outbox_breaker._consecutive_failures = 5
        _deliver_outbox_row(db, _outbox_row())
        assert db.sent == [101]
        assert db.dead == []
        assert n8n_client._outbox_breaker._consecutive_failures == 0

    def test_failure_marks_dead_and_counts_failure(self, monkeypatch):
        client = N8nClient(api_key="k")

        def boom(*a, **kw):
            raise RuntimeError("boom message")

        monkeypatch.setattr(client, "_execute_webhook_signed", boom)
        monkeypatch.setattr(n8n_client, "get_client", lambda: client)
        db = _DeliveryRecorderDB()
        _deliver_outbox_row(db, _outbox_row())
        assert db.sent == []
        assert len(db.dead) == 1
        event_id, last_error, max_attempts = db.dead[0]
        assert event_id == 101
        assert "boom message" in last_error
        assert max_attempts == n8n_client.MAX_OUTBOX_ATTEMPTS
        assert n8n_client._outbox_breaker._consecutive_failures == 1

    def test_missing_webhook_path_falls_back_to_event_type(self, monkeypatch):
        client = N8nClient(api_key="k")
        captured = []

        def fake_signed(path, data, method, timeout, retries):
            captured.append(path)
            return {}

        monkeypatch.setattr(client, "_execute_webhook_signed", fake_signed)
        monkeypatch.setattr(n8n_client, "get_client", lambda: client)
        db = _DeliveryRecorderDB()
        row = _outbox_row()
        del row["webhook_path"]
        _deliver_outbox_row(db, row)
        assert captured == ["risk.halt"]

    def test_none_payload_defaults_to_empty_object(self, monkeypatch):
        client = N8nClient(api_key="k")
        captured = []

        def fake_signed(path, data, method, timeout, retries):
            captured.append(data)
            return {}

        monkeypatch.setattr(client, "_execute_webhook_signed", fake_signed)
        monkeypatch.setattr(n8n_client, "get_client", lambda: client)
        db = _DeliveryRecorderDB()
        _deliver_outbox_row(db, _outbox_row(payload_json=None))
        assert captured == [{}]

    def test_lane_timeout_mapping(self):
        assert _lane_timeout("critical") == CRITICAL_WEBHOOK_TIMEOUT
        assert _lane_timeout("normal") == NORMAL_WEBHOOK_TIMEOUT
        assert _lane_timeout("telemetry") == TELEMETRY_WEBHOOK_TIMEOUT
        assert _lane_timeout("bogus") == NORMAL_WEBHOOK_TIMEOUT
        assert _lane_timeout("") == NORMAL_WEBHOOK_TIMEOUT


# ── drain_outbox ──────────────────────────────────────────────────────

class _DrainDB:
    def __init__(self, rows_by_lane):
        self.rows_by_lane = {k: list(v) for k, v in rows_by_lane.items()}
        self.calls = []

    def claim_pending_outbox_events(self, limit=50, lane=None):
        self.calls.append(lane)
        rows = self.rows_by_lane.get(lane, [])
        self.rows_by_lane[lane] = []
        return rows


class TestDrainOutbox:
    def test_delivers_pending_rows_and_returns_count(self, monkeypatch):
        db = _DrainDB({"critical": [_outbox_row(id=1)],
                       "normal": [_outbox_row(id=2, delivery_class="normal")]})
        monkeypatch.setattr("local_db.get_local_db", lambda: db)
        delivered = []
        monkeypatch.setattr(n8n_client, "_deliver_outbox_row",
                            lambda db_, row: delivered.append(row["id"]))
        assert drain_outbox(timeout_seconds=5) == 2
        assert delivered == [1, 2]

    def test_timeout_zero_delivers_nothing(self, monkeypatch):
        db = _DrainDB({"critical": [_outbox_row(id=1)]})
        monkeypatch.setattr("local_db.get_local_db", lambda: db)
        monkeypatch.setattr(n8n_client, "_deliver_outbox_row",
                            lambda db_, row: (_ for _ in ()).throw(AssertionError("must not deliver")))
        assert drain_outbox(timeout_seconds=0) == 0

    def test_normal_lane_drains_telemetry_after_delivering(self, monkeypatch):
        db = _DrainDB({"critical": [],
                       "normal": [_outbox_row(id=2, delivery_class="normal")]})
        monkeypatch.setattr("local_db.get_local_db", lambda: db)
        monkeypatch.setattr(n8n_client, "_deliver_outbox_row", lambda db_, row: None)
        telemetry = []
        monkeypatch.setattr(n8n_client, "_drain_telemetry",
                            lambda: telemetry.append(True) or 0)
        assert drain_outbox(timeout_seconds=5) == 1
        assert telemetry == [True]

    def test_row_delivery_errors_are_swallowed(self, monkeypatch):
        db = _DrainDB({"critical": [_outbox_row(id=1)], "normal": []})
        monkeypatch.setattr("local_db.get_local_db", lambda: db)

        def boom(db_, row):
            raise RuntimeError("boom")

        monkeypatch.setattr(n8n_client, "_deliver_outbox_row", boom)
        assert drain_outbox(timeout_seconds=5) == 0  # logged, not raised


# ── outbox_metrics ────────────────────────────────────────────────────

class TestOutboxMetrics:
    def test_metrics_merge_db_depth_with_memory_counters(self, monkeypatch):
        class _StatsDB:
            def get_outbox_depth_stats(self):
                return {"outbox_depth": 3, "oldest_undelivered_age_s": 42.0}

        monkeypatch.setattr("local_db.get_local_db", lambda: _StatsDB())
        n8n_client._telemetry_dropped = 7
        m = outbox_metrics()
        assert m["outbox_depth"] == 3
        assert m["oldest_undelivered_age_s"] == 42.0
        assert m["dropped_total"] == 7

    def test_metrics_survive_db_failure(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr("local_db.get_local_db", boom)
        n8n_client._telemetry_dropped = 4
        m = outbox_metrics()
        assert m["outbox_depth"] == 0
        assert m["oldest_undelivered_age_s"] == 0.0
        assert m["dropped_total"] == 4


# ── Health check / singleton / startup ────────────────────────────────

class TestHealthCheckDetails:
    def test_health_check_uses_monitor_webhook_synchronously(self, monkeypatch):
        client = N8nClient(api_key="k")
        with patch.object(client, "execute_webhook", return_value={"ok": True}) as mock_exec:
            monkeypatch.setattr(n8n_client, "_client", client)
            assert n8n_client.n8n_health_check() is True
            mock_exec.assert_called_once_with(
                "superbot-monitor", {"action": "health_check"}, fire_and_forget=False)


class TestGetClientSingleton:
    def test_get_client_caches_instance(self, monkeypatch):
        monkeypatch.setattr(n8n_client, "_client", None)
        c1 = get_client()
        c2 = get_client()
        assert c1 is c2


class TestStartOutboxSender:
    def test_starts_critical_and_normal_threads_once(self, monkeypatch):
        started = []

        class FakeThread:
            def __init__(self, target=None, daemon=None, name=None, kwargs=None):
                self.name = name
                self.kwargs = kwargs or {}
                started.append((name, self.kwargs.get("lane")))

            def start(self):
                pass

        monkeypatch.setattr(n8n_client, "_outbox_sender_started", False)
        monkeypatch.setattr(n8n_client.threading, "Thread", FakeThread)
        _start_outbox_sender()
        _start_outbox_sender()  # second call must be a no-op
        assert started == [("n8n-outbox-critical", "critical"),
                           ("n8n-outbox-normal", "normal")]


class TestWriteOutboxSidecar:
    def test_sidecar_written_with_payload_and_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "SUPERBOT_DATA_DIR", str(tmp_path))
        monkeypatch.setattr("emit.emit_event", lambda *a, **kw: None)
        _write_outbox_sidecar("evt-9", "risk.halt", {"a": 1}, "superbot-critical", "boom")
        files = list((tmp_path / "outbox_failed").glob("evt-9.json"))
        assert len(files) == 1
        rec = json.loads(files[0].read_text())
        assert rec["event_id"] == "evt-9"
        assert rec["type"] == "risk.halt"
        assert rec["webhook_path"] == "superbot-critical"
        assert rec["payload"] == {"a": 1}
        assert rec["error"] == "boom"
        assert "ts" in rec

    def test_sidecar_emit_failure_is_swallowed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "SUPERBOT_DATA_DIR", str(tmp_path))

        def boom(*a, **kw):
            raise RuntimeError("emit down")

        monkeypatch.setattr("emit.emit_event", boom)
        _write_outbox_sidecar("evt-10", "risk.halt", {}, "p", "err")
        assert (tmp_path / "outbox_failed" / "evt-10.json").exists()

    def test_sidecar_unwritable_dir_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(config, "SUPERBOT_DATA_DIR", "/proc/superbot-no-such-dir")
        monkeypatch.setattr("emit.emit_event", lambda *a, **kw: None)
        _write_outbox_sidecar("evt-11", "risk.halt", {}, "p", "err")  # must not raise


# ── API key file loading / startup check ──────────────────────────────

class TestReadApiKeyFile:
    def test_insecure_permissions_rejected(self, tmp_path):
        f = tmp_path / "key"
        f.write_text("secret")
        f.chmod(0o644)  # group/other readable
        assert _read_api_key_file(f) is None

    def test_empty_file_returns_none(self, tmp_path):
        f = tmp_path / "key"
        f.write_text("")
        f.chmod(0o600)
        assert _read_api_key_file(f) is None

    def test_plain_token_file_returns_token(self, tmp_path):
        f = tmp_path / "key"
        f.write_text("plain-token-abc\n")
        f.chmod(0o600)
        assert _read_api_key_file(f) == "plain-token-abc"

    def test_missing_file_returns_none(self, tmp_path):
        assert _read_api_key_file(tmp_path / "nope") is None

    def test_env_var_wins_over_file(self, monkeypatch, tmp_path):
        f = tmp_path / "key"
        f.write_text("file-token")
        f.chmod(0o600)
        monkeypatch.setenv("N8N_API_KEY", "env-token")
        monkeypatch.setenv("N8N_API_KEY_FILE", str(f))
        assert load_api_key() == "env-token"


class TestExecuteCommandEnabled:
    def test_unset_means_disabled(self, monkeypatch):
        monkeypatch.delenv("NODES_EXCLUDE", raising=False)
        monkeypatch.delenv("NODES_INCLUDE", raising=False)
        assert _execute_command_enabled() is False

    def test_nodes_include_reenables(self, monkeypatch):
        monkeypatch.setenv("NODES_INCLUDE", "n8n-nodes-base.executeCommand")
        assert _execute_command_enabled() is True

    def test_nodes_exclude_without_execute_command_means_enabled(self, monkeypatch):
        monkeypatch.setenv("NODES_EXCLUDE", '["n8n-nodes-base.hackerNews"]')
        assert _execute_command_enabled() is True

    def test_nodes_exclude_with_execute_command_means_disabled(self, monkeypatch):
        monkeypatch.setenv("NODES_EXCLUDE", '["n8n-nodes-base.executeCommand"]')
        assert _execute_command_enabled() is False

    def test_nodes_exclude_invalid_json_means_disabled(self, monkeypatch):
        monkeypatch.setenv("NODES_EXCLUDE", "not json")
        assert _execute_command_enabled() is False

    def test_nodes_exclude_non_list_means_disabled(self, monkeypatch):
        monkeypatch.setenv("NODES_EXCLUDE", '{"a": 1}')
        assert _execute_command_enabled() is False


class TestStartupCheck:
    def _client_and_post(self, monkeypatch, resp=None, exc=None):
        client = N8nClient(api_key="k")

        def fake_post(url, json=None, timeout=None):
            if exc is not None:
                raise exc
            return resp

        monkeypatch.setattr(client.session, "post", fake_post)
        monkeypatch.setattr(n8n_client, "get_client", lambda: client)

    def test_fails_when_execute_command_enabled(self, monkeypatch):
        monkeypatch.setenv("NODES_INCLUDE", "n8n-nodes-base.executeCommand")
        monkeypatch.delenv("N8N_HOST", raising=False)
        self._client_and_post(monkeypatch, resp=MagicMock(status_code=403))
        assert n8n_startup_check() is False

    def test_fails_when_unsigned_request_accepted(self, monkeypatch):
        monkeypatch.delenv("NODES_INCLUDE", raising=False)
        monkeypatch.delenv("NODES_EXCLUDE", raising=False)
        monkeypatch.delenv("N8N_HOST", raising=False)
        self._client_and_post(monkeypatch, resp=MagicMock(status_code=200))
        assert n8n_startup_check() is False

    def test_passes_when_unsigned_request_rejected(self, monkeypatch):
        monkeypatch.delenv("NODES_INCLUDE", raising=False)
        monkeypatch.delenv("NODES_EXCLUDE", raising=False)
        monkeypatch.delenv("N8N_HOST", raising=False)
        self._client_and_post(monkeypatch, resp=MagicMock(status_code=403))
        assert n8n_startup_check() is True

    def test_probe_offline_is_not_a_failure(self, monkeypatch):
        monkeypatch.delenv("NODES_INCLUDE", raising=False)
        monkeypatch.delenv("NODES_EXCLUDE", raising=False)
        monkeypatch.delenv("N8N_HOST", raising=False)
        self._client_and_post(monkeypatch, exc=requests.ConnectionError("offline"))
        assert n8n_startup_check() is True

    def test_fails_on_non_loopback_bind(self, monkeypatch):
        monkeypatch.delenv("NODES_INCLUDE", raising=False)
        monkeypatch.delenv("NODES_EXCLUDE", raising=False)
        monkeypatch.setenv("N8N_HOST", "192.168.1.99")
        self._client_and_post(monkeypatch, resp=MagicMock(status_code=403))
        assert n8n_startup_check() is False
