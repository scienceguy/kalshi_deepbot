"""
n8n_client.py — Programmatic n8n API client for Kalshi SuperBot integration.

Uses the n8n API key (X-N8N-API-KEY header) to:
- List, create, update, delete workflows
- Execute workflows via webhook or API execution endpoint
- Get workflow status and executions
- Manage webhooks

N2 — persistent outbox, three lanes, bounded queues, circuit breaker:
- `execute_webhook` writes critical/normal events to the durable
  `event_outbox` table (one local SQLite insert, microseconds) and returns
  without any network I/O. Telemetry is coalesced in memory (latest state
  per key) and never touches the DB.
- Two `OutboxSender` threads (background): one reserved for `critical`, one
  for `normal` + telemetry. They read pending outbox rows and deliver with
  exponential backoff, jitter, and a circuit breaker so an n8n outage does
  not spin.
- Lanes: critical (halts, ruin, auth failures, reconciliation mismatches),
  normal (orders, fills, settlements), telemetry (heartbeats, metrics).
  Telemetry drops oldest when the backlog exceeds a cap and counts drops.
  Per-lane attempt timeouts are always bounded (telemetry connect 1s /
  read 3s; critical bounded at 10s).
- `outbox_metrics()` exports outbox_depth, oldest_undelivered_age_s and
  dropped_total for the periodic state report and `--status`.
- Webhooks are signed with HMAC-SHA256 over `{timestamp_ms}.{nonce}.{body}`
  using `SUPERBOT_WEBHOOK_SECRET`, sent on a separate `requests.Session` that
  carries no `X-N8N-API-KEY`. The nonce gives replay cover; the receiver
  verifies it with n8n_webhook_auth.verify_request.

All API endpoints live under http://127.0.0.1:5678/api/v1/.
"""

import json
import logging
import os
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

import config
import n8n_webhook_auth
import rate_limit
from events import delivery_class

log = logging.getLogger("n8n_client")

# Default base URL — n8n runs locally on port 5678
DEFAULT_BASE_URL = os.environ.get("N8N_BASE_URL", "http://127.0.0.1:5678")

# Webhook timeout must be less than CYCLE_TIMEOUT_SECONDS so a webhook
# can't block a full cycle. Default: cycle timeout minus 5s margin.
DEFAULT_WEBHOOK_TIMEOUT = max(1, config.CYCLE_TIMEOUT_SECONDS - 5)

# N2 outbox limits.
MAX_TELEMETRY_QUEUE = config.N8N_OUTBOX_MAX_TELEMETRY
MAX_OUTBOX_ATTEMPTS = config.N8N_OUTBOX_MAX_ATTEMPTS

# Per-lane per-attempt timeouts (connect, read) in seconds. Always bounded.
CRITICAL_WEBHOOK_TIMEOUT = (1.0, 10.0)
NORMAL_WEBHOOK_TIMEOUT = (1.0, 8.0)
TELEMETRY_WEBHOOK_TIMEOUT = (1.0, 3.0)
_LANE_TIMEOUTS = {"critical": CRITICAL_WEBHOOK_TIMEOUT,
                  "normal": NORMAL_WEBHOOK_TIMEOUT,
                  "telemetry": TELEMETRY_WEBHOOK_TIMEOUT}

