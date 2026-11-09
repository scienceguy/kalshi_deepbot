"""Tests for n8n_client.py — n8n REST API client.

These tests mock the n8n API responses to avoid making real network calls.
"""
import pytest
from unittest.mock import MagicMock, patch
from n8n_client import N8nClient, load_api_key


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
        monkeypatch.setattr("n8n_client.Path", lambda p: key_file if "n8n_api_key" in str(p) else p)
        assert load_api_key() == "file_key_value"


class TestN8nClientInit:
    """Tests for N8nClient initialization."""

    def test_init_stores_api_key(self, monkeypatch):
        """Client stores the provided API key."""
        client = N8nClient(api_key="test_key_123")
        assert client.api_key == "test_key_123"
        assert client.base_url == "http://127.0.0.1:5678"

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
        """execute_webhook with fire_and_forget=True returns immediately without waiting."""
        import queue
        from n8n_client import _webhook_queue
        client = N8nClient(api_key="test_key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"status": "ok"}'

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client.execute_webhook("superbot-trade", {"action": "settlement"}, fire_and_forget=True)
            assert result == {}
            # The webhook should be queued
            assert not _webhook_queue.empty()
            # Drain the queue to avoid affecting other tests
            _webhook_queue.get_nowait()


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
        """n8n_health_check returns True when GET succeeds."""
        from n8n_client import n8n_health_check, get_client

        # Get the global client instance and mock its session
        client = get_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(client.session, "get", return_value=mock_resp):
            assert n8n_health_check() is True

    def test_n8n_health_check_returns_false_on_error(self, monkeypatch):
        """n8n_health_check returns False when GET raises."""
        from n8n_client import n8n_health_check, get_client

        client = get_client()
        with patch.object(client.session, "get", side_effect=Exception("Connection error")):
            assert n8n_health_check() is False