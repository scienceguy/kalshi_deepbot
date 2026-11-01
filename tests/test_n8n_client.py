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
        """execute_webhook returns parsed JSON response."""
        client = N8nClient(api_key="test_key")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok"}
        mock_resp.status_code = 200
        mock_resp.content = b'{"status": "ok"}'

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client.execute_webhook("superbot-trade", {"action": "settlement"})
            assert result == {"status": "ok"}

    def test_execute_webhook_raises_on_error(self, monkeypatch):
        """execute_webhook raises exception on non-2xx response."""
        client = N8nClient(api_key="test_key")
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = Exception("Server error")

        with patch.object(client.session, "request", return_value=mock_resp):
            with pytest.raises(Exception):
                client.execute_webhook("superbot-trade", {"action": "settlement"})

    def test_execute_webhook_empty_response(self, monkeypatch):
        """execute_webhook returns empty dict when response has no content."""
        client = N8nClient(api_key="test_key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b""

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client.execute_webhook("superbot-trade", {"action": "settlement"})
            assert result == {}


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
        from n8n_client import n8n_health_check

        client = N8nClient(api_key="test_key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(client.session, "get", return_value=mock_resp):
            assert n8n_health_check() is True

    def test_n8n_health_check_returns_false_on_error(self, monkeypatch):
        """n8n_health_check returns False when GET raises."""
        from n8n_client import n8n_health_check

        from n8n_client import get_client
        with patch.object(get_client().session, "get", side_effect=Exception("Connection error")):
            assert n8n_health_check() is False