class _CircuitBreaker:
    """Consecutive-failure breaker so an n8n outage does not spin the senders.

    Opens after `threshold` consecutive delivery failures and stays open for
    `cooldown_s`; while open the senders sleep instead of attempting, and the
    pending rows stay pending (nothing is dropped or dead-lettered). One
    success closes it again.
    """

    def __init__(self, threshold: int = 20, cooldown_s: float = 60.0):
        self.threshold = int(threshold)
        self.cooldown_s = float(cooldown_s)
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at and (time.time() - self._opened_at) < self.cooldown_s:
                return False
            return True

    @property
    def is_open(self) -> bool:
        return not self.allow()

    def record_success(self) -> None:
        with self._lock:
            was_open = bool(self._opened_at)
            self._consecutive_failures = 0
            self._opened_at = 0.0
        if was_open:
            self._emit_breaker_event("breaker.clear")

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.threshold:
                if not self._opened_at or (time.time() - self._opened_at) >= self.cooldown_s:
                    log.warning(
                        "n8n circuit breaker OPEN after %d consecutive failures — "
                        "pausing outbox delivery for %.0fs (rows stay pending)",
                        self._consecutive_failures, self.cooldown_s,
                    )
                    just_opened = not self._opened_at
                else:
                    just_opened = False
                self._opened_at = time.time()
            else:
                just_opened = False
        if just_opened:
            self._emit_breaker_event("breaker.trip")

    def _emit_breaker_event(self, event_type: str) -> None:
        # Critical lane: routed via the durable outbox, so this never fails
        # the sender thread that triggered it.
        try:
            from emit import emit_event
            emit_event("n8n_outbox", event_type,
                       {"reason": "consecutive delivery failures" if "trip" in event_type
                        else "delivery recovered"},
                       key=f"{event_type}:{int(time.time()) // 60}",
                       mode="dry" if config.DRY_RUN else "live")
        except Exception as e:
            log.debug("breaker event emit failed (non-fatal): %s", e)

_outbox_breaker = _CircuitBreaker()

# Telemetry is process-local and coalesced: latest state per
# (webhook_path|venue) wins; the map is bounded by MAX_TELEMETRY_QUEUE with
# drop-oldest eviction, and every eviction is counted in dropped_total.
_telemetry_pending: Dict[str, Tuple["N8nClient", str, Dict[str, Any], str]] = {}
_telemetry_pending_lock = threading.Lock()
_telemetry_dropped = 0
_telemetry_dropped_lock = threading.Lock()
_outbox_sender_started = False
_outbox_sender_lock = threading.Lock()

def _telemetry_dropped_inc() -> None:
    global _telemetry_dropped
    with _telemetry_dropped_lock:
        _telemetry_dropped += 1

def _telemetry_dropped_get() -> int:
    with _telemetry_dropped_lock:
        return _telemetry_dropped

def _enqueue_telemetry(key: str, item: Tuple["N8nClient", str, Dict[str, Any], str]) -> None:
    """Coalesce telemetry by key (latest wins); bounded; drop-oldest on overflow."""
    with _telemetry_pending_lock:
        if key in _telemetry_pending:
            _telemetry_pending[key] = item  # latest state wins
            return
        if len(_telemetry_pending) >= MAX_TELEMETRY_QUEUE:
            oldest = next(iter(_telemetry_pending))  # dict preserves insertion order
            _telemetry_pending.pop(oldest)
            _telemetry_dropped_inc()
        _telemetry_pending[key] = item

def _drain_telemetry() -> int:
    """Fire every pending telemetry item once with the short lane timeout."""
    with _telemetry_pending_lock:
        items = list(_telemetry_pending.values())
        _telemetry_pending.clear()
    for client, webhook_path, data, method in items:
        try:
            client._execute_webhook_signed(
                webhook_path, data, method, TELEMETRY_WEBHOOK_TIMEOUT, 1
            )
        except Exception as e:
            log.debug("Telemetry %s failed (dropped): %s", webhook_path, e)
    return len(items)

def _outbox_sender(db=None, lane: str = "normal"):
    """Background sender that reads pending outbox rows for one N2 lane.

    This is the durable delivery path: it survives restarts and n8n outages.
    Reads from event_outbox (pending, next_attempt_at <= now), delivers with
    HMAC signing, marks sent on success or bumps attempts/dead on failure.
    The critical lane gets its own reserved sender; the normal sender also
    drains coalesced telemetry.
    """
    if db is None:
        from local_db import get_local_db
        db = get_local_db()

    while True:
        try:
            if not _outbox_breaker.allow():
                time.sleep(5.0)
                continue
            rows = db.claim_pending_outbox_events(limit=50, lane=lane)
            if not rows:
                if lane == "normal":
                    _drain_telemetry()
                time.sleep(1.0)
                continue
            for row in rows:
                if not _outbox_breaker.allow():
                    break
                _deliver_outbox_row(db, row)
        except Exception as e:  # pragma: no cover - defensive
            log.error("Outbox sender error: %s", e)
            time.sleep(2.0)

