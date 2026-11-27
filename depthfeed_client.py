"""
depthfeed_client.py - Thin DepthFeed MCP client for orderbook snapshots.

DepthFeed (https://api.depthfeed.com/mcp) is a REST fallback / reconciliation
source for Kalshi orderbook snapshots ONLY. It is NOT on the hot path and never
replaces kalshi_client.py for live order placement or sizing.

All public methods return None on any failure (network, auth, plan gate,
parse error) so callers always fall back to Kalshi REST. The bearer token
(DEPTHFEED_MCP_TOKEN) is read from the environment; when unset the client is a
no-op.

Uses raw JSON-RPC over HTTP (the `requests` library already a dependency) so
there is no hard dependency on the `mcp` Python package, which is not installed
in this venv.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

import config

log = logging.getLogger(__name__)

# DepthFeed MCP endpoints available on every plan (keyless) for the reads we use:
#   kalshi_get_snapshots  - recorded Kalshi yes/no depth (keyless, 24h window)
#   kalshi_get_orderbook  - latest live depth (plan-gated; pro/scale/enterprise)
#   screener, whoami      - cross-venue top-of-book / session info (keyless)
_DEPTHFEED_TOOL_ORDERBOOK = "kalshi_get_orderbook"
_DEPTHFEED_TOOL_SNAPSHOTS = "kalshi_get_snapshots"
_DEPTHFEED_TOOL_POLY_SEARCH = "polymarket_search_markets"
_DEPTHFEED_TOOL_POLY_SNAPSHOTS = "polymarket_get_snapshots"
_DEPTHFEED_TOOL_SCREENER = "screener"
_DEPTHFEED_TOOL_WHOAMI = "whoami"

_DEPTHFEED_TOKEN = os.environ.get("DEPTHFEED_MCP_TOKEN", "")
_DEPTHFEED_TOKEN_FILE = os.environ.get("DEPTHFEED_MCP_TOKEN_FILE", "")
if not _DEPTHFEED_TOKEN and _DEPTHFEED_TOKEN_FILE:
    try:
        with open(_DEPTHFEED_TOKEN_FILE) as _tf:
            _DEPTHFEED_TOKEN = _tf.read().strip()
    except Exception:
        _DEPTHFEED_TOKEN = ""
_DEPTHFEED_URL = config.DEPTHFEED_MCP_URL
_DEPTHFEED_TIMEOUT = config.DEPTHFEED_REQUEST_TIMEOUT_SECONDS

if not config.DEPTHFEED_ENABLED:
    log.info("DepthFeed client disabled (DEPTHFEED_ENABLED=false)")
elif not _DEPTHFEED_TOKEN:
    log.info(
        "DepthFeed client enabled in keyless demo mode (BTC Polymarket only). "
        "Set DEPTHFEED_MCP_TOKEN for all 7 assets + Kalshi + deep history."
    )

_LOCK = threading.Lock()
_SESSION: Optional[requests.Session] = None
_LAST_CALL_TS = 0.0
_FAILURES = 0
_CIRCUIT_OPEN_UNTIL = 0.0


def _session() -> requests.Session:
    global _SESSION
    with _LOCK:
        if _SESSION is None:
            _SESSION = requests.Session()
            _SESSION.headers.update(
                {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                }
            )
            if _DEPTHFEED_TOKEN:
                _SESSION.headers.update(
                    {"Authorization": "Bearer %s" % _DEPTHFEED_TOKEN}
                )
        return _SESSION


def _circuit_closed() -> bool:
    global _CIRCUIT_OPEN_UNTIL, _FAILURES
    now = time.time()
    if _CIRCUIT_OPEN_UNTIL > 0 and now < _CIRCUIT_OPEN_UNTIL:
        return False
    if _CIRCUIT_OPEN_UNTIL > 0:
        _CIRCUIT_OPEN_UNTIL = 0.0
        _FAILURES = 0
    return True


def _circuit_record_failure() -> None:
    global _FAILURES, _CIRCUIT_OPEN_UNTIL
    _FAILURES += 1
    if _FAILURES >= 3:
        _CIRCUIT_OPEN_UNTIL = time.time() + 30.0
        _FAILURES = 0
        log.warning("DepthFeed circuit opened for 30s after repeated failures")


def _circuit_record_success() -> None:
    global _FAILURES, _CIRCUIT_OPEN_UNTIL
    _FAILURES = 0
    _CIRCUIT_OPEN_UNTIL = 0.0


def _min_gap_sleep() -> None:
    """Throttle DepthFeed calls to respect the demo plan (2 rps / 60 rpm)."""
    global _LAST_CALL_TS
    now = time.time()
    gap = now - _LAST_CALL_TS
    min_gap = 0.6  # well under 2 rps
    if gap < min_gap:
        time.sleep(min_gap - gap)
    _LAST_CALL_TS = time.time()


def _mcp_call(tool_name: str, params: dict, request_id: int) -> Optional[dict]:
    """One raw JSON-RPC POST to the DepthFeed MCP endpoint.

    Uses the MCP `tools/call` method: the tool name goes in params.name and
    the tool arguments in params.arguments. Returns parsed result dict or None
    on any failure. Never raises.

    Keyless demo tier works without a bearer token (BTC Polymarket only).
    When DEPTHFEED_MCP_TOKEN is set it is sent as a Bearer header, unlocking
    all 7 assets, Kalshi, full ladder, and deep history.
    """
    if not config.DEPTHFEED_ENABLED:
        return None
    if not _circuit_closed():
        return None
    _min_gap_sleep()
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": params},
    }
    try:
        resp = _session().post(_DEPTHFEED_URL, json=payload, timeout=_DEPTHFEED_TIMEOUT)
        if resp.status_code != 200:
            log.warning(
                "DepthFeed MCP %s HTTP %s - falling back to Kalshi REST",
                tool_name, resp.status_code,
            )
            _circuit_record_failure()
            return None
        data = resp.json()
    except Exception as e:
        log.warning("DepthFeed MCP %s request error: %s - falling back to Kalshi REST",
                    tool_name, e)
        _circuit_record_failure()
        return None
    if "error" in data:
        log.warning(
            "DepthFeed MCP %s error code=%s message=%s - falling back to Kalshi REST",
            tool_name, data["error"].get("code"), data["error"].get("message"),
        )
        _circuit_record_failure()
        return None
    _circuit_record_success()
    return data.get("result")


def _extract_text(result: dict) -> str:
    """MCP tool results carry content as [{type, text}]; text is a JSON string."""
    content = result.get("content", [])
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "".join(parts)


def _parse_json_text(text: str) -> Optional[dict]:
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        # Some servers wrap JSON in markdown fences; strip them defensively.
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            stripped = stripped.lstrip("json").strip()
        try:
            return json.loads(stripped)
        except (ValueError, TypeError):
            return None


def _price_cents(v) -> Optional[int]:
    """DepthFeed prices arrive as decimal dollars (0.48 = 48c). Return int cents."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0 or f > 1.0:
        return None
    return int(round(f * 100))


