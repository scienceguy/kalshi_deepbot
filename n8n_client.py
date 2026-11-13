"""
n8n_client.py — Programmatic n8n API client for Kalshi SuperBot integration.

Uses the n8n API key (X-N8N-API-KEY header) to:
- List, create, update, delete workflows
- Execute workflows via webhook or API execution endpoint
- Get workflow status and executions
- Manage webhooks

All endpoints live under http://127.0.0.1:5678/api/v1/
"""

import logging
import os
import queue
import stat
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter

import config

log = logging.getLogger("n8n_client")

# Default base URL — n8n runs locally on port 5678
DEFAULT_BASE_URL = "http://127.0.0.1:5678"

# Webhook timeout must be less than CYCLE_TIMEOUT_SECONDS so a webhook
# can't block a full cycle. Default: cycle timeout minus 5s margin.
DEFAULT_WEBHOOK_TIMEOUT = max(1, config.CYCLE_TIMEOUT_SECONDS - 5)

# Non-blocking webhook queue — fire-and-forget to avoid stalling the cycle
_webhook_queue: "queue.Queue[tuple]" = queue.Queue()
_webhook_worker_started = False
_webhook_worker_lock = threading.Lock()


def _webhook_worker():
    """Background worker that processes webhook queue."""
    while True:
        try:
            item = _webhook_queue.get(timeout=1.0)
            if item is None:  # Shutdown signal
                break
            client, webhook_path, data, method, timeout, max_retries = item
            try:
                client._execute_webhook_sync(webhook_path, data, method, timeout, max_retries)
            except Exception as e:
                log.warning("Background webhook %s failed: %s", webhook_path, e)
        except queue.Empty:
            continue
        except Exception as e:
            log.error("Webhook worker error: %s", e)


def _start_webhook_worker():
    """Start the background webhook worker thread."""
    global _webhook_worker_started
    with _webhook_worker_lock:
        if not _webhook_worker_started:
            thread = threading.Thread(target=_webhook_worker, daemon=True, name="n8n-webhook-worker")
            thread.start()
            _webhook_worker_started = True


_N8N_TOKEN_KEY_NAMES = {"n8n_local_access_token", "n8n_api_key", "n8n_access_token",
                         "n8n_api_token", "api_key", "token", "n8n_key"}


def _read_api_key_file(key_file: Path) -> Optional[str]:
    """Read an n8n API key from a protected file.

    Accepts either a bare token on one line or a KEY=VALUE file whose key
    matches a known token name (e.g. n8n_local_access_token). A multi-line
    KEY=VALUE file must never be returned whole — it is not a valid header
    value and requests raises before the HTTP call is even attempted.
    """
    try:
        mode = stat.S_IMODE(os.stat(str(key_file)).st_mode)
        if mode & 0o077:
            log.warning(
                "%s has insecure permissions (mode %o) — "
                "skipping file, falling back to env var only",
                key_file, mode,
            )
            return None
        text = key_file.read_text().strip()
        if not text:
            return None
        if "=" in text:
            for raw_line in text.splitlines():
                k, sep, v = raw_line.partition("=")
                if sep and k.strip().lower() in _N8N_TOKEN_KEY_NAMES:
                    token = v.strip().strip('"').strip("'")
                    return token or None
            log.warning(
                "%s contains KEY=VALUE lines but no recognisable token key — "
                "refusing to use it as an API key",
                key_file,
            )
            return None
        return text
    except Exception:
        return None


def load_api_key() -> Optional[str]:
    """Load n8n API key from env var or n8n_api_key.txt file."""
    api_key = os.environ.get("N8N_API_KEY", "").strip()
    if api_key:
        return api_key

    # Check n8n api key file (path overridable via N8N_API_KEY_FILE)
    key_file = Path(
        os.environ.get(
            "N8N_API_KEY_FILE",
            "/data/data/com.termux/files/home/.n8n/n8n_api_key.txt",
        )
    )
    api_key = _read_api_key_file(key_file)
    if api_key:
        return api_key

    # Also check the programmatic API key file created by n8n setup
    # (path overridable via N8N_PROGRAMMATIC_API_KEY_FILE)
    key_file = Path(
        os.environ.get(
            "N8N_PROGRAMMATIC_API_KEY_FILE",
            "/data/data/com.termux/files/home/n8n-programmatic-api-key.txt",
        )
    )
    return _read_api_key_file(key_file)


