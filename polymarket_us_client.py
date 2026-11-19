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
import time
from pathlib import Path

from polymarket_us import PolymarketUS
from config import POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY
import config

import rate_limit

log = logging.getLogger(__name__)

POLYMARKET_MIN_GAP_SECONDS: float = 0.20  # Max 5 req/s to prevent Cloudflare 429 throttling (reads)
POLYMARKET_ORDER_MIN_GAP_SECONDS: float = 0.20  # Max 5 req/s for orders (may be different)
POLYMARKET_BULK_MIN_GAP_SECONDS: float = 0.50  # Max 2 req/s for bulk (settlement, search)

# A2: Separate pacing lanes for reads, orders, and bulk.
# Orders never wait behind bulk/reads.
_RATE_BUCKET_READS = rate_limit.TokenBucket(1.0 / POLYMARKET_MIN_GAP_SECONDS,
                                             burst=1,
                                             name="polymarket_us_reads")
_RATE_BUCKET_ORDERS = rate_limit.TokenBucket(1.0 / POLYMARKET_ORDER_MIN_GAP_SECONDS,
                                              burst=1,
                                              name="polymarket_us_orders")
_RATE_BUCKET_BULK = rate_limit.TokenBucket(1.0 / POLYMARKET_BULK_MIN_GAP_SECONDS,
                                            burst=1,
                                            name="polymarket_us_bulk")

def _enforce_rate_limit(min_gap: float = POLYMARKET_MIN_GAP_SECONDS, lane: str = "reads") -> None:
    """Pace Polymarket US requests via lane-specific token bucket (A2).

    Args:
        min_gap: Override min gap for this call (rare).
        lane: "reads" | "orders" | "bulk" — orders lane never waits behind bulk.
    """
    if min_gap != POLYMARKET_MIN_GAP_SECONDS:
        time.sleep(min_gap)
        return
    if lane == "orders":
        _RATE_BUCKET_ORDERS.acquire(block=True)
    elif lane == "bulk":
        _RATE_BUCKET_BULK.acquire(block=True)
    else:
        _RATE_BUCKET_READS.acquire(block=True)

class PolymarketAPIError(Exception):
    """Raised when a Polymarket US API call fails."""
    pass

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
_cached_unauth_client = None  # A4: cached unauthenticated client for public endpoints

def _get_unauth_client():
    """Return a cached unauthenticated PolymarketUS client for public endpoints.

    Reuses HTTP connection pool instead of creating a new client per call.
    """
    global _cached_unauth_client
    if _cached_unauth_client is None:
        _cached_unauth_client = PolymarketUS(key_id="", secret_key="")
    return _cached_unauth_client

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

def reload_credentials() -> None:
    """A4: Explicitly reload credentials and recreate clients.

    Call this after updating environment variables or BW mirror files.
    """
    global _cached_client, _cached_creds, _cached_unauth_client
    _cached_client = None
    _cached_creds = ("", "")
    _cached_unauth_client = None

# ── Public API (market data: no auth required) ────────────────────────────

def get_market_by_slug(slug: str) -> dict:
    """Fetch market data by slug (public endpoint, no auth required).

    Calls GET /v1/market/slug/{slug} — data is public on Polymarket US.
    """
    _enforce_rate_limit(lane="reads")
    client = _get_unauth_client()
    return client.get(f"/v1/market/slug/{slug}", authenticated=False)

def get_market_book(slug: str) -> dict:
    """Fetch order book / BBO for a market slug.

    A4: Uses cached unauthenticated client; fallback to .book() uses separate
    rate limit so a flaky .bbo() cannot double the latency.
    """
    _enforce_rate_limit(lane="reads")
    client = _get_unauth_client()
    try:
        res = client.markets.bbo(slug)
        return res if isinstance(res, dict) else (res.model_dump() if hasattr(res, "model_dump") else {})
    except Exception:
        # A4: Fallback gets its own rate limit budget (reads lane)
        _enforce_rate_limit(lane="reads")
        try:
            res = client.markets.book(slug)
            return res if isinstance(res, dict) else (res.model_dump() if hasattr(res, "model_dump") else {})
        except Exception as e:
            log.warning("Poly get_market_book %s failed: %s", slug, e)
            return {}

