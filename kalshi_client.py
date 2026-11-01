"""
kalshi_client.py — Signed Kalshi REST client using RSA-PSS authentication.
Handles auth headers, rate limiting, and all API calls the bot needs.
"""

import os
import stat
import time
import base64
import logging
import requests
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.backends import default_backend

import config

log = logging.getLogger(__name__)

# Kalshi rate limit: ~10 req/s sustained; we stay well under it.
_MIN_REQUEST_GAP = 0.12   # seconds between requests


class KalshiAPIError(Exception):
    """Raised when the Kalshi API returns an error status code."""
    pass


class KalshiClient:
    """
    Minimal signed REST client for Kalshi v2 API.
    All methods return parsed JSON dicts or raise on HTTP error.
    """

    def __init__(self):
        self.base_url   = config.KALSHI_BASE_URL
        self.key_id     = config.KALSHI_KEY_ID
        self._privkey   = self._load_private_key()
        self._last_req  = 0.0
        self.session    = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        # Verify the loaded key matches KALSHI_KEY_ID by making a lightweight call.
        # Fails fast at startup instead of producing opaque auth errors later.
        self._verify_key()

    def _verify_key(self):
        """Confirm the loaded private key corresponds to KALSHI_KEY_ID.

        Makes a read-only balance request; if the key is wrong the API returns 401.
        Catches the mismatch at startup with a clear message instead of failing
        later with an opaque auth error on the first trade.
        """
        try:
            self.get_balance()
        except Exception as e:
            raise RuntimeError(
                f"Kalshi key verification failed: key_id={self.key_id} does not "
                f"match the loaded private key, or the key is invalid. "
                f"Original error: {e}"
            )
        self.session.mount('https://', HTTPAdapter(pool_connections=20, pool_maxsize=20))

    # ── Key Loading ───────────────────────────────────────────────────────────

    def _load_private_key(self) -> rsa.RSAPrivateKey:
        """Load RSA private key from file path or env content."""
        # Try file path first
        if config.KALSHI_KEY_FILE:
            pem_path = Path(config.KALSHI_KEY_FILE).expanduser()
            if pem_path.exists():
                # Refuse world-readable key files — any "other" permission bit leaks the key
                mode = stat.S_IMODE(os.stat(str(pem_path)).st_mode)
                if mode & 0o077:
                    log.error(
                        "Kalshi private key file %s has insecure permissions (mode %o) — "
                        "refusing to load. Set chmod 600 on the key file.",
                        pem_path, mode,
                    )
                    raise PermissionError(
                        f"Private key file {pem_path} is world-readable (mode {mode}). "
                        f"Run: chmod 600 {pem_path}"
                    )
                pem_data = pem_path.read_bytes()
                return serialization.load_pem_private_key(
                    pem_data, password=None, backend=None
                )

        # Fall back to inline PEM content from env
        if config.KALSHI_PRIVATE_KEY_CONTENT:
            pem_str = config.KALSHI_PRIVATE_KEY_CONTENT
            # Normalise escaped newlines that sometimes appear in .env values
            pem_str = pem_str.replace("\\n", "\n")
            if not pem_str.startswith("-----"):
                pem_str = (
                    "-----BEGIN RSA PRIVATE KEY-----\n"
                    + pem_str
                    + "\n-----END RSA PRIVATE KEY-----"
                )
            try:
                return serialization.load_pem_private_key(
                    pem_str.encode(), password=None, backend=None
                )
            except Exception as e:
                raise ValueError(
                    "KALSHI_PRIVATE_KEY is set but could not be parsed as a valid "
                    "RSA private key. Check the key format and ensure it begins with "
                    "'-----BEGIN RSA PRIVATE KEY-----'. Original error: %s" % e
                )

        raise RuntimeError(
            "No private key found. Set KALSHI_KEY_FILE or KALSHI_PRIVATE_KEY in env."
        )

    # ── Auth Header ───────────────────────────────────────────────────────────

    def _auth_header(self, method: str, path: str) -> dict:
        """
        Build the KALSHI-ACCESS-* headers required for every request.
        Signature = RSA-PSS(SHA-256, f"{timestamp}{method}{path}")
        """
        ts_ms  = str(int(time.time() * 1000))
        # Strip query string from path for signature
        sig_path = path.split("?")[0]
        msg    = f"{ts_ms}{method.upper()}{sig_path}".encode()
        sig    = self._privkey.sign(msg, padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ), hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY":       self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    # ── Rate-limited Request ──────────────────────────────────────────────────

    def _request(self, method: str, path: str, timeout: int = None, **kwargs) -> dict:
        """Execute a rate-limited, signed request. Returns parsed JSON."""
        # Rate limiting
        gap = time.time() - self._last_req
        if gap < _MIN_REQUEST_GAP:
            time.sleep(_MIN_REQUEST_GAP - gap)

        full_path = path if path.startswith("/") else f"/{path}"
        url       = self.base_url + full_path
        headers   = self._auth_header(method, urlparse(url).path)

        # Default timeout: 10s connect/read; overridable per call
        if timeout is None:
            timeout = config.REQUEST_TIMEOUT_SECONDS
        kwargs.setdefault("timeout", timeout)

        self._last_req = time.time()
        for _attempt in range(5):
            try:
                resp = self.session.request(method, url, headers=headers, **kwargs)
            except requests.RequestException as e:
                log.warning("HTTP error %s %s: %s", method, path, e)
                raise
            if resp.status_code != 429:
                break
            try:
                wait = float(resp.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                wait = 0.0
            wait = wait or 0.7 * (2 ** _attempt)
            log.warning("429 %s %s — backing off %.1fs (attempt %d/5)", method, path, wait, _attempt + 1)
            time.sleep(wait)

        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 404:
            log.warning("404 %s %s — resource not found", method, path)
            raise KalshiAPIError(f"404 {method} {path}: resource not found")
        elif resp.status_code == 409:
            log.warning("409 conflict %s %s: %s", method, path, resp.text[:200])
            raise KalshiAPIError(f"409 {method} {path}: {resp.text[:200]}")
        else:
            log.error("HTTP %s %s %s — %s", resp.status_code, method, path, resp.text[:300])
            resp.raise_for_status()

    # ── Account ───────────────────────────────────────────────────────────────

    # Alias used by some modules
    def _req(self, method, path, **kw): return self._request(method, path, **kw)

    def get_balance(self) -> float:
        """Return available balance in dollars."""
        data = self._request("GET", "/portfolio/balance")
        cents = data.get("balance", 0)
        return cents / 100.0

    def get_positions(self) -> list:
        """Return list of open market positions."""
        data = self._request("GET", "/portfolio/positions")
        return data.get("market_positions", [])

    # ── Markets ───────────────────────────────────────────────────────────────

    _markets_cache: dict = {}  # (series_ticker, status, limit) -> (timestamp, data)

    def get_markets(self, series_ticker: str = "", status: str = "open",
                    limit: int = 100) -> list:
        """Fetch open markets, optionally filtered by series ticker prefix.

        Results are cached for 30s to avoid redundant API calls when the
        same series is queried by multiple callers (e.g. find_active_markets
        and data_collector).
        """
        key = (series_ticker, status, limit)
        now = time.time()
        cached = self._markets_cache.get(key)
        if cached and now - cached[0] < 30:
            return cached[1]

        params = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        data = self._request("GET", "/markets", params=params)
        result = data.get("markets", [])
        self._markets_cache[key] = (now, result)
        return result

    def get_market(self, ticker: str) -> dict:
        """Fetch a single market's details including expiration_value for settlement."""
        data = self._request("GET", f"/markets/{ticker}")
        return data.get("market", data)

    def get_orderbook(self, ticker: str, depth: int = 5) -> dict:
        """
        Fetch the current orderbook for a market.
        Returns {"yes": [...], "no": [...]} each sorted best-first.
        Each entry is [price_cents, quantity].
        """
        if depth < 1:
            log.warning("get_orderbook(%s): depth=%s below 1, clamping to 1", ticker, depth)
            depth = 1
        elif depth > 50:
            log.warning("get_orderbook(%s): depth=%s above 50, clamping to 50", ticker, depth)
            depth = 50

        data = self._request("GET", f"/markets/{ticker}/orderbook",
                              params={"depth": depth})
        if not isinstance(data, dict):
            log.warning("get_orderbook(%s): unexpected response type %s, returning empty book",
                        ticker, type(data).__name__)
            return {"yes": [], "no": []}

        ob = data.get("orderbook", data.get("orderbook_fp", {}))
        if not isinstance(ob, dict):
            log.warning("get_orderbook(%s): orderbook is not a dict (%s), returning empty book",
                        ticker, type(ob).__name__)
            return {"yes": [], "no": []}

        def price_cents(v):
            """*_dollars levels arrive as decimal strings ('0.9400') → cents."""
            try:
                f = float(v)
                return int(round(f * 100)) if f <= 1.0 else int(round(f))
            except (TypeError, ValueError):
                log.warning("get_orderbook(%s): unparseable price %r, skipping entry", ticker, v)
                return None

        def parse_side(raw):
            """Convert Kalshi orderbook side to [[price_cents, qty], ...]."""
            if not raw:
                return []
            result = []
            for entry in raw:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    pc = price_cents(entry[0])
                    if pc is None:
                        continue
                    try:
                        q = int(float(entry[1]))
                    except (TypeError, ValueError):
                        continue
                    result.append([pc, q])
                elif isinstance(entry, dict):
                    p = entry.get("price", entry.get("yes_price", 0))
                    q = entry.get("quantity", entry.get("contracts", 0))
                    pc = price_cents(p)
                    if pc is None:
                        continue
                    try:
                        q = int(float(q))
                    except (TypeError, ValueError):
                        continue
                    result.append([pc, q])
            return result

        yes_raw = ob.get("yes", ob.get("yes_dollars", []))
        no_raw  = ob.get("no",  ob.get("no_dollars",  []))

        # Kalshi returns levels ascending; the best bid is the highest price
        return {
            "yes": sorted(parse_side(yes_raw), key=lambda x: x[0], reverse=True),
            "no":  sorted(parse_side(no_raw),  key=lambda x: x[0], reverse=True),
        }

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_order(self, ticker: str, side: str, contracts: int,
                    price_cents: int, client_order_id: str,
                    order_type: str = "limit") -> dict:
        """
        Place a limit order via Kalshi v2 API.
        side: "yes" or "no"
        price_cents: 1-99
        V2 API: side="bid" buys YES, side="ask" sells YES (= buys NO).
        Price is a dollar string e.g. "0.6100".
        """
        if price_cents < 1 or price_cents > 99:
            log.error("place_order: price_cents=%s out of valid range (1-99), rejecting", price_cents)
            return None

        if contracts <= 0:
            log.error("place_order: contracts=%s must be > 0, rejecting", contracts)
            return None

        if side == "yes":
            api_side      = "bid"
            price_dollars = f"{price_cents / 100:.4f}"
        else:
            # Buying NO = selling YES at the complement price
            api_side      = "ask"
            price_dollars = f"{(100 - price_cents) / 100:.4f}"

        payload = {
            "ticker":                      ticker,
            "client_order_id":             client_order_id,
            "side":                        api_side,
            "count":                       str(int(contracts)),
            "price":                       price_dollars,
            "time_in_force":               "good_till_canceled",
            "self_trade_prevention_type":  "taker_at_cross",
        }

        if config.DRY_RUN:
            log.info("[DRY RUN] %s", payload)
            return {"dry_run": True, "order_id": client_order_id}

        resp = self._request("POST", "/portfolio/orders", json=payload)
        # Normalise: v2 returns order_id at top level
        if resp and "order_id" not in resp and "order" in resp:
            resp["order_id"] = resp["order"].get("order_id", "")
        return resp

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order by order_id."""
        if config.DRY_RUN:
            log.info("[DRY RUN] Would cancel order: %s", order_id)
            return {"dry_run": True}
        try:
            return self._request("DELETE", f"/portfolio/orders/{order_id}")
        except KalshiAPIError as e:
            if "404" in str(e):
                log.debug("cancel_order %s: order not found (already filled/cancelled)", order_id)
                return {"cancelled": False, "reason": "not_found"}
            raise

    def get_orders(self, status: str = "resting") -> list:
        """Return open/resting orders."""
        data = self._request("GET", "/portfolio/orders",
                              params={"status": status})
        return data.get("orders", [])