class N8nClient:
    """Client for n8n REST API."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or load_api_key()
        self.session = requests.Session()
        self.session.headers.update({
            "X-N8N-API-KEY": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.session.mount("http://", HTTPAdapter(pool_connections=10, pool_maxsize=10))
        self.session.mount("https://", HTTPAdapter(pool_connections=10, pool_maxsize=10))

        if not self.api_key:
            log.warning("No n8n API key found — API calls will fail")

    def _request(self, method: str, path: str, data: Optional[Dict] = None,
                 params: Optional[Dict] = None, timeout: int = 30,
                 max_retries: int = 3) -> Dict[str, Any]:
        """Make an authenticated request to the n8n API.

        Retries on 5xx (server-side, transient) errors with 2-second backoff.
        Does not retry on 4xx (client-side) errors.
        """
        url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        last_err = None
        for attempt in range(max_retries):
            try:
                resp = self.session.request(
                    method, url, json=data, params=params, timeout=timeout
                )
                resp.raise_for_status()
                if resp.content:
                    return resp.json()
                return {}
            except requests.RequestException as e:
                last_err = e
                # Check if it's a 5xx response we should retry
                if hasattr(e, 'response') and e.response is not None:
                    status = e.response.status_code
                    if 500 <= status < 600:
                        if attempt < max_retries - 1:
                            log.warning(
                                "n8n API %s %s: HTTP %s — retry %d/%d",
                                method, path, status, attempt + 1, max_retries,
                            )
                            time.sleep(2.0 * (attempt + 1))
                            continue
                        log.error(
                            "n8n API %s %s: HTTP %s — exhausted %d retries",
                            method, path, status, max_retries,
                        )
                log.error("n8n API %s %s failed: %s", method, path, e)
                raise
        raise last_err

    # ── Workflows ──────────────────────────────────────────────────────────

    def list_workflows(self, limit: int = 50, skip: int = 0) -> Dict[str, Any]:
        """List all workflows."""
        return self._request("GET", "workflows", params={"limit": limit, "skip": skip})

    def get_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Get a single workflow by ID."""
        return self._request("GET", f"workflows/{workflow_id}")

    def create_workflow(self, workflow_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new workflow."""
        return self._request("POST", "workflows", data=workflow_data)

    def update_workflow(self, workflow_id: str, workflow_data: Dict[str, Any]) -> Dict[str, Any]:
        """Update an existing workflow."""
        return self._request("PUT", f"workflows/{workflow_id}", data=workflow_data)

    def delete_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Delete a workflow."""
        return self._request("DELETE", f"workflows/{workflow_id}")

    def activate_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Activate a workflow."""
        return self._request("POST", f"workflows/{workflow_id}/activate")

    def deactivate_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Deactivate a workflow."""
        return self._request("POST", f"workflows/{workflow_id}/deactivate")

    # ── Executions ─────────────────────────────────────────────────────────

    def list_executions(self, workflow_id: Optional[str] = None,
                        limit: int = 20, skip: int = 0) -> Dict[str, Any]:
        """List executions, optionally filtered by workflow."""
        params: Dict[str, Any] = {"limit": limit, "skip": skip}
        if workflow_id:
            params["workflowId"] = workflow_id
        return self._request("GET", "executions", params=params)

    def get_execution(self, execution_id: str) -> Dict[str, Any]:
        """Get execution details by ID."""
        return self._request("GET", f"executions/{execution_id}")

    # ── Webhooks ───────────────────────────────────────────────────────────

    def _execute_webhook_sync(self, webhook_path: str, data: Dict[str, Any],
                              method: str = "POST", timeout: int = DEFAULT_WEBHOOK_TIMEOUT,
                              max_retries: int = 3) -> Dict[str, Any]:
        """Execute a webhook by path synchronously. Returns the webhook response.

        Retries on 5xx (server-side, transient) errors with 2-second backoff.
        Also retries on connection errors and timeouts.
        Does not retry on 4xx (client-side) errors.
        """
        url = f"{self.base_url}/webhook/{webhook_path}"
        last_err = None
        for attempt in range(max_retries):
            try:
                resp = self.session.request(
                    method, url, json=data, timeout=timeout
                )
                resp.raise_for_status()
                if resp.content:
                    try:
                        return resp.json()
                    except ValueError:
                        return {"raw": resp.text}
                return {}
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = e
                if attempt < max_retries - 1:
                    log.warning(
                        "Webhook %s %s: connection/timeout error — retry %d/%d: %s",
                        method, webhook_path, attempt + 1, max_retries, e,
                    )
                    time.sleep(2.0 * (attempt + 1))
                    continue
                log.error("Webhook %s %s: connection/timeout error — exhausted %d retries: %s",
                          method, webhook_path, max_retries, e)
                raise
            except requests.RequestException as e:
                last_err = e
                # Check if it's a 5xx response we should retry
                if hasattr(e, 'response') and e.response is not None:
                    status = e.response.status_code
                    if 500 <= status < 600:
                        if attempt < max_retries - 1:
                            log.warning(
                                "Webhook %s %s: HTTP %s — retry %d/%d",
                                method, webhook_path, status, attempt + 1, max_retries,
                            )
                            time.sleep(2.0 * (attempt + 1))
                            continue
                        log.error(
                            "Webhook %s %s: HTTP %s — exhausted %d retries",
                            method, webhook_path, status, max_retries,
                        )
                log.error("Webhook %s %s failed: %s", method, webhook_path, e)
                raise
        raise last_err

    def execute_webhook(self, webhook_path: str, data: Dict[str, Any],
                        method: str = "POST", timeout: int = DEFAULT_WEBHOOK_TIMEOUT,
                        max_retries: int = 3, fire_and_forget: bool = True) -> Dict[str, Any]:
        """Execute a webhook by path.

        By default (fire_and_forget=True), queues the webhook for background
        execution and returns immediately with an empty dict — the cycle does
        not block waiting for n8n. Set fire_and_forget=False for synchronous
        execution (e.g., health checks where we need the response).
        """
        if fire_and_forget:
            _start_webhook_worker()
            _webhook_queue.put((self, webhook_path, data, method, timeout, max_retries))
            return {}
        else:
            return self._execute_webhook_sync(webhook_path, data, method, timeout, max_retries)

    # ── SuperBot convenience methods ───────────────────────────────────────

    def run_backtest(self, data: Optional[Dict] = None) -> Dict[str, Any]:
        """Execute the SuperBot Backtest webhook."""
        return self.execute_webhook("superbot-backtest", data or {})

    def run_data_collect(self, data: Optional[Dict] = None) -> Dict[str, Any]:
        """Execute the SuperBot Data Collect webhook."""
        return self.execute_webhook("superbot-collect", data or {})

    def run_trade(self, data: Optional[Dict] = None) -> Dict[str, Any]:
        """Execute the SuperBot Trade webhook."""
        return self.execute_webhook("superbot-trade", data or {})

    def run_monitor(self, data: Optional[Dict] = None) -> Dict[str, Any]:
        """Execute the SuperBot Monitor webhook."""
        return self.execute_webhook("superbot-monitor", data or {})

    def get_workflow_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Find a workflow by name."""
        workflows = self.list_workflows(limit=100)
        for w in workflows.get("data", []):
            if w.get("name") == name:
                return w
        return None

    def ensure_workflow_active(self, workflow_id: str) -> Dict[str, Any]:
        """Ensure a workflow is active, activating if needed."""
        wf = self.get_workflow(workflow_id)
        if not wf.get("active"):
            return self.activate_workflow(workflow_id)
        return wf


# Global client instance (lazy)
_client: Optional[N8nClient] = None


def get_client() -> N8nClient:
    """Get or create the global N8nClient instance."""
    global _client
    if _client is None:
        _client = N8nClient()
    return _client


# Convenience functions
def n8n_health_check() -> bool:
    """Verify n8n is reachable via a read-only GET.

    Returns False if n8n is down — so startup can fail fast instead of
    mid-trade. Does NOT write through the settlement webhook (avoids
    side effects during a health check).
    """
    try:
        client = get_client()
        resp = client.session.get(f"{client.base_url}/", timeout=5)
        return resp.status_code < 500
    except Exception:
        return False