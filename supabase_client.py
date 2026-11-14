"""
supabase_client.py — Full Supabase integration for Kalshi SuperBot.

Writes signals, orders, bot state, and backtest results to Supabase.
Uses the Supabase REST API with service role key. Gracefully degrades
to no-op if credentials are missing (dry run mode).

Tables expected (create via Supabase dashboard):
  signals: id, ts, type, ticker, side, price_cents, edge, model_prob,
           contracts, reason, executed, blocked_reason, outcome_won,
           outcome_pnl, settled_at
  orders: id, ts, ticker, side, contracts, price_cents, order_id,
          status, fees_dollars, cycles, settled_at
  bot_state: id, ts, key, value_json
  backtest_runs: id, ts, params_json, ev_per_trade, total_pnl, n_trades,
                 win_rate, max_drawdown, final_balance
"""
import os
import json
import logging
import stat
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import requests
from requests.adapters import HTTPAdapter

import config  # noqa: F401  — import loads .env into os.environ

log = logging.getLogger(__name__)

_schema_warned = False  # log the missing-schema hint once, not on every write

# ── Circuit breaker for Supabase outages ──
# Trips after N consecutive failures, skips writes for a cooldown period.
_SUPABASE_FAILURES: int = 0
_SUPABASE_SKIP_UNTIL: float = 0.0
_SUPABASE_CIRCUIT_THRESHOLD = 5
_SUPABASE_CIRCUIT_COOLDOWN = 60.0  # seconds


def _supabase_circuit_open() -> bool:
    """Return True if Supabase writes are currently skipped due to outage."""
    # Both names must be global: without the _SUPABASE_FAILURES declaration the
    # reset below created a local, so the module counter stayed >= threshold and
    # a single later failure re-opened the circuit (crippling the audit trail).
    global _SUPABASE_SKIP_UNTIL, _SUPABASE_FAILURES
    if _SUPABASE_SKIP_UNTIL <= 0:
        return False
    if time.time() >= _SUPABASE_SKIP_UNTIL:
        _SUPABASE_SKIP_UNTIL = 0.0
        _SUPABASE_FAILURES = 0
        return False
    return True


def _supabase_record_failure() -> None:
    """Increment failure count; open circuit if threshold reached."""
    global _SUPABASE_FAILURES, _SUPABASE_SKIP_UNTIL
    _SUPABASE_FAILURES += 1
    if _SUPABASE_FAILURES >= _SUPABASE_CIRCUIT_THRESHOLD:
        _SUPABASE_SKIP_UNTIL = time.time() + _SUPABASE_CIRCUIT_COOLDOWN
        log.warning(
            "Supabase circuit opened — skipping writes for %.0fs after %d failures",
            _SUPABASE_CIRCUIT_COOLDOWN, _SUPABASE_FAILURES,
        )


def _supabase_record_success() -> None:
    """Reset failure count on a successful call."""
    global _SUPABASE_FAILURES, _SUPABASE_SKIP_UNTIL, _schema_warned
    _SUPABASE_FAILURES = 0
    _SUPABASE_SKIP_UNTIL = 0.0
    _schema_warned = False  # re-enable schema-missing warning after success


# ── Load credentials (lazy, at call time) ─────────────────────────────────────
# Credential loading order (highest priority first):
# 1. Protected key file (0600) — highest priority
# 2. Environment variable
# 3. .env (loaded by config.py at import)

SUPABASE_KEY_FILE = os.environ.get("SUPABASE_KEY_FILE", "")
SUPABASE_SERVICE_KEY_FILE = os.environ.get("SUPABASE_SERVICE_KEY_FILE", "")

# Valid key names recognised in key=value files (order: first match wins)
_VALID_KEY_NAMES = {"SUPABASE_SERVICE_KEY", "SUPABASE_API_KEY", "sb_secret"}