def _qty(v) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _normalize_side(raw) -> list:
    """Convert DepthFeed [[price, qty], ...] (dollars) to [[price_cents, qty], ...],
    sorted best-first (highest price first for bids/yes, lowest first for asks/no)."""
    result = []
    if not isinstance(raw, list):
        return result
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        pc = _price_cents(entry[0])
        if pc is None:
            continue
        q = _qty(entry[1])
        if q is None or q <= 0:
            continue
        result.append([pc, q])
    result.sort(key=lambda x: x[0], reverse=True)
    return result


def get_orderbook_snapshot(ticker: str) -> Optional[dict]:
    """Return the latest DepthFeed-recorded Kalshi orderbook for ``ticker``.

    Returns {"yes": [[price_cents, qty], ...], "no": [[price_cents, qty], ...],
    "ts_ms": int, "source": "depthfeed"} sorted best-first, or None on any
    failure so callers fall back to Kalshi REST. Never raises.
    """
    if not config.DEPTHFEED_ENABLED:
        return None
    result = _mcp_call(
        _DEPTHFEED_TOOL_SNAPSHOTS,
        {"ticker": ticker, "limit": 50},
        request_id=1,
    )
    if result is None:
        return None
    text = _extract_text(result)
    data = _parse_json_text(text)
    if not isinstance(data, list) or not data:
        return None
    # DepthFeed snapshots are historical; select only this ticker's proven book.
    latest = None
    latest_ts = None
    for entry in data:
        if not isinstance(entry, dict) or not _identity_matches(entry, ticker):
            continue
        ts_ms = _entry_timestamp_ms(entry)
        if ts_ms is not None and (latest_ts is None or ts_ms > latest_ts):
            latest, latest_ts = entry, ts_ms
    if latest is None:
        return None
    return {
        "yes": _normalize_side(latest.get("yes", [])),
        "no": _normalize_side(latest.get("no", [])),
        "ts_ms": latest_ts,
        "source": "depthfeed",
        "raw_timestamp": (latest.get("timestamp") or latest.get("ts")
                          or latest.get("time")),
    }