def get_market_settlement(slug: str) -> dict | None:
    """Fetch settlement outcome for a market slug.

    Returns dict with {"slug": slug, "settlement": float(1.0 or 0.0)} if settled,
    or None if market is not yet settled or not found.

    A3: Single attempt, returns NotSettledYet on 404. A background poller
    (separate thread) should own retries with backoff and jitter.
    """
    client = _get_client() or _get_unauth_client()
    try:
        _enforce_rate_limit(lane="bulk")
        res = client.markets.settlement(slug)
        if isinstance(res, dict) and "settlement" in res:
            return {"slug": slug, "settlement": float(res["settlement"])}
        elif hasattr(res, "settlement"):
            return {"slug": slug, "settlement": float(res.settlement)}
        return None
    except Exception as e:
        err_str = str(e).lower()
        if "404" in err_str or "not found" in err_str or "not settled" in err_str:
            return None
        log.warning("Settlement lookup failed for %s: %s", slug, e)
        return None

def get_events(limit: int = 100) -> list:
    """List events from Polymarket US (public gateway)."""
    _enforce_rate_limit(lane="reads")
    client = _get_unauth_client()
    try:
        res = client.events.list({"limit": limit})
        if isinstance(res, dict):
            return res.get("events", []) or []
        return getattr(res, "events", []) or []
    except Exception as e:
        log.warning("Poly get_events failed: %s", e)
        return []

def get_event_by_slug(slug: str) -> dict:
    """Retrieve full event detail including all child markets by slug."""
    _enforce_rate_limit(lane="reads")
    client = _get_unauth_client()
    try:
        res = client.events.retrieve_by_slug(slug)
        return res if isinstance(res, dict) else (res.model_dump() if hasattr(res, "model_dump") else {})
    except Exception as e:
        log.warning("Poly get_event_by_slug %s failed: %s", slug, e)
        return {}

def search_events(query: str) -> list:
    """Search Polymarket US events by keyword (e.g. BTC, ETH, SOL)."""
    _enforce_rate_limit(lane="bulk")
    client = _get_unauth_client()
    try:
        res = client.search.query({"query": query})
        if isinstance(res, dict):
            return res.get("events", []) or []
        return getattr(res, "events", []) or []
    except Exception as e:
        log.warning("Poly search_events %s failed: %s", query, e)
        return []

# ── Authenticated API (portfolio & balances) ─────────────────────────────

def _get_authenticated_client():
    """Return a configured client for authenticated calls, or raise RuntimeError."""
    client = _get_client()
    if client is None:
        raise RuntimeError(
            "Polymarket US client not configured: missing POLYMARKET_KEY_ID and/or "
            "POLYMARKET_SECRET_KEY (and no BW mirror files). "
            "Set env vars POLYMARKET_KEY_ID/POLYMARKET_SECRET_KEY or place "
            "Bitwarden mirror files at ~/.secrets/bw/polymarket_*.json."
        )
    return client

def get_portfolio_positions() -> dict:
    """Fetch portfolio positions (authenticated endpoint)."""
    _enforce_rate_limit(lane="reads")
    client = _get_authenticated_client()
    return client.get("/v1/portfolio/positions", authenticated=True)

def prewarm() -> bool:
    """L6: pre-warm the persistent HTTP session at startup.

    The SDK client is cached per process (connection pool reuse), but the
    first request still pays the TCP+TLS handshake. One cheap authenticated
    read at startup moves that cost off the order hot path. Never raises;
    False when there are no credentials.
    """
    try:
        client = _get_client()
        if client is None:
            return False
        client.get("/v1/account/balances", authenticated=True)
        return True
    except Exception as e:
        log.debug("HTTP pre-warm failed (non-fatal): %s", e)
        return False

