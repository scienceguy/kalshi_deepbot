"""
n8n_client.py — Programmatic n8n API client for Kalshi SuperBot integration.

Uses the n8n API key (X-N8N-API-KEY header) to:
- List, create, update, delete workflows
- Execute workflows via webhook or API execution endpoint
- Get workflow status and executions
- Manage webhooks

All endpoints live under http://127.0.0.1:5678/api/v1/
"""

import json
import logging
import os
import stat
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("n8n_client")

# Default base URL — n8n runs locally on port 5678
DEFAULT_BASE_URL = "http://127.0.0.1:5678"


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
    if key_file.exists():
        try:
            # Refuse to read if the file is world-readable (any "other" bit set)
            mode = stat.S_IMODE(os.stat(str(key_file)).st_mode)
            if mode & 0o077:
                log.warning(
                    "n8n_api_key.txt has insecure permissions (mode %o) — "
                    "skipping file, falling back to env var only",
                    mode,
                )
            else:
                api_key = key_file.read_text().strip()
                if api_key:
                    return api_key
        except Exception:
            pass

    # Also check the programmatic API key file created by n8n setup
    # (path overridable via N8N_PROGRAMMATIC_API_KEY_FILE)
    key_file = Path(
        os.environ.get(
            "N8N_PROGRAMMATIC_API_KEY_FILE",
            "/data/data/com.termux/files/home/n8n-programmatic-api-key.txt",
        )
    )
    if key_file.exists():
        try:
            mode = stat.S_IMODE(os.stat(str(key_file)).st_mode)
            if mode & 0o077:
                log.warning(
                    "n8n-programmatic-api-key.txt has insecure permissions (mode %o) — "
                    "skipping file, falling back to env var only",
                    mode,
                )
            else:
                api_key = key_file.read_text().strip()
                if api_key:
                    return api_key
        except Exception:
            pass

    return None


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

    def execute_webhook(self, webhook_path: str, data: Dict[str, Any],
                        method: str = "POST", timeout: int = 60) -> Dict[str, Any]:
        """Execute a webhook by path. Returns the webhook response."""
        url = f"{self.base_url}/webhook/{webhook_path}"
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
        except requests.RequestException as e:
            log.error("Webhook %s %s failed: %s", method, webhook_path, e)
            raise

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
def run_backtest_webhook(data: Optional[Dict] = None) -> Dict[str, Any]:
    """Run the SuperBot backtest via n8n webhook."""
    return get_client().run_backtest(data)


def run_collect_webhook(data: Optional[Dict] = None) -> Dict[str, Any]:
    """Run the SuperBot data collection via n8n webhook."""
    return get_client().run_data_collect(data)


def run_trade_webhook(data: Optional[Dict] = None) -> Dict[str, Any]:
    """Run the SuperBot trade via n8n webhook."""
    return get_client().run_trade(data)


def run_monitor_webhook(data: Optional[Dict] = None) -> Dict[str, Any]:
    """Run the SuperBot monitor via n8n webhook."""
    return get_client().run_monitor(data)


def n8n_health_check() -> bool:
    """Verify n8n is reachable and the superbot-trade webhook responds.

    Returns False if the API key is missing, n8n is down, or the webhook
    errors — so startup can fail fast instead of mid-trade.
    """
    try:
        get_client().execute_webhook("superbot-trade", {"action": "health_check"})
        return True
    except Exception:
        return False