def get_snapshots(ticker: str, limit: int = 50) -> Optional[list]:
    """Raw DepthFeed kalshi_get_snapshots list for ``ticker`` (most recent first),
    or None on failure. Reconciliation/debug use only."""
    if not config.DEPTHFEED_ENABLED:
        return None
    result = _mcp_call(
        _DEPTHFEED_TOOL_SNAPSHOTS,
        {"ticker": ticker, "limit": limit},
        request_id=2,
    )
    if result is None:
        return None
    text = _extract_text(result)
    data = _parse_json_text(text)
    if not isinstance(data, dict) or "data" not in data:
        return None
    data = data["data"]
    if not isinstance(data, list):
        return None
    return data


def available() -> bool:
    """True if the DepthFeed client is enabled (keyless demo works without a token)."""
    return bool(config.DEPTHFEED_ENABLED)


def failures() -> int:
    """Consecutive failure count (for observability, never gates trading)."""
    return _FAILURES


# ========================================================================
# Polymarket support (keyless demo tier: BTC Polymarket markets immediately;
# bearer token unlocks all 7 assets + Kalshi + full ladder + deep history)
# ========================================================================

def get_polymarket_markets(coin: str = "btc", limit: int = 50) -> Optional[list]:
    """Search Polymarket up/down crypto markets for a coin via DepthFeed MCP.

    Returns the raw list of market dicts (with slug, market_id, condition_id,
    clob_token_up/down, start/end times, etc.) or None on failure.
    Never raises.
    """
    if not config.DEPTHFEED_ENABLED:
        return None
    result = _mcp_call(_DEPTHFEED_TOOL_POLY_SEARCH,
                       {"query": coin, "limit": limit},
                       request_id=3)
    if result is None:
        return None
    text = _extract_text(result)
    data = _parse_json_text(text)
    if not isinstance(data, dict) or "data" not in data:
        return None
    data = data["data"]
    if not isinstance(data, list):
        return None
    return data


def get_polymarket_snapshots(market_id: str, limit: int = 50) -> Optional[list]:
    """Raw DepthFeed polymarket_get_snapshots list for a market_id.

    Returns list of snapshot dicts (btc_price, price_up, price_down, time,
    id, market_id) or None on failure. Never raises.
    """
    if not config.DEPTHFEED_ENABLED:
        return None
    result = _mcp_call(_DEPTHFEED_TOOL_POLY_SNAPSHOTS,
                       {"market_id": market_id, "limit": limit,
                        "include_orderbook": True},
                       request_id=4)
    if result is None:
        return None
    text = _extract_text(result)
    data = _parse_json_text(text)
    if not isinstance(data, dict) or "data" not in data:
        return None
    data = data["data"]
    if not isinstance(data, list):
        return None
    return data