def _lane_timeout(delivery_class: str) -> Tuple[float, float]:
    return _LANE_TIMEOUTS.get(delivery_class or "normal", NORMAL_WEBHOOK_TIMEOUT)

def _deliver_outbox_row(db, row: Dict[str, Any]) -> None:
    """Deliver a single outbox row to n8n with HMAC signing."""
    event_id = row["id"]
    # N3: the row stores the webhook path it was enqueued for; fall back to
    # the event type for rows written before webhook_path existed.
    webhook_path = row.get("webhook_path") or row["type"]
    payload = json.loads(row["payload_json"] or "{}")
    lane = row.get("delivery_class") or "normal"

    client = get_client()  # I7: reuse one signing session, not one per delivery
    try:
        client._execute_webhook_signed(webhook_path, payload, "POST",
                                       _lane_timeout(lane), 1)
        db.mark_outbox_sent(event_id)
        _outbox_breaker.record_success()
        log.debug("Outbox event %s delivered (%s)", event_id, row["type"])
    except Exception as e:
        log.warning("Outbox event %s failed (%s): %s", event_id, row["type"], e)
        db.mark_outbox_dead(event_id, str(e)[:200], max_attempts=MAX_OUTBOX_ATTEMPTS)
        _outbox_breaker.record_failure()

def _write_outbox_sidecar(event_id: str, event_type: str,
                          payload: Dict[str, Any], webhook_path: str,
                          error: str) -> None:
    """I4: last-resort record when a critical event cannot be delivered at all.

    The local DB is the normal durable store; this only runs when that insert
    failed and the synchronous fallback also failed, so the event is not lost
    silently. Operator replay is manual — one file per failed event.
    Also emits an alert event so the operator is notified.
    """
    try:
        d = Path(config.SUPERBOT_DATA_DIR) / "outbox_failed"
        d.mkdir(parents=True, exist_ok=True)
        rec = {"event_id": event_id, "type": event_type,
               "webhook_path": webhook_path, "payload": payload, "error": error,
               "ts": datetime.now(timezone.utc).isoformat()}
        (d / f"{event_id}.json").write_text(json.dumps(rec))
    except Exception as e:
        log.error("Could not write outbox sidecar for %s: %s", event_id, e)
    # Emit alert so operator sees it in digest
    try:
        import emit
        emit.emit_event(
            "kalshi_trader", "outbox.failed",
            {"event_id": event_id, "type": event_type,
             "webhook_path": webhook_path, "error": error[:200]},
            key=f"outbox.failed:{event_id}")
    except Exception:
        pass

def _sign_body(timestamp: str, body: str, nonce: str = "") -> str:
    """HMAC-SHA256 over ``{timestamp}.{nonce}.{body}`` (N1: nonce replay cover).

    Kept for backward compatibility with callers that pass only
    (timestamp, body): an empty nonce signs ``{timestamp}..{body}``.
    """
    return n8n_webhook_auth.sign_body(timestamp, nonce, body)

def _start_outbox_sender():
    """Start the two background outbox senders (critical + normal/telemetry)."""
    global _outbox_sender_started
    with _outbox_sender_lock:
        if not _outbox_sender_started:
            threading.Thread(
                target=_outbox_sender, daemon=True,
                name="n8n-outbox-critical",
                kwargs={"lane": "critical"},
            ).start()
            threading.Thread(
                target=_outbox_sender, daemon=True,
                name="n8n-outbox-normal",
                kwargs={"lane": "normal"},
            ).start()
            _outbox_sender_started = True

def drain_outbox(timeout_seconds: float = 10.0) -> int:
    """Drain pending outbox events synchronously (for graceful shutdown).
    
    Delivers all pending critical/normal events up to timeout_seconds.
    Returns number of events delivered.
    """
    from local_db import get_local_db
    db = get_local_db()
    delivered = 0
    start = time.time()
    for lane in ("critical", "normal"):
        while time.time() - start < timeout_seconds:
            rows = db.claim_pending_outbox_events(limit=50, lane=lane)
            if not rows:
                break
            for row in rows:
                if time.time() - start >= timeout_seconds:
                    break
                try:
                    _deliver_outbox_row(db, row)
                    delivered += 1
                except Exception as e:
                    log.warning("Outbox drain failed for %s: %s", row.get("type"), e)
            if lane == "normal":
                # Also try to drain telemetry
                _drain_telemetry()
    return delivered

