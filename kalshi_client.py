"""
kalshi_client.py — Signed Kalshi REST client using RSA-PSS authentication.
Handles auth headers, rate limiting, and all API calls the bot needs.
"""

import os
import stat
import time

# Kalshi's smallest tradable lot: fractional contracts down to 0.01.
MIN_CONTRACTS = 0.01
import base64
import logging
import threading
import requests
from pathlib import Path
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import config

log = logging.getLogger(__name__)

# Kalshi rate limit: ~10 req/s sustained; we stay well under it.
_MIN_REQUEST_GAP = config.REQUEST_MIN_GAP_SECONDS   # seconds between requests
# Balance endpoint hits stricter write bucket (100 tokens/s at Basic) — needs more gap
_MIN_BALANCE_GAP = config.REQUEST_MIN_GAP_SECONDS * 2  # 2x for balance endpoint


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
        # Validate that the base URL contains the expected API root so
        # signature paths are correct. Warn instead of fail to allow
        # non-standard URLs (e.g., local proxies) with explicit opt-in.
        if "/trade-api/v2" not in self.base_url:
            log.warning(
                "KALSHI_BASE_URL (%s) does not contain '/trade-api/v2' — "
                "signature paths may be incorrect. Set KALSHI_ALLOW_NONSTANDARD_URL=true "
                "to suppress this warning.",
                self.base_url,
            )
            if not getattr(config, "KALSHI_ALLOW_NONSTANDARD_URL", False):
                raise ValueError(
                    f"KALSHI_BASE_URL ({self.base_url}) must contain '/trade-api/v2' "
                    f"so that signature paths are computed correctly. "
                    f"Set KALSHI_ALLOW_NONSTANDARD_URL=true to override."
                )
        self._api_path  = urlparse(self.base_url).path
        self.key_id     = config.KALSHI_KEY_ID
        self._privkey   = self._load_private_key()
        self._last_req  = 0.0
        self._last_balance_req = 0.0  # Separate timer for balance endpoint
        self._lock      = threading.Lock()
        self.session    = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.session.mount('https://', HTTPAdapter(pool_connections=20, pool_maxsize=20))
        self._verified = False

    def _verify_key(self):
        """Confirm the loaded private key corresponds to KALSHI_KEY_ID.

        Makes a read-only markets request; if the key is wrong the API returns 401.
        Catches the mismatch at startup with a clear message instead of failing
        later with an opaque auth error on the first trade.
        Uses get_markets instead of get_balance because some key types lack
        portfolio/balance permission but can read public market data.

        Sets ``_verified`` only *after* the markets call succeeds so a transient
        startup failure (network blip, 429) doesn't permanently poison the client
        — the next request retries verification via ``_ensure_verified()``.
        Uses ``_verifying`` as a re-entrancy guard since ``get_markets`` itself
        calls ``_ensure_verified()``.
        """
        if getattr(self, "_verifying", False):
            return  # Already verifying — don't recurse
        self._verifying = True
        try:
            self.get_markets(limit=1)
        except Exception as e:
            self._verifying = False
            raise RuntimeError(
                f"Kalshi key verification failed: key_id={self.key_id} does not "
                f"match the loaded private key, or the key is invalid. "
                f"Original error: {e}"
            )
        self._verifying = False
        self._verified = True

    def _ensure_verified(self):
        """Verify key on first use rather than in constructor."""
        if not self._verified:
            self._verify_key()

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
        Signature = RSA-PSS(SHA-256, f"{timestamp}{method}{sign_path}")
        where sign_path is the FULL path from the API root (e.g.
        "/trade-api/v2/portfolio/balance") — Kalshi signs this, not just the
        request path. Query string is appended by requests separately and is
        not part of the signature.
        """
        ts_ms  = str(int(time.time() * 1000))
        sign_path = self._api_path + path
        msg    = f"{ts_ms}{method.upper()}{sign_path}".encode()
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
        # Verify key on first use (fails fast with clear error if key is wrong)
        self._ensure_verified()

        full_path = path if path.startswith("/") else f"/{path}"
        url       = self.base_url + full_path
        headers   = self._auth_header(method, full_path)

        # Default timeout: 10s connect/read; overridable per call
        if timeout is None:
            timeout = config.REQUEST_TIMEOUT_SECONDS
        kwargs.setdefault("timeout", timeout)

        for _attempt in range(5):
            # Rate limiting (thread-safe) — re-checked before every attempt
            # including 429 retries so backoff isn't bypassed.
            # Balance endpoint hits stricter write bucket — use separate timer
            is_balance = path == "/portfolio/balance"
            with self._lock:
                if is_balance:
                    gap = time.time() - self._last_balance_req
                    min_gap = _MIN_BALANCE_GAP
                else:
                    gap = time.time() - self._last_req
                    min_gap = _MIN_REQUEST_GAP
                if gap < min_gap:
                    time.sleep(min_gap - gap)
                if is_balance:
                    self._last_balance_req = time.time()
                else:
                    self._last_req = time.time()

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
        """Return available balance in dollars.

        Kalshi /portfolio/balance returns balance in cents. We divide by 100
        to get dollars. If Kalshi ever changes this field's units, all PnL
        and sizing math will be silently wrong — verify against a known
        value at startup if deploying to a new account.
        """
        data = self._request("GET", "/portfolio/balance")
        cents = data.get("balance", 0)
        dollars = cents / 100.0
        # Sanity guard: raw >$10B as cents, or <$0.01 after division — both
        # indicate a unit mismatch (dollars vs cents).
        if cents > 1_000_000_000 or (cents > 0 and dollars < 0.01):
            log.warning(
                "Balance raw value %s seems mislabeled (converted to $%.2f) — "
                "verify KALSHI balance units (cents vs dollars)",
                cents, dollars,
            )
        return dollars

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

    def invalidate_markets(self, series_ticker: str | None = None,
                           status: str | None = None) -> None:
        """Invalidate cached market data.

        Call this after order placement or settlement so that subsequent
        get_markets() calls fetch fresh data from the API rather than
        returning stale cached results.
        """
        if series_ticker is None and status is None:
            # Invalidate all markets
            self._markets_cache.clear()
        else:
            # Invalidate specific key(s)
            keys_to_remove = []
            for key in self._markets_cache:
                series, stat, limit = key
                if series_ticker is not None and series != series_ticker:
                    continue
                if status is not None and stat != status:
                    continue
                keys_to_remove.append(key)
            for key in keys_to_remove:
                del self._markets_cache[key]

    def get_market(self, ticker: str) -> dict:
        """Fetch a single market's details including expiration_value for settlement."""
        data = self._request("GET", f"/markets/{ticker}")
        if "market" not in data:
            log.warning("get_market(%s): API response missing 'market' key — "
                        "using whole response as fallback", ticker)
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

    @staticmethod
    def no_price_to_api_price(price_cents: int) -> str:
        """Convert NO side price from internal cents (1-99) to Kalshi API dollar string.

        Kalshi v2 API: buying NO = selling YES at complement price.
        Internal: NO at 85c means we want to buy NO at 85c.
        API: We sell YES at (100 - 85) = 15c = "0.1500".

        This complement is unconditional. NO_PRICE_IS_COMPLEMENT only describes
        how *market data* reports NO prices; it must not change order pricing.
        Sharing one flag made buys send the raw NO price on the YES leg (buying
        NO at 85c as "ask 85c" = buying NO at 15c) while sells used the
        complement, so at least one direction was always wrong.
        """
        if not 1 <= price_cents <= 99:
            raise ValueError(f"price_cents={price_cents} out of valid range (1-99)")
        return f"{(100 - price_cents) / 100:.4f}"

    @staticmethod
    def yes_price_to_api_price(price_cents: int) -> str:
        """Convert YES side price from internal cents (1-99) to Kalshi API dollar string."""
        if not 1 <= price_cents <= 99:
            raise ValueError(f"price_cents={price_cents} out of valid range (1-99)")
        return f"{price_cents / 100:.4f}"

    @staticmethod
    def api_price_to_internal_no(price_cents: int, is_complement: bool = True) -> int:
        """Convert NO side price from Kalshi API response to internal cents.

        Args:
            price_cents: Price from Kalshi API (1-99)
            is_complement: If True (default), Kalshi stores NO as complement (100 - internal).
                          If False, Kalshi stores NO price directly.
        Returns:
            Internal NO price in cents (1-99).
        """
        if not 1 <= price_cents <= 99:
            raise ValueError(f"price_cents={price_cents} out of valid range (1-99)")
        return 100 - price_cents if is_complement else price_cents

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

        if contracts < MIN_CONTRACTS:
            log.error("place_order: contracts=%s below Kalshi minimum %.2f, rejecting",
                      contracts, MIN_CONTRACTS)
            return None

        if side == "yes":
            api_side = "bid"
            price_dollars = self.yes_price_to_api_price(price_cents)
        else:
            # Buying NO = selling YES at the complement price
            api_side = "ask"
            price_dollars = self.no_price_to_api_price(price_cents)

        payload = {
            "ticker":                      ticker,
            "client_order_id":             client_order_id,
            "side":                        api_side,
            "count":                       f"{contracts:.2f}",
            "price":                       price_dollars,
            "time_in_force":               "good_till_canceled",
            "self_trade_prevention_type":  "taker_at_cross",
            # Maker orders must never cross: crossing would fill as a taker and
            # pay a fee the zero-maker-fee model does not budget for. Post-only
            # makes Kalshi reject the cross instead. Aggressive mode wants the
            # cross, so it opts out.
            "post_only":                   not config.AGGRESSIVE_FILL,
        }

        if config.DRY_RUN:
            log.info("[DRY RUN] %s", payload)
            return {"dry_run": True, "order_id": client_order_id}

        resp = self._request("POST", "/portfolio/events/orders", json=payload)
        # Normalise: v2 returns order_id at top level
        if resp and "order_id" not in resp and "order" in resp:
            resp["order_id"] = resp["order"].get("order_id", "")
        return resp

    def sell_position(self, ticker: str, side: str, contracts: int,
                      price_cents: int, client_order_id: str = "",
                      time_in_force: str = "immediate_or_cancel") -> dict:
        """Close a position by selling it back at price_cents (the current bid).

        This is a taker order: a sell priced AT the best bid crosses the spread
        and matches it, so it cannot rest as a maker — Kalshi would reject a
        post_only order like that, and the exit would never happen. A maker sell
        would have to rest at or above the ask, which only fills if the market
        moves further in our favour, i.e. it caps the position instead of
        closing it. Taker it is, and _book_exit charges the taker fee to match.

        Kalshi's unified book quotes the YES leg: selling YES is an ask at the
        YES price, selling NO is a bid at (100 - NO price). Sending the NO price
        on the YES leg would invert the order, so the complement is applied here.
        """
        if not 1 <= price_cents <= 99:
            raise ValueError(f"sell_position: price_cents={price_cents} out of range (1-99)")
        if contracts < MIN_CONTRACTS:
            raise ValueError(
                f"sell_position: contracts={contracts} below Kalshi minimum {MIN_CONTRACTS}")

        if side == "yes":
            api_side, yes_leg_cents = "ask", price_cents
        elif side == "no":
            api_side, yes_leg_cents = "bid", 100 - price_cents
        else:
            raise ValueError(f"sell_position: side must be 'yes' or 'no', got {side!r}")

        payload = {
            "ticker":                      ticker,
            "client_order_id":             client_order_id or f"exit-{ticker}-{int(time.time())}",
            "side":                        api_side,
            "count":                       f"{contracts:.2f}",
            "price":                       f"{yes_leg_cents / 100:.4f}",
            "time_in_force":               time_in_force,
            "self_trade_prevention_type":  "taker_at_cross",
            # post_only only when it is genuinely resting; an exit crosses.
            "post_only":                   time_in_force != "immediate_or_cancel",
        }

        if config.DRY_RUN:
            log.info("[DRY RUN] sell %s", payload)
            return {"dry_run": True, "order_id": payload["client_order_id"]}

        resp = self._request("POST", "/portfolio/events/orders", json=payload)
        if resp and "order_id" not in resp and "order" in resp:
            resp["order_id"] = resp["order"].get("order_id", "")
        return resp

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order by order_id."""
        if config.DRY_RUN:
            log.info("[DRY RUN] Would cancel order: %s", order_id)
            return {"dry_run": True}
        try:
            return self._request("DELETE", f"/portfolio/events/orders/{order_id}")
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

    # ── Historical Markets (no auth required) ───────────────────────────

    def get_historical_markets(self, series_ticker: str = "",
                               limit: int = 100,
                               cursor: str = "") -> dict:
        """Fetch settled markets older than the cutoff from the historical tier.

        GET /historical/markets — returns settled markets with expiration_value,
        close_time, floor_strike, and other settlement fields.

        Args:
            series_ticker: Filter by series (e.g. "KXBTC15M").
            limit: Max results per page.
            cursor: Pagination cursor from a previous response.

        Returns dict with "markets" list and optional "cursor" for next page.
        """
        params = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/historical/markets", params=params)

    def fetch_all_historical_markets(self, series_ticker: str = "",
                                     limit: int = 100,
                                     page_size: int = 100) -> list:
        """Fetch all settled markets from the historical tier with cursor pagination.

        Convenience wrapper around ``get_historical_markets`` that follows
        cursors until either ``limit`` markets are collected or no more pages
        exist.

        Args:
            series_ticker: Filter by series (e.g. "KXBTC15M").
            limit: Max total results to collect.
            page_size: Results per API page (max 100).

        Returns list of market dicts.
        """
        fetched = []
        cursor = ""
        while len(fetched) < limit:
            page = min(page_size, limit - len(fetched))
            data = self.get_historical_markets(
                series_ticker=series_ticker, limit=page, cursor=cursor
            )
            page_markets = data.get("markets", [])
            if not page_markets:
                break
            fetched.extend(page_markets)
            cursor = data.get("cursor", "")
            if not cursor:
                break
        return fetched

    def close(self):
        """Close the underlying requests session to release sockets."""
        self.session.close()

    def get_market_candlesticks(self, ticker: str,
                                start_ts: int,
                                end_ts: int,
                                period_interval: int = 1,
                                limit: int = 100) -> dict:
        """Fetch candlestick (OHLCV) data for a historical market.

        GET /historical/markets/{ticker}/candlesticks — returns time-series
        price candles. Valid period_interval values: 1 (1 minute), 60 (1 hour),
        1440 (1 day).

        Args:
            ticker: Market ticker (e.g. "KXBTC15M-26NOV011145-45").
            start_ts: Start timestamp in seconds (UTC epoch).
            end_ts: End timestamp in seconds (UTC epoch).
            period_interval: Candle length in minutes (1, 60, or 1440).
            limit: Max candles to return.

        Returns dict with candlestick data.
        """
        path = f"/historical/markets/{ticker}/candlesticks"
        params = {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_interval,
            "limit": limit,
        }
        return self._request("GET", path, params=params)
