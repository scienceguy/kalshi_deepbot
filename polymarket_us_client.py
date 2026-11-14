"""
Polymarket US client for Kalshi SuperBot.

Targets the US production API at api.polymarket.us.
Reads balances, market data, and places fee-free maker limit orders.
Credentials: config.POLYMARKET_KEY_ID / config.POLYMARKET_SECRET_KEY,
environment variables POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY,
or fallback to the Bitwarden mirror at ~/.secrets/bw/polymarket_*.json.

Never logs or prints credential values.
"""

import json
import logging
import os
import warnings
from pathlib import Path

from polymarket_us import PolymarketUS
from config import POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY

log = logging.getLogger(__name__)


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
    _default_bw = str(Path.home() / ".secrets/bw")
    _BW_DIR = Path(os.environ.get("BW_DIR", _default_bw))
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
    _default_bw = str(Path.home() / ".secrets/bw")
    _BW_DIR = Path(os.environ.get("BW_DIR", _default_bw))
    _secret_file = _BW_DIR / "polymarket_secret_key.json"
    if _secret_file.exists():
        try:
            data = json.loads(_secret_file.read_text())
            return data.get("value", "")
        except (json.JSONDecodeError, OSError):
            return ""
    return ""


_cached_client = None
_cached_creds = ("", "")


def _get_client():
    """Return a configured PolymarketUS client, caching to reuse HTTP connection pools.

    Reads creds from environment / BW mirror and refreshes client if credentials change.
    """
    global _cached_client, _cached_creds
    key_id = _load_key_id()
    secret_key = _load_secret_key()
    if not key_id or not secret_key:
        return None
    if _cached_client is None or _cached_creds != (key_id, secret_key):
        _cached_client = PolymarketUS(key_id=key_id, secret_key=secret_key)
        _cached_creds = (key_id, secret_key)
    return _cached_client


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
    """Fetch order book / BBO for a market slug."""
    temp_client = _get_client() or PolymarketUS(key_id="", secret_key="")
    try:
        res = temp_client.markets.bbo(slug)
        return res if isinstance(res, dict) else (res.model_dump() if hasattr(res, "model_dump") else {})
    except Exception:
        try:
            res = temp_client.markets.book(slug)
            return res if isinstance(res, dict) else (res.model_dump() if hasattr(res, "model_dump") else {})
        except Exception as e:
            log.warning("Poly get_market_book %s failed: %s", slug, e)
            return {}


def get_market_settlement(slug: str) -> dict | None:
    """Fetch settlement outcome for a market slug.

    Returns dict with {"slug": slug, "settlement": float(1.0 or 0.0)} if settled,
    or None if market is not yet settled or not found.
    """
    temp_client = _get_client() or PolymarketUS(key_id="", secret_key="")
    try:
        res = temp_client.markets.settlement(slug)
        if isinstance(res, dict) and "settlement" in res:
            return {"slug": slug, "settlement": float(res["settlement"])}
        elif hasattr(res, "settlement"):
            return {"slug": slug, "settlement": float(res.settlement)}
    except Exception:
        pass
    return None


def get_events(limit: int = 100) -> list:
    """List events from Polymarket US (public gateway)."""
    temp_client = _get_client() or PolymarketUS(key_id="", secret_key="")
    try:
        res = temp_client.events.list({"limit": limit})
        if isinstance(res, dict):
            return res.get("events", []) or []
        return getattr(res, "events", []) or []
    except Exception as e:
        log.warning("Poly get_events failed: %s", e)
        return []


def get_event_by_slug(slug: str) -> dict:
    """Retrieve full event detail including all child markets by slug."""
    temp_client = _get_client() or PolymarketUS(key_id="", secret_key="")
    try:
        res = temp_client.events.retrieve_by_slug(slug)
        return res if isinstance(res, dict) else (res.model_dump() if hasattr(res, "model_dump") else {})
    except Exception as e:
        log.warning("Poly get_event_by_slug %s failed: %s", slug, e)
        return {}


