"""
n8n_client.py — Programmatic n8n API client for Kalshi SuperBot integration.

Uses the n8n API key (X-N8N-API-KEY header) to:
- List, create, update, delete workflows
- Execute workflows via webhook or API execution endpoint
- Get workflow status and executions
- Manage webhooks

N1 — durable signed bounded n8n outbox:
- `execute_webhook` writes events to the durable `event_outbox` table
  (one local SQLite insert, microseconds) and returns without any network I/O.
- `OutboxSender` (background thread) reads pending outbox rows and delivers
  to n8n with exponential backoff, jitter, and `Retry-After` honouring.
- Critical events (settlement, halt, fill) are never dropped; telemetry
  events (heartbeat, latency) use drop-oldest when the backlog exceeds a cap.
- Webhooks are signed with HMAC-SHA256 over `{timestamp_ms}.{nonce}.{body}`
  using `SUPERBOT_WEBHOOK_SECRET`, sent on a separate `requests.Session` that
  carries no `X-N8N-API-KEY`. The nonce gives replay cover; the receiver
  verifies it with n8n_webhook_auth.verify_request.

All API endpoints live under http://127.0.0.1:5678/api/v1/.
"""

import json
import logging
import os
import queue
import stat
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter

import config
import n8n_webhook_auth
from events import Event, make_event, delivery_class, CRITICAL_TYPES, TELEMETRY_TYPES

log = logging.getLogger("n8n_client")

# Default base URL — n8n runs locally on port 5678
DEFAULT_BASE_URL = os.environ.get("N8N_BASE_URL", "http://127.0.0.1:5678")

# Webhook timeout must be less than CYCLE_TIMEOUT_SECONDS so a webhook
# can't block a full cycle. Default: cycle timeout minus 5s margin.
DEFAULT_WEBHOOK_TIMEOUT = max(1, config.CYCLE_TIMEOUT_SECONDS - 5)

# N1 outbox limits.
MAX_TELEMETRY_QUEUE = config.N8N_OUTBOX_MAX_TELEMETRY
MAX_OUTBOX_ATTEMPTS = config.N8N_OUTBOX_MAX_ATTEMPTS

# In-memory queues for fast enqueue (the DB outbox is the source of truth).
# Critical: unbounded (never dropped). Telemetry: bounded (drop-oldest).
_webhook_queue: "queue.Queue[tuple]" = queue.Queue()
_telemetry_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=MAX_TELEMETRY_QUEUE)
_telemetry_dropped = 0
_telemetry_dropped_lock = threading.Lock()
_webhook_worker_started = False
_webhook_worker_lock = threading.Lock()
_outbox_sender_started = False
_outbox_sender_lock = threading.Lock()


def _telemetry_dropped_inc() -> None:
    global _telemetry_dropped
    with _telemetry_dropped_lock:
        _telemetry_dropped += 1


def _telemetry_dropped_get() -> int:
    with _telemetry_dropped_lock:
        return _telemetry_dropped


def _webhook_worker():
    """Background worker that processes the in-memory webhook queue.

    Each item is (client, webhook_path, data, method, timeout, max_retries).
    Delivery is best-effort; the durable outbox (OutboxSender) guarantees
    at-least-once delivery even if this thread misses an item.
    """
    while True:
        try:
            item = _webhook_queue.get(timeout=1.0)
            if item is None:  # Shutdown signal
                break
            client, webhook_path, data, method, timeout, max_retries = item
            try:
                client._execute_webhook_signed(webhook_path, data, method, timeout, max_retries)
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
            thread = threading.Thread(
                target=_webhook_worker, daemon=True, name="n8n-webhook-worker"
            )
            thread.start()
            _webhook_worker_started = True


def _outbox_sender(db=None):
    """Background sender that reads pending outbox rows from the DB and delivers.

    This is the durable delivery path: it survives restarts and n8n outages.
    Reads from event_outbox (pending, next_attempt_at <= now), delivers with
    HMAC signing, marks sent on success or bumps attempts/dead on failure.
    """
    if db is None:
        from local_db import LocalDB
        db = LocalDB()

    while True:
        try:
            rows = db.get_pending_outbox_events(limit=50)
            if not rows:
                time.sleep(1.0)
                continue
            for row in rows:
                _deliver_outbox_row(db, row)
        except Exception as e:  # pragma: no cover - defensive
            log.error("Outbox sender error: %s", e)
            time.sleep(2.0)