def outbox_metrics() -> Dict[str, Any]:
    """N2 export: outbox_depth, oldest_undelivered_age_s, dropped_total.

    Best-effort: a local-DB hiccup returns the in-memory counters only.
    """
    metrics: Dict[str, Any] = {
        "outbox_depth": 0,
        "oldest_undelivered_age_s": 0.0,
        "dropped_total": _telemetry_dropped_get(),
    }
    try:
        from local_db import get_local_db
        metrics.update(get_local_db().get_outbox_depth_stats())
    except Exception as e:
        log.debug("outbox_metrics: local db unavailable: %s", e)
    return metrics

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

def _assert_transport_safe(base_url: str) -> None:
    """H2: refuse plaintext to a non-loopback host.

    The gate key and every HMAC signature ride in headers, so a plaintext hop
    to another machine hands both to anyone on the path. `https://` is allowed
    from anywhere (warned when not loopback, so a tunnel stays visible in the
    logs); loopback `http://` is the normal local case.
    """
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    loopback = (host in ("localhost", "::1", "0.0.0.0")
                or host.startswith("127."))
    if parsed.scheme == "http" and not loopback:
        raise ValueError(
            f"refusing plaintext http:// to non-loopback host {host!r}: "
            "use https:// or a loopback address")
    if parsed.scheme == "https" and not loopback:
        log.warning("n8n base URL %s is not loopback — gate key and HMAC "
                    "signatures cross the network (TLS only)", host)

