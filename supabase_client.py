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
from typing import Optional, Dict, Any, List
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger(__name__)

# ── Circuit breaker for Supabase outages ──
# Trips after N consecutive failures, skips writes for a cooldown period.
_SUPABASE_FAILURES: int = 0
_SUPABASE_SKIP_UNTIL: float = 0.0
_SUPABASE_CIRCUIT_THRESHOLD = 5
_SUPABASE_CIRCUIT_COOLDOWN = 60.0  # seconds


def _supabase_circuit_open() -> bool:
    """Return True if Supabase writes are currently skipped due to outage."""
    global _SUPABASE_SKIP_UNTIL
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
    global _SUPABASE_FAILURES, _SUPABASE_SKIP_UNTIL
    _SUPABASE_FAILURES = 0
    _SUPABASE_SKIP_UNTIL = 0.0


# ── Load credentials ─────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY_FILE = os.environ.get("SUPABASE_KEY_FILE", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

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
    """Read a raw service key from a protected file (no KEY=VALUE wrapper).

    The file must contain only the key value, with optional trailing newline.
    Mode must be 0600 or stricter; otherwise the key is refused.
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
            return f.read().strip()
    except OSError as e:
        log.warning("Could not read Supabase key file %s: %s", path, e)
        return ""


# Load URL from env or key file
if not SUPABASE_URL and SUPABASE_KEY_FILE:
    file_url, _ = _parse_kv_file(SUPABASE_KEY_FILE)
    SUPABASE_URL = SUPABASE_URL or file_url

# Load service key: protected file first (0600), then env var, then .env
if SUPABASE_KEY_FILE:
    raw_key = _parse_raw_key_file(SUPABASE_KEY_FILE)
    if raw_key:
        SUPABASE_SERVICE_KEY = raw_key
elif not SUPABASE_SERVICE_KEY and SUPABASE_KEY_FILE:
    # Fall back to KEY=VALUE parsing of the file
    _, file_key = _parse_kv_file(SUPABASE_KEY_FILE)
    SUPABASE_SERVICE_KEY = SUPABASE_SERVICE_KEY or file_key

# Also check root .env
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    root_env = Path(__file__).resolve().parent.parent / ".env"
    if root_env.exists():
        env_url, env_key = _parse_kv_file(str(root_env))
        SUPABASE_URL = SUPABASE_URL or env_url
        SUPABASE_SERVICE_KEY = SUPABASE_SERVICE_KEY or env_key

def is_enabled() -> bool:
    """Check whether Supabase credentials are currently available."""
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


if not is_enabled():
    missing = []
    if not SUPABASE_URL:
        missing.append("SUPABASE_URL")
    if not SUPABASE_SERVICE_KEY:
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
        self.url = SUPABASE_URL.rstrip("/") if SUPABASE_URL else ""
        self.key = SUPABASE_SERVICE_KEY or ""
        self.enabled = is_enabled()
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

    def _request(self, method: str, path: str, data: Optional[Dict] = None,
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
                    log.warning("Supabase %s %s: HTTP %s — retry %d/%d",
                                method, path, resp.status_code, attempt + 1, max_retries)
                    time.sleep(1.0 * (attempt + 1))
                    continue
                log.warning("Supabase %s %s failed: %s %s",
                            method, path, resp.status_code, resp.text[:200])
                _supabase_record_failure()
                return None
            except requests.RequestException as e:
                last_err = e
                log.warning("Supabase %s %s error: %s (attempt %d/%d)",
                            method, path, e, attempt + 1, max_retries)
                time.sleep(1.0 * (attempt + 1))
        _supabase_record_failure()
        log.warning("Supabase %s %s failed after %d retries: %s",
                    method, path, max_retries, last_err)
        return None

    # ─── signals table ─────────────────────────────────────────────────────────
    def insert_signal(self, signal_data: Dict[str, Any]) -> bool:
        """Insert a new trade signal."""
        required = ("ts", "ticker", "side", "price_cents", "contracts")
        missing = [f for f in required if f not in signal_data]
        if missing:
            log.warning("insert_signal: missing required fields %s — skipping", ", ".join(missing))
            return False
        try:
            result = self._request("POST", "signals", data=signal_data)
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
            missing = [f for f in required if f not in s]
            if missing:
                log.warning("insert_signals_batch: skipping signal missing %s", ", ".join(missing))
                continue
            clean.append(s)
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
        """Insert a new order record."""
        try:
            result = self._request("POST", "orders", data=order_data)
            return result is not None
        except Exception as e:
            log.warning("insert_order error: %s", e)
            return False

    def update_order_settlement(self, order_id: str, status: str,
                                fill_price_cents: Optional[int] = None,
                                outcome_won: Optional[bool] = None,
                                pnl_dollars: Optional[float] = None) -> bool:
        """Update an order with settlement outcome."""
        try:
            data: Dict[str, Any] = {"status": status}
            if fill_price_cents is not None:
                data["fill_price_cents"] = fill_price_cents
            if outcome_won is not None:
                data["outcome_won"] = outcome_won
            if pnl_dollars is not None:
                data["pnl_dollars"] = pnl_dollars
            data["settled_at"] = datetime.now(timezone.utc).isoformat()
            result = self._request("PATCH", f"orders?order_id=eq.{order_id}", data=data)
            return result is not None
        except Exception as e:
            log.warning("update_order_settlement error: %s", e)
            return False

    # ─── bot state ─────────────────────────────────────────────────────────────
    def set_bot_state(self, key: str, value: Any) -> bool:
        """Set a bot state key-value pair (upsert)."""
        try:
            data = {"key": key, "value_json": json.dumps(value),
                    "ts": datetime.now(timezone.utc).isoformat()}
            result = self._request("POST", "bot_state", data=data,
                                   extra_headers={"Prefer": "resolution=merge-duplicates"})
            return result is not None
        except Exception as e:
            log.warning("set_bot_state error: %s", e)
            return False

    def get_bot_state(self, key: str) -> Optional[Any]:
        """Get a bot state value by key."""
        try:
            result = self._request("GET", f"bot_state?key=eq.{key}&select=value_json")
            # Supabase returns either a list [{"value_json": "..."}] or a dict {"value_json": "..."}
            rows = result if isinstance(result, list) else ([result] if result else [])
            if rows:
                return json.loads(rows[0]["value_json"])
            return None
        except Exception as e:
            log.warning("get_bot_state error: %s", e)
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

    # ─── health check ──────────────────────────────────────────────────────────
    def health_check(self) -> bool:
        """Test Supabase connectivity."""
        try:
            result = self._request("GET", "signals?select=*&limit=1", max_retries=1)
            return result is not None
        except Exception:
            return False


# Global instance (lazy)
_supabase: Optional[SupabaseClient] = None


def get_supabase() -> SupabaseClient:
    """Get or create the global Supabase client."""
    global _supabase
    if _supabase is None:
        _supabase = SupabaseClient()
    return _supabase