def _snapshot_timestamp_ms(value) -> Optional[int]:
    if value is None:
        return None
    try:
        text = str(value).strip()
        if text.replace(".", "", 1).isdigit():
            number = float(text)
            return int(number if abs(number) >= 100_000_000_000 else number * 1000)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _identity_matches(snapshot: dict, market_id: str) -> bool:
    expected = str(market_id)
    return any(str(snapshot.get(key)) == expected
               for key in ("market_id", "ticker", "market_ticker", "slug"))


def _entry_timestamp_ms(snapshot: dict) -> Optional[int]:
    values = []
    for key in ("time", "timestamp", "ts"):
        if key in snapshot and snapshot[key] not in (None, ""):
            ts_ms = _snapshot_timestamp_ms(snapshot[key])
            if ts_ms is None:
                return None
            values.append(ts_ms)
    if not values or any(value != values[0] for value in values[1:]):
        return None
    return values[0]


def _latest_snapshot(snapshots: list, market_id: str):
    latest = None
    latest_ts = None
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or not _identity_matches(snapshot, market_id):
            continue
        ts_ms = _entry_timestamp_ms(snapshot)
        if ts_ms is not None and (latest_ts is None or ts_ms > latest_ts):
            latest, latest_ts = snapshot, ts_ms
    return latest, latest_ts


def get_polymarket_orderbook(market_id: str) -> Optional[dict]:
    """Return the latest Polymarket orderbook snapshot for a market.

    Normalizes polymarket snapshots into a unified book shape:
    {"up": [[price, qty], ...], "down": [[price, qty], ...],
     "mid": float, "ts_ms": int, "source": "depthfeed", "market_id": str}

    Polymarket snapshots store price_up/price_down (midpoint and complement)
    but not the full ladder unless include_orderbook=true was used.
    Returns None on failure so callers fall back gracefully. Never raises.
    """
    snapshots = get_polymarket_snapshots(market_id, limit=5)
    if not snapshots:
        return None
    latest, ts_ms = _latest_snapshot(snapshots, market_id)
    if latest is None:
        return None
    price_up = latest.get("price_up")
    price_down = latest.get("price_down")
    mid = None
    if price_up is not None:
        try:
            mid = float(price_up)
        except (TypeError, ValueError):
            pass
    return {
        "up": [[mid, 0]] if mid else [],  # depthfeed keyless returns top-of-book only
        "down": [[1.0 - mid, 0]] if mid else [],
        "mid": mid,
        "ts_ms": ts_ms,
        "source": "depthfeed",
        "market_id": market_id,
        "raw_timestamp": (latest.get("time") or latest.get("timestamp")
                          or latest.get("ts")),
        "orderbook_up": latest.get("orderbook_up"),
        "orderbook_down": latest.get("orderbook_down"),
    }


def get_screener(coin: str = "btc") -> Optional[dict]:
    """Cross-venue top-of-book snapshot via DepthFeed screener.

    Returns the screener data dict with assets/cells/windows, or None on failure.
    Never raises.
    """
    if not config.DEPTHFEED_ENABLED:
        return None
    result = _mcp_call(_DEPTHFEED_TOOL_SCREENER,
                       {"coin": coin},
                       request_id=5)
    if result is None:
        return None
    text = _extract_text(result)
    data = _parse_json_text(text)
    if not isinstance(data, dict) or "data" not in data:
        return None
    return data["data"]


def get_whoami() -> Optional[dict]:
    """Return the DepthFeed session's plan, limits, and coin access.

    Useful for verifying which assets/venues are unlocked with the current key.
    Returns None on failure. Never raises.
    """
    if not config.DEPTHFEED_ENABLED:
        return None
    result = _mcp_call(_DEPTHFEED_TOOL_WHOAMI, {}, request_id=6)
    if result is None:
        return None
    text = _extract_text(result)
    data = _parse_json_text(text)
    if not isinstance(data, dict) or "data" not in data:
        return None
    return data["data"]