class N8nClient:
    """Client for n8n REST API and signed webhook delivery."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, api_key: Optional[str] = None):
        _assert_transport_safe(base_url)
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

    # ── Webhooks ──────────────────────────────────────────────────────

    def _execute_webhook_sync(self, webhook_path: str, data: Dict[str, Any],
                              method: str = "POST", timeout: int = DEFAULT_WEBHOOK_TIMEOUT,
                              max_retries: int = 3) -> Dict[str, Any]:
        """Execute a webhook synchronously (no signing, API key session)."""
        # M3: acquire rate limit token for n8n webhook endpoints
        rate_limit.default_limiter().acquire("n8n_webhook")
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
                    elif status == 429:
                        # M3: record rejection for backoff
                        rate_limit.default_limiter().record_rejection("n8n_webhook")
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
        cover); the gate key rides in X-Superbot-Key as the n8n webhook
        headerAuth possession gate. H2: the gate key is never the HMAC key.
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
            "X-Superbot-Key": n8n_webhook_auth.gate_key(),
        }
        if extra_headers:
            headers.update(extra_headers)

        # M3: acquire rate limit token for n8n webhook endpoints
        rate_limit.default_limiter().acquire("n8n_webhook")
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
                    elif status == 429:
                        # M3: record rejection for backoff
                        rate_limit.default_limiter().record_rejection("n8n_webhook")
                log.error("Signed webhook %s %s failed: %s", method, webhook_path, e)
                raise
        raise last_err

    def execute_webhook(self, webhook_path: str, data: Dict[str, Any],
                        method: str = "POST", timeout: int = DEFAULT_WEBHOOK_TIMEOUT,
                        max_retries: int = 3, fire_and_forget: bool = True
                        ) -> Dict[str, Any]:
        """Execute a webhook by path (N2: durable outbox, three lanes).

        N2 behavior:
        - critical/normal events are written to the durable `event_outbox`
          table (one local SQLite insert, microseconds) and returned from
          immediately — no network I/O in the trading thread.
        - Two senders deliver from the DB: one reserved for `critical`, one
          for `normal` (+ coalesced telemetry), with per-lane bounded
          timeouts, exponential backoff with jitter, and a circuit breaker
          so an n8n outage does not spin.
        - Telemetry events (heartbeat, metrics) never touch the DB: they are
          coalesced in memory (latest state per key), bounded, drop-oldest.

        Set fire_and_forget=False for synchronous execution (health checks).
        """
        if fire_and_forget:
            venue = data.get("venue", "kalshi")
            mode = data.get("mode", "dry" if config.DRY_RUN else "live")
            # N3: a versioned envelope carries its own type; legacy callers
            # fall back to the webhook path.
            event_type = (data.get("type") or data.get("event_type")
                          or webhook_path.replace("-", "."))
            cls = delivery_class(event_type)
            _start_outbox_sender()
            if cls == "telemetry":
                # Coalesce: latest state per key wins; bounded; drops counted.
                key = f"{webhook_path}|{venue}"
                _enqueue_telemetry(key, (self, webhook_path, data, method))
                return {}
            event_id = str(uuid.uuid4())
            try:
                from local_db import get_local_db
                db = get_local_db()
                rowid = db.insert_outbox_event(
                    event_type=event_type,
                    payload_json=json.dumps(data),
                    delivery_class=cls,
                    venue=venue,
                    mode=mode,
                    event_id=event_id,
                    webhook_path=webhook_path,
                )
                if rowid is None:
                    raise RuntimeError("insert_outbox_event returned None")
            except Exception as e:
                log.warning("N2: outbox insert failed for %s: %s", webhook_path, e)
                if cls == "critical":
                    # I4: a critical event must not be dropped because the local
                    # DB is unavailable. Deliver synchronously; only if that
                    # fails too, leave a sidecar for manual replay.
                    try:
                        self._execute_webhook_sync(webhook_path, data, method,
                                                   timeout, max_retries)
                    except Exception as e2:
                        log.error("I4: synchronous critical delivery failed for "
                                  "%s: %s — writing sidecar", webhook_path, e2)
                        _write_outbox_sidecar(event_id, event_type, data,
                                              webhook_path, str(e2)[:200])
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
    """Verify n8n is reachable via the monitor webhook (ack-only sink).

    The superbot-monitor webhook is an ack-only sink (no side effects),
    so health checks don't contaminate the trade webhook metrics/logs.
    """
    try:
        client = get_client()
        # Use the monitor webhook for health checks - it's ack-only by design.
        # Use fire_and_forget=False for synchronous execution to get actual response.
        _ = client.execute_webhook("superbot-monitor", {"action": "health_check"}, fire_and_forget=False)
        # If we get here without exception, the webhook succeeded
        return True
    except Exception:
        return False

def _execute_command_enabled() -> bool:
    """H5: True when n8n's environment re-enables the Execute Command node.

    n8n 2.x excludes it by default, so unset/absent means disabled. Setting
    NODES_EXCLUDE to a list that omits it, or NODES_INCLUDE to name it, turns
    arbitrary shell back on for anyone with editor or API access.
    """
    if "executeCommand" in (os.environ.get("NODES_INCLUDE") or ""):
        return True
    raw = os.environ.get("NODES_EXCLUDE")
    if raw is None:
        return False
    try:
        excluded = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(excluded, list):
        return False
    return "n8n-nodes-base.executeCommand" not in excluded

def n8n_startup_check() -> bool:
    """N1 startup gate: warn on non-loopback bind or unsigned acceptance.

    - WARNING when n8n appears bound to a non-loopback address
      (N8N_HOST / N8N_LISTEN_ADDRESS / configured base URL host).
    - ERROR when the Execute Command node is re-enabled (H5): n8n must never
      be able to run shell, scripts go through the loopback job bridge.
    - CRITICAL when an unsigned webhook request is accepted: the superbot-*
      webhooks must reject requests without the shared secret.
    Returns True when no issue was found.
    """
    from urllib.parse import urlparse
    issues = []
    if _execute_command_enabled():
        log.error("n8n has the Execute Command node ENABLED (NODES_EXCLUDE/"
                  "NODES_INCLUDE) — anyone with editor or API access can run "
                  "shell as the n8n user; use scripts/job_bridge.py instead")
        issues.append("execute-command-enabled")
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