def get_account_balances() -> dict:
    """Fetch account balances (authenticated endpoint)."""
    _enforce_rate_limit(lane="reads")
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

def health_check() -> bool:
    """Check connectivity to Polymarket US gateway."""
    try:
        client = _get_client() or PolymarketUS(key_id="", secret_key="")
        res = client.get("/v1/markets?limit=1", authenticated=False)
        return bool(res and isinstance(res, dict))
    except Exception as e:
        log.warning("Polymarket US health check failed: %s", e)
        return False

# Public aliases
get_client = _get_client
get_authenticated_client = _get_authenticated_client

def close() -> None:
    """Close the SDK client connections.

    Closes both authenticated and unauthenticated cached clients.
    """
    global _cached_client, _cached_unauth_client
    if _cached_client is not None:
        try:
            _cached_client.close()
        except Exception:
            pass
        _cached_client = None
    if _cached_unauth_client is not None:
        try:
            _cached_unauth_client.close()
        except Exception:
            pass
        _cached_unauth_client = None

# ── Authenticated API (order placement) ──────────────────────────────────

def _poly_amount(value: float) -> dict:
    """Build an Amount dict for the Polymarket US SDK."""
    return {"value": f"{value:.2f}", "currency": "USD"}

def _poly_price(side: str, price_dollars: float) -> float:
    """Convert a user-facing side price to the long-side (YES) price.

    Polymarket US `price.value` always represents the long side's price.
    Buying NO at price X means the YES token is priced at (1 - X), so
    price.value = 1 - X. Buying YES at price X means price.value = X.
    """
    if side == "yes":
        return price_dollars
    return round(1.0 - price_dollars, 4)

def _build_order_params(slug: str, side: str, price_dollars: float,
                        contracts: int, good_till_time: str | None = None) -> dict:
    """F3: Build order params dict shared by preview_order and place_maker_order.

    Single source of truth for order construction to prevent drift on money path.
    """
    intent = ("ORDER_INTENT_BUY_LONG" if side == "yes"
              else "ORDER_INTENT_BUY_SHORT")
    tif = "TIME_IN_FORCE_GOOD_TILL_DATE" if good_till_time else "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    params = {
        "marketSlug": slug,
        "intent": intent,
        "type": "ORDER_TYPE_LIMIT",
        "price": _poly_amount(_poly_price(side, price_dollars)),
        "quantity": contracts,
        "tif": tif,
        "participateDontInitiate": True,   # fee-free maker
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    }
    if good_till_time:
        params["goodTillTime"] = good_till_time
    return params

def preview_order(slug: str, side: str, price_dollars: float,
                  contracts: int, good_till_time: str | None = None) -> dict | None:
    """Validate an order against Polymarket US production order preview API.

    Args:
        slug: Market slug.
        side: "yes" or "no".
        price_dollars: Limit price in dollars (0-1).
        contracts: Number of contracts.
        good_till_time: Optional ISO-8601 UTC timestamp (e.g., "2026-11-19T15:30:00+00:00")
                        for TIME_IN_FORCE_GOOD_TILL_DATE. If None, uses GTC.

    Returns the preview response dict on success, or None on failure.
    """
    _enforce_rate_limit(lane="orders")
    client = _get_authenticated_client()
    params = _build_order_params(slug, side, price_dollars, contracts, good_till_time)
    try:
        resp = client.orders.preview({"request": params})
        # J4: Capture venue commission fields from preview response for fee model validation
        # The Order type in polymarket_us.types.orders has:
        #   - commissionsBasisPoints: str (total commission in bps)
        #   - makerCommissionsBasisPoints: str (maker commission in bps)
        if resp and "order" in resp:
            order = resp["order"]
            total_bps = order.get("commissionsBasisPoints")
            maker_bps = order.get("makerCommissionsBasisPoints")
            if total_bps is not None or maker_bps is not None:
                log.info("J4: Poly preview commission data for %s %s @ %.4f: "
                         "total_bps=%s, maker_bps=%s",
                         slug, side, price_dollars, total_bps, maker_bps)
        return resp
    except Exception as e:
        log.warning("Poly preview failed for %s %s @ %.4f: %s",
                    slug, side, price_dollars, e)
        return None