def search_events(query: str) -> list:
    """Search Polymarket US events by keyword (e.g. BTC, ETH, SOL)."""
    temp_client = _get_client() or PolymarketUS(key_id="", secret_key="")
    try:
        res = temp_client.search.query({"query": query})
        if isinstance(res, dict):
            return res.get("events", []) or []
        return getattr(res, "events", []) or []
    except Exception as e:
        log.warning("Poly search_events %s failed: %s", query, e)
        return []


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


# ── Authenticated API (order placement) ──────────────────────────────────

def _poly_amount(value: float) -> dict:
    """Build an Amount dict for the Polymarket US SDK."""
    return {"value": f"{value:.2f}", "currency": "USD"}


def preview_order(slug: str, side: str, price_dollars: float,
                  contracts: int) -> dict | None:
    """Validate an order against Polymarket US production order preview API.

    Returns the preview response dict on success, or None on failure.
    """
    client = _get_authenticated_client()
    intent = ("ORDER_INTENT_BUY_LONG" if side == "yes"
              else "ORDER_INTENT_BUY_SHORT")
    params = {
        "marketSlug": slug,
        "intent": intent,
        "type": "ORDER_TYPE_LIMIT",
        "price": _poly_amount(price_dollars),
        "quantity": contracts,
        "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        "participateDontInitiate": True,   # fee-free maker
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    }
    try:
        return client.orders.preview({"request": params})
    except Exception as e:
        log.warning("Poly preview failed for %s %s @ %.4f: %s",
                    slug, side, price_dollars, e)
        return None


def place_maker_order(slug: str, side: str, price_dollars: float,
                      contracts: int) -> dict | None:
    """Place a fee-free maker limit order on Polymarket US.

    Uses ``participateDontInitiate=True`` so the order provides liquidity and
    pays no taker fee. Intended for favourite-maker entries at 75-99c.

    Args:
        slug: Polymarket US market slug (e.g. ``cpc-btc-updown-15m-...``).
        side: ``"yes"`` (buy the Up/Yes token) or ``"no"`` (sell Yes / buy Down).
        price_dollars: Limit price in dollars (0–1).
        contracts: Number of contracts (integer).

    Returns:
        The create response dict, or None on failure. In DRY_RUN the caller
        should decide whether to actually create; this function always attempts
        the create after a successful preview.
    """
    if preview_order(slug, side, price_dollars, contracts) is None:
        return None

    client = _get_authenticated_client()
    intent = ("ORDER_INTENT_BUY_LONG" if side == "yes"
              else "ORDER_INTENT_BUY_SHORT")
    params = {
        "marketSlug": slug,
        "intent": intent,
        "type": "ORDER_TYPE_LIMIT",
        "price": _poly_amount(price_dollars),
        "quantity": contracts,
        "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        "participateDontInitiate": True,   # fee-free maker
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    }
    # Create: place the order.
    try:
        resp = client.orders.create(params)
        return resp
    except Exception as e:
        log.warning("Poly create failed for %s %s @ %.4f: %s",
                    slug, side, price_dollars, e)
        return None


def get_open_orders() -> list:
    """List open Polymarket US orders."""
    client = _get_authenticated_client()
    try:
        resp = client.orders.list()
        return resp.get("orders", []) or []
    except Exception as e:
        log.warning("Poly get_open_orders failed: %s", e)
        return []


def cancel_order(order_id: str, market_slug: str = "") -> bool:
    """Cancel a Polymarket US order by id and market slug.

    If market_slug is not provided, looks up the order's marketSlug from
    get_open_orders() so callers don't fail with BadRequestError.
    """
    client = _get_authenticated_client()
    if not market_slug:
        for o in get_open_orders():
            if o.get("id") == order_id:
                market_slug = o.get("marketSlug", "")
                break
    if not market_slug:
        log.warning("Poly cancel_order %s failed: market_slug required and not found in open orders", order_id)
        return False
    try:
        client.orders.cancel(order_id, {"marketSlug": market_slug})
        return True
    except Exception as e:
        log.warning("Poly cancel_order %s failed: %s", order_id, e)
        return False


def close_position(slug: str) -> bool:
    """Close the position on a Polymarket US market."""
    client = _get_authenticated_client()
    try:
        client.orders.close_position({"marketSlug": slug})
        return True
    except Exception as e:
        log.warning("Poly close_position %s failed: %s", slug, e)
        return False