def _parse_kv_file(path: str) -> tuple:
    """Parse a KEY=VALUE file. Only explicit key=value lines are accepted;
    bare lines are ignored (no ambiguous >20-char fallback)."""
    url = ""
    key = ""
    try:
        with open(path) as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if not k or not v:
                    continue
                if k == "SUPABASE_URL":
                    url = url or v
                elif k in _VALID_KEY_NAMES:
                    key = key or v
    except OSError as e:
        log.warning("Could not read Supabase credential file %s: %s", path, e)
    return url, key


def _parse_raw_key_file(path: str) -> str:
    """Read a raw service key from a protected file.

    Two shapes are accepted (mode must be 0600 or stricter):
      1. KEY=VALUE file whose key is one of _VALID_KEY_NAMES (e.g. sb_secret)
         — the value of the first matching line is returned.
      2. A bare key on a single line.
    Multi-line KEY=VALUE files previously fell through to branch 2 and the
    ENTIRE file (including "KEY=" prefixes and newlines) was sent as the
    apikey, so every Supabase call failed auth.
    """
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            log.warning(
                "Supabase key file %s has insecure permissions (mode %o) — "
                "refusing to read. Set chmod 600.",
                path, mode,
            )
            return ""
        with open(path) as f:
            content = f.read()
        # KEY=VALUE shape: return the value of the first recognised key line.
        if "=" in content:
            _, kv_key = _parse_kv_file(path)
            if kv_key:
                log.info("Supabase service key parsed from KEY=VALUE file %s", path)
                return kv_key
            return ""  # KEY=VALUE file without a recognisable key — refuse to guess
        return content.strip()
    except OSError as e:
        log.warning("Could not read Supabase key file %s: %s", path, e)
        return ""


def _load_credentials() -> tuple[str, str]:
    """Load Supabase credentials from env/file at call time.
    
    Returns (url, service_key). Called lazily by SupabaseClient.__init__.
    """
    # Load URL: key file first (highest priority), then env
    url = ""
    if SUPABASE_KEY_FILE:
        file_url, _ = _parse_kv_file(SUPABASE_KEY_FILE)
        if file_url:
            url = file_url
            log.info("Supabase URL loaded from key file: %s", SUPABASE_KEY_FILE)
    if not url:
        url = os.environ.get("SUPABASE_URL", "")
        if url:
            log.debug("Supabase URL loaded from environment variable")

    # Load service key: protected raw file first (highest priority), then env.
    # Honor SUPABASE_SERVICE_KEY_FILE (dedicated service key) alongside the
    # generic SUPABASE_KEY_FILE, then fall back to the env var.
    service_key = ""
    for key_file in (SUPABASE_SERVICE_KEY_FILE, SUPABASE_KEY_FILE):
        if not key_file:
            continue
        raw_key = _parse_raw_key_file(key_file)
        if raw_key:
            service_key = raw_key
            log.info("Supabase service key loaded from key file: %s", key_file)
            break
        else:
            # Fall back to KEY=VALUE parsing of the file
            _, file_key = _parse_kv_file(key_file)
            if file_key:
                service_key = file_key
                log.info("Supabase service key loaded from key file (KEY=VALUE): %s", key_file)
                break
    if not service_key:
        service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if service_key:
            log.debug("Supabase service key loaded from environment variable")

    return url, service_key


def is_enabled() -> bool:
    """Check whether Supabase credentials are currently available."""
    url, key = _load_credentials()
    return bool(url and key)


url, key = _load_credentials()
if not (url and key):
    missing = []
    if not url:
        missing.append("SUPABASE_URL")
    if not key:
        missing.append("SUPABASE_SERVICE_KEY")
    log.warning(
        "Supabase credentials incomplete — missing %s. "
        "Set SUPABASE_URL and SUPABASE_SERVICE_KEY (or SUPABASE_API_KEY) "
        "as env vars or in a KEY=VALUE file pointed to by SUPABASE_KEY_FILE. "
        "Supabase writes are disabled (dry-run mode).",
        " and ".join(missing),
    )