def _deliver_outbox_row(db, row: Dict[str, Any]) -> None:
    """Deliver a single outbox row to n8n with HMAC signing."""
    event_id = row["id"]
    webhook_path = row["type"]  # event type maps to webhook path
    payload = json.loads(row["payload_json"] or "{}")

    client = N8nClient()  # API methods not needed; only _execute_webhook_signed
    try:
        client._execute_webhook_signed(webhook_path, payload, "POST",
                                       DEFAULT_WEBHOOK_TIMEOUT, 1)
        db.mark_outbox_sent(event_id)
        log.debug("Outbox event %s delivered (%s)", event_id, row["type"])
    except Exception as e:
        log.warning("Outbox event %s failed (%s): %s", event_id, row["type"], e)
        db.mark_outbox_dead(event_id, str(e)[:200], max_attempts=MAX_OUTBOX_ATTEMPTS)


def _sign_body(timestamp: str, body: str, nonce: str = "") -> str:
    """HMAC-SHA256 over ``{timestamp}.{nonce}.{body}`` (N1: nonce replay cover).

    Kept for backward compatibility with callers that pass only
    (timestamp, body): an empty nonce signs ``{timestamp}..{body}``.
    """
    return n8n_webhook_auth.sign_body(timestamp, nonce, body)


def _start_outbox_sender():
    """Start the outbox sender thread."""
    global _outbox_sender_started
    with _outbox_sender_lock:
        if not _outbox_sender_started:
            thread = threading.Thread(
                target=_outbox_sender, daemon=True, name="n8n-outbox-sender"
            )
            thread.start()
            _outbox_sender_started = True


_N8N_TOKEN_KEY_NAMES = {"n8n_local_access_token", "n8n_api_key", "n8n_access_token",
                         "n8n_api_token", "api_key", "token", "n8n_key"}


def _read_api_key_file(key_file: Path) -> Optional[str]:
    """Read an n8n API key from a protected file."""
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

    key_file = Path(
        os.environ.get(
            "N8N_API_KEY_FILE",
            os.path.expanduser("~/.secrets/n8n_api_key"),
        )
    )
    api_key = _read_api_key_file(key_file)
    if api_key:
        return api_key

    key_file = Path(
        os.environ.get(
            "N8N_PROGRAMMATIC_API_KEY_FILE",
            os.path.expanduser("~/n8n-programmatic-api-key.txt"),
        )
    )
    return _read_api_key_file(key_file)


