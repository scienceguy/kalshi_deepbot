"""
Polymarket US read-only client for Kalshi SuperBot.

Targets the US production API at api.polymarket.us.
Read-only: no order placement, only portfolio/balance/market data.
Credentials: config.POLYMARKET_KEY_ID / config.POLYMARKET_SECRET_KEY,
environment variables POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY,
or fallback to the Bitwarden mirror at ~/.secrets/bw/polymarket_*.json.

Never logs or prints credential values.
"""

import json
import os
import warnings
from pathlib import Path

from polymarket_us import PolymarketUS
from config import POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY


# ── Credential loading (reads from config, then env, then BW mirror) ───────────

def _load_key_id() -> str:
    """Load POLYMARKET_KEY_ID from config, then env, then BW mirror. Returns empty string if none."""
    # 1. Try config (which reads from env via _env_str)
    if POLYMARKET_KEY_ID:
        return POLYMARKET_KEY_ID
    # 2. Fall back to os.environ
    key_id = os.environ.get("POLYMARKET_KEY_ID", "")
    if key_id:
        return key_id
    # 3. Fall back to BW mirror
    _BW_DIR = Path(os.environ.get("BW_DIR", "/data/data/com.termux/files/home/.secrets/bw"))
    _key_file = _BW_DIR / "polymarket_api_key.json"
    if _key_file.exists():
        try:
            data = json.loads(_key_file.read_text())
            return data.get("value", "")
        except (json.JSONDecodeError, OSError):
            return ""
    return ""


def _load_secret_key() -> str:
    """Load POLYMARKET_SECRET_KEY from config, then env, then BW mirror. Returns empty string if none."""
    # 1. Try config (which reads from env via _env_str)
    if POLYMARKET_SECRET_KEY:
        return POLYMARKET_SECRET_KEY
    # 2. Fall back to os.environ
    secret_key = os.environ.get("POLYMARKET_SECRET_KEY", "")
    if secret_key:
        return secret_key
    # 3. Fall back to BW mirror
    _BW_DIR = Path(os.environ.get("BW_DIR", "/data/data/com.termux/files/home/.secrets/bw"))
    _secret_file = _BW_DIR / "polymarket_secret_key.json"
    if _secret_file.exists():
        try:
            data = json.loads(_secret_file.read_text())
            return data.get("value", "")
        except (json.JSONDecodeError, OSError):
            return ""
    return ""


def _get_client():
    """Return a configured PolymarketUS client, or None if no creds available.

    Always reads creds fresh from os.environ (or BW mirror), so monkeypatching
    env vars in tests works correctly — no module-level caching.
    """
    key_id = _load_key_id()
    secret_key = _load_secret_key()
    if not key_id or not secret_key:
        return None
    return PolymarketUS(key_id=key_id, secret_key=secret_key)


# ── Public API (market data: no auth required) ────────────────────────────

def get_market_by_slug(slug: str) -> dict:
    """Fetch market data by slug (public endpoint, no auth required).

    Calls GET /v1/market/slug/{slug} — data is public on Polymarket US.
    """
    # For unauthenticated calls, create a temp client; the SDK routes to
    # gateway_base_url and ignores key_id/secret_key when authenticated=False.
    temp_client = _get_client()  # may be None, but we pass authenticated=False below
    # The SDK's _request method uses gateway_base_url when authenticated=False,
    # and doesn't require key_id/secret_key. We can pass authenticated=False
    # even if _get_client() returns None because the SDK handles that case.
    # However, to be safe, we create a minimal client if none available.
    if temp_client is None:
        temp_client = PolymarketUS(key_id="", secret_key="")
    return temp_client.get(f"/v1/market/slug/{slug}", authenticated=False)


def get_market_book(slug: str) -> dict:
    """Fetch order book / BBO for a market slug.

    Tries the full order book path first (/book), then falls back to BBO (/bbo).
    Market data is public — no auth credentials required.
    """
    try:
        data = _get_market_data(f"/v1/market/slug/{slug}/book", authenticated=False)
    except Exception:
        # Fall back to BBO endpoint
        data = _get_market_data(f"/v1/market/slug/{slug}/bbo", authenticated=False)
    return data


def _get_market_data(path: str, *, authenticated: bool = False) -> dict:
    """Internal helper: call SDK get() for market data with the appropriate auth flag."""
    temp_client = PolymarketUS(key_id="", secret_key="") if not authenticated else _get_client()
    if temp_client is None and not authenticated:
        temp_client = PolymarketUS(key_id="", secret_key="")
    return temp_client.get(path, authenticated=authenticated)


# ── Authenticated API (portfolio & balances) ─────────────────────────────

def _get_authenticated_client():
    """Return a configured client for authenticated calls, or raise RuntimeError.

    This reads creds fresh each time, so monkeypatching env vars works.
    """
    key_id = _load_key_id()
    secret_key = _load_secret_key()
    if not key_id or not secret_key:
        raise RuntimeError(
            "Polymarket US client not configured: missing POLYMARKET_KEY_ID and/or "
            "POLYMARKET_SECRET_KEY (and no BW mirror files). "
            "Set env vars POLYMARKET_KEY_ID/POLYMARKET_SECRET_KEY or place "
            "Bitwarden mirror files at ~/.secrets/bw/polymarket_*.json."
        )
    return PolymarketUS(key_id=key_id, secret_key=secret_key)


def get_portfolio_positions() -> dict:
    """Fetch portfolio positions (authenticated endpoint)."""
    client = _get_authenticated_client()
    return client.get("/v1/portfolio/positions", authenticated=True)


def get_account_balances() -> dict:
    """Fetch account balances (authenticated endpoint)."""
    client = _get_authenticated_client()
    return client.get("/v1/account/balances", authenticated=True)


def usd_cash_balance(data: dict) -> float:
    """Extract USD cash from a /v1/account/balances response.

    Schema (official): {"balances": [{"currentBalance": <num>, "currency": "USD", ...}]}.
    Returns 0.0 when no USD entry is present.
    """
    for entry in data.get("balances") or []:
        if entry.get("currency") == "USD":
            try:
                return float(entry.get("currentBalance", 0.0))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def close() -> None:
    """Close the SDK client connection.

    Note: since we don't cache a long-lived client, this is a no-op for the
    typical pattern. Provided for API completeness.
    """
    # No persistent client to close; each call creates its own.
    pass