class SupabaseClient:
    def __init__(self):
        url, key = _load_credentials()
        self.url = url.rstrip("/") if url else ""
        self.key = key or ""
        self.enabled = bool(url and key)
        self.session = requests.Session()
        self.session.headers.update({
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        })
        self.session.mount('https://', HTTPAdapter(pool_connections=10, pool_maxsize=10))
        if not self.enabled:
            log.warning("Supabase credentials not found — Supabase writes disabled")

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method: str, path: str, data: Optional[Any] = None,
                 params: Optional[Dict] = None, extra_headers: Optional[Dict[str, str]] = None,
                 max_retries: int = 3) -> Optional[Dict]:
        if not self.enabled:
            return None
        if _supabase_circuit_open():
            log.warning("Supabase circuit open — skipping %s %s", method, path)
            return None
        url = f"{self.url}/rest/v1/{path}"
        headers = self._headers(extra_headers)
        json_data = data if data else None
        last_err = None
        for attempt in range(max_retries):
            try:
                resp = self.session.request(
                    method, url, headers=headers, json=json_data,
                    params=params, timeout=(5, 10)
                )
                if 200 <= resp.status_code < 300:
                    _supabase_record_success()
                    if resp.content:
                        try:
                            return resp.json()
                        except json.JSONDecodeError:
                            return {}
                    return {}
                if resp.status_code in (429, 500, 502, 503, 504):
                    # Retryable errors — record failure for circuit breaker
                    log.warning("Supabase %s %s: HTTP %s — retry %d/%d",
                                method, path, resp.status_code, attempt + 1, max_retries)
                    _supabase_record_failure()
                    time.sleep(1.0 * (attempt + 1))
                    continue
                # Non-retryable errors (4xx except 429) — log and disable client on persistent client/schema errors
                global _schema_warned
                if resp.status_code in (401, 403):
                    # Auth failure — disable client permanently for this process
                    log.error(
                        "Supabase auth failure (HTTP %s) — service key invalid or revoked. "
                        "Supabase writes disabled. Rotate key in dashboard and restart.",
                        resp.status_code,
                    )
                    self.enabled = False
                    return None
                if resp.status_code == 404 and "PGRST205" in resp.text:
                    # Table not created yet (schema.sql not run). One actionable
                    # error, then silence — this fires on every write otherwise.
                    if not _schema_warned:
                        _schema_warned = True
                        log.error(
                            "Supabase tables missing — run supabase/schema.sql in the "
                            "Supabase dashboard SQL Editor. Audit writes will be dropped "
                            "until then."
                        )
                    else:
                        log.debug("Supabase %s %s: table not created yet", method, path)
                    return None
                # Persistent client/schema 4xx (400=bad request, 422=unprocessable).
                # 409 is deliberately NOT here: a unique-constraint conflict on ONE
                # row (e.g. pnl_ledger's idempotency_key) must not disable every
                # later Supabase write for the process — that silently dropped the
                # whole audit trail. Conflicts are logged and the row is skipped.
                if resp.status_code in (400, 422):
                    log.warning(
                        "Supabase %s %s: HTTP %s (%s) — skipping row, keeping client enabled",
                        method, path, resp.status_code, resp.text[:200],
                    )
                    return None
                # Other 4xx — log but don't disable (could be transient)
                log.warning("Supabase %s %s: HTTP %s — non-retryable error, not recording failure",
                            method, path, resp.status_code)
                return None
            except requests.RequestException as e:
                last_err = e
                log.warning("Supabase %s %s error: %s (attempt %d/%d)",
                            method, path, e, attempt + 1, max_retries)
                # Network errors are retryable — record for circuit breaker
                _supabase_record_failure()
                time.sleep(1.0 * (attempt + 1))
        # All retries exhausted (retryable errors only reach here)
        log.warning("Supabase %s %s failed after %d retries: %s",
                    method, path, max_retries, last_err)
        return None

    # ─── signals table ─────────────────────────────────────────────────────────
    def insert_signal(self, signal_data: Dict[str, Any]) -> bool:
        """Insert a new trade signal."""
        data = dict(signal_data)
        if not data.get("type"):
            data["type"] = "taker"
        required = ("ts", "ticker", "side", "price_cents", "contracts")
        missing = [f for f in required if f not in data]
        if missing:
            log.warning("insert_signal: missing required fields %s — skipping", ", ".join(missing))
            return False
        try:
            result = self._request("POST", "signals", data=data)
            return result is not None
        except Exception as e:
            log.warning("insert_signal error: %s", e)
            return False

    def insert_signals_batch(self, signals: list) -> bool:
        """Insert multiple trade signals in a single batch request.

        Supabase REST API accepts a JSON array for bulk inserts, which is
        far more efficient than one POST per signal when recording many
        signals (e.g. after a backtest run or batch evaluation).
        """
        if not signals:
            return True
        required = ("ts", "ticker", "side", "price_cents", "contracts")
        clean = []
        for s in signals:
            item = dict(s)
            if not item.get("type"):
                item["type"] = "taker"
            missing = [f for f in required if f not in item]
            if missing:
                log.warning("insert_signals_batch: skipping signal missing %s", ", ".join(missing))
                continue
            clean.append(item)
        if not clean:
            return False
        try:
            result = self._request("POST", "signals", data=clean)
            return result is not None
        except Exception as e:
            log.warning("insert_signals_batch error: %s", e)
            return False

    # ─── orders table ──────────────────────────────────────────────────────────
    def insert_order(self, order_data: Dict[str, Any]) -> bool:
        """Insert a new order record (upsert on order_id)."""
        try:
            result = self._request("POST", "orders", data=order_data,
                                   params={"on_conflict": "order_id"},
                                   extra_headers={"Prefer": "resolution=merge-duplicates"})
            return result is not None
        except Exception as e:
            log.warning("insert_order error: %s", e)
            return False

    # ─── bot state ─────────────────────────────────────────────────────────────
    def _ns_key(self, key: str) -> str:
        """Namespace a bot_state key by host when BOT_STATE_HOST is set.

        # reviewed: two dry-run traders share the Supabase bot_state row space,
        # so the non-authoritative one tags its keys "<host>:<key>" to stop the
        # last-writer-wins race. The authoritative writer leaves the tag empty.
        """
        host = (getattr(config, "BOT_STATE_HOST", "") or "").strip()
        return f"{host}:{key}" if host else key

    def set_bot_state(self, key: str, value: Any) -> bool:
        """Upsert one bot_state key (PATCH by key, INSERT only for a new key).

        bot_state has a unique constraint on `key`, but its `id` PK has no
        sequence (DEFAULT 1). A plain POST upsert therefore either targets the
        PK or collides on it with id=1, raises 409, and `_request` disables the
        client for the rest of the process — silently dropping every later
        write. PATCH by key avoids the conflict entirely (it hits every key
        after the first run); a brand-new key is the only case needing an
        explicit id.
        """
        key = self._ns_key(key)
        try:
            data = {"value_json": json.dumps(value),
                    "ts": datetime.now(timezone.utc).isoformat()}
            updated = self._request("PATCH", f"bot_state?key=eq.{key}", data=data)
            if updated:
                return True
            # New key. No sequence on id, so mint max(id)+1 by hand; the
            # single-instance lock means there is no concurrent writer to race.
            head = self._request("GET", "bot_state?select=id&order=id.desc&limit=1")
            max_id = head[0]["id"] if head else 0
            created = self._request(
                "POST", "bot_state", data={**data, "id": max_id + 1, "key": key})
            return created is not None
        except Exception as e:
            log.warning("set_bot_state(%s) error: %s", key, e)
            return False

    def set_bot_state_batch(self, states: dict) -> bool:
        """Set multiple bot state key-value pairs using PATCH + INSERT.

        This properly handles upsert on the unique 'key' column.
        Returns True only if every key was written; logs per-key failures so
        partial writes are visible rather than silent.
        """
        try:
            all_ok = True
            for k, v in states.items():
                if not self.set_bot_state(k, v):
                    all_ok = False
                    log.warning("set_bot_state_batch: key %r failed to write", k)
            return all_ok
        except Exception as e:
            log.warning("set_bot_state_batch error: %s", e)
            return False

    def get_bot_state(self, key: str) -> Optional[Any]:
        """Get a bot state value by key (host-namespaced when BOT_STATE_HOST set)."""
        key = self._ns_key(key)
        try:
            result = self._request("GET", f"bot_state?key=eq.{key}&select=value_json")
            # Supabase returns either a list [{"value_json": "..."}] or a dict {"value_json": "..."}
            rows = result if isinstance(result, list) else ([result] if result else [])
            if rows:
                try:
                    return json.loads(rows[0]["value_json"])
                except json.JSONDecodeError as e:
                    log.error(
                        "get_bot_state(%s): corrupt JSON in value_json — %s",
                        key, e
                    )
                    return None
            return None
        except Exception as e:
            log.warning("get_bot_state(%s) error: %s", key, e)
            return None

    # ─── backtest runs ─────────────────────────────────────────────────────────
    def insert_backtest_run(self, params: Dict[str, Any], metrics: Dict[str, Any]) -> bool:
        """Record a backtest run result."""
        try:
            data = {
                "params_json": json.dumps(params),
                "ev_per_trade": metrics.get("ev", 0.0),
                "total_pnl": metrics.get("total_pnl", 0.0),
                "n_trades": metrics.get("n_trades", 0),
                "win_rate": metrics.get("win_rate", 0.0),
                "max_drawdown": metrics.get("max_drawdown", 0.0),
                "final_balance": metrics.get("final_balance", 0.0),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            result = self._request("POST", "backtest_runs", data=data)
            return result is not None
        except Exception as e:
            log.warning("insert_backtest_run error: %s", e)
            return False

    def insert_pnl_ledger(self, ledger_data: Dict[str, Any]) -> bool:
        """Record a settlement P&L ledger entry (idempotent on idempotency_key).

        Upserts on idempotency_key so duplicate settlement events don't
        double-count P&L.
        """
        required = ("idempotency_key", "market_ticker", "realized_pnl_dollars")
        missing = [f for f in required if f not in ledger_data]
        if missing:
            log.warning("insert_pnl_ledger: missing required fields %s — skipping",
                        ", ".join(missing))
            return False
        try:
            # Upsert on the unique idempotency_key, not the bigserial PK: the
            # default on_conflict target is the PK, so a re-run collides on the
            # idempotency_key unique constraint and PostgREST answers 409.
            result = self._request("POST", "pnl_ledger", data=ledger_data,
                                   params={"on_conflict": "idempotency_key"},
                                   extra_headers={"Prefer": "resolution=merge-duplicates"})
            return result is not None
        except Exception as e:
            log.warning("insert_pnl_ledger error: %s", e)
            return False

    # ─── health check ──────────────────────────────────────────────────────────
    def health_check(self) -> bool:
        """Test Supabase connectivity and auth.

        Returns True if Supabase is reachable and the service key is valid,
        AND the required tables exist. Returns False on auth failure (401/403),
        table missing (PGRST205), or connection error.
        """
        url = f"{self.url}/rest/v1/signals?select=*&limit=1"
        try:
            resp = self.session.get(url, headers=self._headers(), timeout=(5, 10))
            if resp.status_code == 200:
                return True
            if resp.status_code in (401, 403):
                log.warning("Supabase health check: HTTP %s — service key invalid or revoked", resp.status_code)
                return False
            if resp.status_code == 404:
                # Check if it's PGRST205 (table doesn't exist) vs other 404
                try:
                    error_data = resp.json()
                    if error_data.get("code") == "PGRST205":
                        log.warning("Supabase health check: table 'signals' does not exist (PGRST205)")
                        return False
                except Exception:
                    pass
                # Other 404 might be a different issue, treat as reachable
                log.warning("Supabase health check: HTTP 404 — unexpected 404")
                return True
            log.warning("Supabase health check: HTTP %s — unexpected response", resp.status_code)
            return True  # reachable, assume auth OK
        except Exception as e:
            log.warning("Supabase health check error: %s", e)
            return False


# Global instance (lazy)
_supabase: Optional[SupabaseClient] = None


def get_supabase() -> SupabaseClient:
    """Get or create the global Supabase client."""
    global _supabase
    if _supabase is None:
        _supabase = SupabaseClient()
    return _supabase