class N8nClient:
    """Client for n8n REST API and signed webhook delivery."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or load_api_key()

        # Session for authenticated API calls (workflows, executions).
        # Carries X-N8N-API-KEY.
        self.session = requests.Session()
        self.session.headers.update({
            "X-N8N-API-KEY": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.session.mount("http://", HTTPAdapter(pool_connections=10, pool_maxsize=10))
        self.session.mount("https://", HTTPAdapter(pool_connections=10, pool_maxsize=10))

        # Separate session for webhooks: NO API key, HMAC signing instead.
        self._webhook_session = requests.Session()
        self._webhook_session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._webhook_session.mount("http://", HTTPAdapter(pool_connections=10, pool_maxsize=10))
        self._webhook_session.mount("https://", HTTPAdapter(pool_connections=10, pool_maxsize=10))

        if not self.api_key:
            log.warning("No n8n API key found — API calls will fail")

    # ── REST API (unchanged) ──────────────────────────────────────────

    def _request(self, method: str, path: str, data: Optional[Dict] = None,
                 params: Optional[Dict] = None, timeout: int = 30,
                 max_retries: int = 3) -> Dict[str, Any]:
        """Make an authenticated request to the n8n API."""
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

    def list_workflows(self, limit: int = 50, skip: int = 0) -> Dict[str, Any]:
        return self._request("GET", "workflows", params={"limit": limit, "skip": skip})

    def get_workflow(self, workflow_id: str) -> Dict[str, Any]:
        return self._request("GET", f"workflows/{workflow_id}")

    def create_workflow(self, workflow_data: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "workflows", data=workflow_data)

    def update_workflow(self, workflow_id: str, workflow_data: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", f"workflows/{workflow_id}", data=workflow_data)

    def delete_workflow(self, workflow_id: str) -> Dict[str, Any]:
        return self._request("DELETE", f"workflows/{workflow_id}")

    def activate_workflow(self, workflow_id: str) -> Dict[str, Any]:
        return self._request("POST", f"workflows/{workflow_id}/activate")

    def deactivate_workflow(self, workflow_id: str) -> Dict[str, Any]:
        return self._request("POST", f"workflows/{workflow_id}/deactivate")

    def list_executions(self, workflow_id: Optional[str] = None,
                        limit: int = 20, skip: int = 0) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "skip": skip}
        if workflow_id:
            params["workflowId"] = workflow_id
        return self._request("GET", "executions", params=params)

    def get_execution(self, execution_id: str) -> Dict[str, Any]:
        return self._request("GET", f"executions/{execution_id}")

    # ── Webhooks ──────────────────────────────────────────────────────

    def _execute_webhook_sync(self, webhook_path: str, data: Dict[str, Any],
                              method: str = "POST", timeout: int = DEFAULT_WEBHOOK_TIMEOUT,
                              max_retries: int = 3) -> Dict[str, Any]:
        """Execute a webhook synchronously (no signing, API key session)."""
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

    def _execute_webhook_signed(self, webhook_path: str, data: Dict[str, Any],
                                method: str = "POST", timeout: int = DEFAULT_WEBHOOK_TIMEOUT,
                                max_retries: int = 3,
                                extra_headers: Optional[Dict[str, str]] = None
                                ) -> Dict[str, Any]:
        """Execute a webhook with HMAC signing on the no-API-key session.

        Signature covers ``{timestamp_ms}.{nonce}.{body}`` (N1: nonce replay
        cover); the static shared secret rides in X-Superbot-Key as the
        n8n webhook headerAuth possession gate.
        """
        timestamp = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        # ensure_ascii=False: byte-equal to JS JSON.stringify for all content
        # except U+2028/U+2029 (n8n escapes those); keep both sides identical.
        body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        signature = n8n_webhook_auth.sign_body(timestamp, nonce, body)
        headers = {
            "X-Superbot-Timestamp": timestamp,
            "X-Superbot-Nonce": nonce,
            "X-Superbot-Signature": signature,
            "X-Superbot-Key": n8n_webhook_auth.static_key(),
        }
        if extra_headers:
            headers.update(extra_headers)

        url = f"{self.base_url}/webhook/{webhook_path}"
        last_err = None
        for attempt in range(max_retries):
            try:
                # data= must be bytes: a str makes requests set Content-Length
                # from the character count while writing UTF-8 bytes, which
                # truncates any non-ASCII body on the receiver side.
                resp = self._webhook_session.request(
                    method, url, data=body.encode("utf-8"), headers=headers, timeout=timeout
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
                        "Signed webhook %s %s: connection/timeout error — retry %d/%d: %s",
                        method, webhook_path, attempt + 1, max_retries, e,
                    )
                    time.sleep(2.0 * (attempt + 1))
                    continue
                log.error("Signed webhook %s %s: connection/timeout error — exhausted %d retries: %s",
                          method, webhook_path, max_retries, e)
                raise
            except requests.RequestException as e:
                last_err = e
                if hasattr(e, 'response') and e.response is not None:
                    status = e.response.status_code
                    if 500 <= status < 600:
                        if attempt < max_retries - 1:
                            log.warning(
                                "Signed webhook %s %s: HTTP %s — retry %d/%d",
                                method, webhook_path, status, attempt + 1, max_retries,
                            )
                            time.sleep(2.0 * (attempt + 1))
                            continue
                        log.error(
                            "Signed webhook %s %s: HTTP %s — exhausted %d retries",
                            method, webhook_path, status, max_retries,
                        )
                log.error("Signed webhook %s %s failed: %s", method, webhook_path, e)
                raise
        raise last_err

    def execute_webhook(self, webhook_path: str, data: Dict[str, Any],
                        method: str = "POST", timeout: int = DEFAULT_WEBHOOK_TIMEOUT,
                        max_retries: int = 3, fire_and_forget: bool = True
                        ) -> Dict[str, Any]:
        """Execute a webhook by path (N1: durable outbox + bounded queues).

        N1 behavior:
        - Writes the event to the durable event_outbox table (one local SQLite
          insert, microseconds) and returns immediately — no network I/O.
        - Critical events (settlement, halt, fill) go to the unbounded queue;
          telemetry events go to a bounded queue with drop-oldest.
        - The OutboxSender thread reads from the DB and delivers with signing.

        Set fire_and_forget=False for synchronous execution (health checks).
        """
        if fire_and_forget:
            # N1: write to durable outbox first (source of truth).
            venue = data.get("venue", "kalshi")
            mode = data.get("mode", "dry" if config.DRY_RUN else "live")
            event_type = data.get("event_type", webhook_path.replace("-", "."))
            cls = delivery_class(event_type)
            event_id = str(uuid.uuid4())
            try:
                from local_db import LocalDB
                db = LocalDB()
                db.insert_outbox_event(
                    event_type=event_type,
                    payload_json=json.dumps(data),
                    delivery_class=cls,
                    venue=venue,
                    mode=mode,
                    event_id=event_id,
                )
            except Exception as e:
                log.warning("N1: outbox insert failed for %s: %s", webhook_path, e)

            # Enqueue for fast in-process delivery (best-effort; DB is durable).
            _start_outbox_sender()
            if cls == "critical":
                _webhook_queue.put((self, webhook_path, data, method, timeout, max_retries))
            else:
                # Telemetry: bounded, drop-oldest when full.
                try:
                    _telemetry_queue.put_nowait(
                        (self, webhook_path, data, method, timeout, max_retries)
                    )
                except queue.Full:
                    _telemetry_dropped_inc()
                    log.debug("N1: telemetry queue full, dropped oldest")

            return {}
        else:
            return self._execute_webhook_sync(webhook_path, data, method, timeout, max_retries)

    # ── SuperBot convenience methods ──────────────────────────────────

    def run_backtest(self, data: Optional[Dict] = None) -> Dict[str, Any]:
        return self.execute_webhook("superbot-backtest", data or {})

    def run_data_collect(self, data: Optional[Dict] = None) -> Dict[str, Any]:
        return self.execute_webhook("superbot-collect", data or {})

    def run_trade(self, data: Optional[Dict] = None) -> Dict[str, Any]:
        return self.execute_webhook("superbot-trade", data or {})

    def run_monitor(self, data: Optional[Dict] = None) -> Dict[str, Any]:
        return self.execute_webhook("superbot-monitor", data or {})

    def get_workflow_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        workflows = self.list_workflows(limit=100)
        for w in workflows.get("data", []):
            if w.get("name") == name:
                return w
        return None

    def ensure_workflow_active(self, workflow_id: str) -> Dict[str, Any]:
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


def n8n_health_check() -> bool:
    """Verify n8n is reachable via a read-only GET."""
    try:
        client = get_client()
        resp = client.session.get(f"{client.base_url}/", timeout=5)
        return resp.status_code < 500
    except Exception:
        return False


def n8n_startup_check() -> bool:
    """N1 startup gate: warn on non-loopback bind or unsigned acceptance.

    - WARNING when n8n appears bound to a non-loopback address
      (N8N_HOST / N8N_LISTEN_ADDRESS / configured base URL host).
    - CRITICAL when an unsigned webhook request is accepted: the superbot-*
      webhooks must reject requests without the shared secret.
    Returns True when no issue was found.
    """
    from urllib.parse import urlparse
    issues = []
    bind = (os.environ.get("N8N_HOST") or os.environ.get("N8N_LISTEN_ADDRESS") or "").strip()
    if not bind:
        try:
            bind = urlparse(DEFAULT_BASE_URL).hostname or ""
        except Exception:
            bind = ""
    if bind and bind not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        log.warning("n8n appears bound to non-loopback address %r — the SuperBot "
                    "webhooks are meant for 127.0.0.1 only; rebind or tunnel", bind)
        issues.append("non-loopback-bind")
    # Probe: an unsigned request must NOT be accepted by the superbot webhooks.
    try:
        client = get_client()
        resp = client.session.post(
            f"{client.base_url}/webhook/superbot-monitor",
            json={"probe": "unsigned"}, timeout=5)
        if resp.status_code < 400:
            log.critical("n8n accepted an UNSIGNED webhook request (status %s) — "
                         "the superbot-* webhooks are not authenticated; any local "
                         "process can post forged events to them", resp.status_code)
            issues.append("unsigned-accepted")
    except Exception as e:
        log.debug("unsigned probe could not reach n8n (offline?): %s", e)
    return not issues