def place_maker_order(slug: str, side: str, price_dollars: float,
                      contracts: int, preview: bool = True,
                      good_till_time: str | None = None) -> dict | None:
    """Place a fee-free maker limit order on Polymarket US.

    Uses ``participateDontInitiate=True`` so the order provides liquidity and
    pays no taker fee. Intended for favourite-maker entries at 92-99c.

    Args:
        slug: Polymarket US market slug (e.g. ``cpc-btc-updown-15m-...``).
        side: ``"yes"`` (buy the Up/Yes token) or ``"no"`` (sell Yes / buy Down).
        price_dollars: Limit price in dollars (0–1).
        contracts: Number of contracts (integer).
        preview: If True, validate with preview API first.
        good_till_time: Optional ISO-8601 UTC timestamp for TIME_IN_FORCE_GOOD_TILL_DATE.
                        If None, uses GTC. If GTD is rejected and POLY_ALLOW_GTC_FALLBACK
                        is false, returns None and emits a critical event.

    Returns:
        The create response dict, or None on failure.
    """
    # Preview with GTD if requested
    if preview:
        preview_resp = preview_order(slug, side, price_dollars, contracts, good_till_time)
        if preview_resp is None:
            return None
        # Check if preview succeeded with GTD
        if good_till_time and preview_resp.get("tif") == "TIME_IN_FORCE_GOOD_TILL_DATE":
            pass  # GTD accepted
        elif good_till_time and not config.POLY_ALLOW_GTC_FALLBACK:
            # GTD not supported by venue and fallback disabled
            log.error("Poly GTD rejected for %s %s @ %.4f (fallback disabled)", slug, side, price_dollars)
            # Emit critical event for alerting
            try:
                import emit
                emit.emit_event("polymarket_us_client", "order.gtd_rejected",
                                {"slug": slug, "side": side, "price": price_dollars,
                                 "contracts": contracts, "good_till_time": good_till_time},
                                key=f"gtd_rejected:{slug}:{side}",
                                severity="critical")
            except Exception:
                pass
            return None

    _enforce_rate_limit(lane="orders")
    client = _get_authenticated_client()
    params = _build_order_params(slug, side, price_dollars, contracts, good_till_time)
    # Create: place the order.
    try:
        resp = client.orders.create(params)
        return resp
    except Exception as e:
        log.warning("Poly create failed for %s %s @ %.4f: %s",
                    slug, side, price_dollars, e)
        return None

def get_open_orders() -> list:
    """List open Polymarket US orders.

    Raises on venue/API failure. An empty list must mean "the venue reported
    zero open orders" — callers (reconcile) treat an absent order as
    cancelled, so a swallowed error here would release reserved cash for an
    order that may still be live.
    """
    _enforce_rate_limit(lane="orders")
    client = _get_authenticated_client()
    resp = client.orders.list()
    return resp.get("orders", []) or []

def cancel_order(order_id: str, market_slug: str = "") -> bool:
    """Cancel a Polymarket US order by id and market slug.

    If market_slug is not provided, looks up the order's marketSlug from
    get_open_orders() so callers don't fail with BadRequestError.
    """
    _enforce_rate_limit(lane="orders")
    client = _get_authenticated_client()
    if not market_slug:
        try:
            for o in get_open_orders():
                if o.get("id") == order_id:
                    market_slug = o.get("marketSlug", "")
                    break
        except Exception as e:
            log.warning("Poly cancel_order %s failed: could not list open orders: %s", order_id, e)
            return False
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
    """Close the position on a Polymarket US market.

    A4: Enforces rate limit like other order calls (orders lane).
    """
    _enforce_rate_limit(lane="orders")
    client = _get_authenticated_client()
    try:
        client.orders.close_position({"marketSlug": slug})
        return True
    except Exception as e:
        log.warning("Poly close_position %s failed: %s", slug, e)
        return False
