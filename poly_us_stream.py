"""
poly_us_stream.py — Polymarket US WebSocket streaming client.

Wraps the polymarket_us SDK WebSocket classes (MarketsWebSocket,
PrivateWebSocket) with a background asyncio thread, auto-reconnect, and
thread-safe caches for BBO (best bid/ask), orders, positions, and balances.

Follows the same background-thread pattern as binance_feed.py.

Never places orders; never uses --live. Credentials are read from
polymarket_us_client (config → env → BW mirror).

Usage:
    stream = PolyStream()
    stream.start()                     # background daemon thread
    stream.track("cpc-btc-updown-15m-...")
    bbo = stream.get_bbo("cpc-btc-updown-15m-...")
    stream.stop()
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from polymarket_us.websocket import MarketsWebSocket, PrivateWebSocket

import polymarket_us_client

log = logging.getLogger(__name__)

WS_RECONNECT_BASE = 1.0
WS_RECONNECT_MAX = 30.0
WS_SUBSCRIBE_BATCH = 50
WS_IDLE_POLL_SECONDS = 0.5

@dataclass
class BBO:
    """Best bid/ask for a market (long-side / YES prices)."""

    slug: str
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    updated_at: float = 0.0

class PolyStream:
    """WebSocket streaming client for Polymarket US market + private data.

    Runs in a background daemon thread with its own asyncio event loop.
    Callers read from thread-safe caches; the background thread keeps the
    WebSocket connections alive and pushes updates into the caches.

    Graceful degradation: without credentials the stream never connects and
    get_bbo() always returns None, so callers fall back to REST polling.
    """

    def __init__(self, key_id: str = "", secret_key: str = "", auto_start: bool = False):
        self.key_id = key_id
        self.secret_key = secret_key
        self._has_creds = bool(key_id and secret_key)

        # Thread-safe caches
        self._bbo: dict[str, BBO] = {}
        self._bbo_lock = threading.RLock()
        self._order_cache: dict[str, dict] = {}
        self._position_cache: dict[str, dict] = {}
        self._balance_cache: dict[str, dict] = {}
        self._private_lock = threading.RLock()

        # Lifecycle
        self._running = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = threading.Event()
        self._tracked_slugs: set[str] = set()
        self._tracked_slugs_lock = threading.RLock()
        # L5: last time any market/private message arrived. Dead-man's
        # switch: None (stream never expected) vs a stale timestamp.
        self.last_message_at: float = time.time()

        # WebSocket instances (set in _connect_and_subscribe)
        self._markets_ws = None
        self._private_ws = None

        if auto_start and self._has_creds:
            self.start()

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background websocket thread. Idempotent."""
        if self._running:
            return
        if not self._has_creds:
            log.warning("PolyStream: no Polymarket US credentials — stream disabled")
            return
        self._running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="poly-us-stream")
        self._thread.start()

    def stop(self) -> None:
        """Stop the background thread and close websockets."""
        self._running = False
        self._stop.set()
        if self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._close_ws(), self._loop).result(timeout=5)
            except Exception as e:
                log.debug("PolyStream close error: %s", e)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self._connected.clear()

    def _run_loop(self) -> None:
        """Background thread entry: runs asyncio event loop with reconnect."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ws_reconnect_loop())
        finally:
            self._loop.close()
            self._loop = None

    async def _ws_reconnect_loop(self) -> None:
        """Reconnect loop: connect, subscribe, idle until stopped or error."""
        backoff = WS_RECONNECT_BASE
        while self._running and not self._stop.is_set():
            try:
                await self._connect_and_subscribe()
                backoff = WS_RECONNECT_BASE
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("PolyStream websocket error: %s — reconnecting in %.1fs", e, backoff)
                self._connected.clear()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, WS_RECONNECT_MAX)

    async def _connect_and_subscribe(self) -> None:
        """Connect both websockets and subscribe to tracked slugs."""
        markets_ws = MarketsWebSocket(key_id=self.key_id, secret_key=self.secret_key)
        private_ws = PrivateWebSocket(key_id=self.key_id, secret_key=self.secret_key)
        await markets_ws.connect()
        await private_ws.connect()
        self._markets_ws = markets_ws
        self._private_ws = private_ws
        self._connected.set()

        # Message handlers
        markets_ws.on("market_data_lite", self._on_market_data_lite)
        markets_ws.on("trade", self._on_trade)
        markets_ws.on("error", self._on_ws_error)
        markets_ws.on("close", self._on_ws_close)
        private_ws.on("order_update", self._on_order_update)
        private_ws.on("position_update", self._on_position_update)
        private_ws.on("account_balance_update", self._on_balance_update)
        private_ws.on("error", self._on_ws_error)
        private_ws.on("close", self._on_ws_close)

        await self._resubscribe(markets_ws, private_ws)

        # Idle until connection drops or we're stopped
        while self._running and not self._stop.is_set():
            await asyncio.sleep(WS_IDLE_POLL_SECONDS)
            if not markets_ws.is_connected or not private_ws.is_connected:
                break

    async def _resubscribe(self, markets_ws, private_ws) -> None:
        """Subscribe to all tracked slugs (after connect/reconnect)."""
        with self._tracked_slugs_lock:
            slugs = list(self._tracked_slugs)
        if not slugs:
            return
        for i in range(0, len(slugs), WS_SUBSCRIBE_BATCH):
            batch = slugs[i : i + WS_SUBSCRIBE_BATCH]
            req_id = f"stream-{int(time.time() * 1000)}-{i}"
            await markets_ws.subscribe_market_data_lite(req_id, batch)
            await private_ws.subscribe_orders(req_id, batch)
            await private_ws.subscribe_positions(req_id, batch)
        await private_ws.subscribe_account_balance(f"stream-balance-{int(time.time() * 1000)}")

    def track(self, slug: str) -> None:
        """Add a slug to the streaming subscription set."""
        with self._tracked_slugs_lock:
            self._tracked_slugs.add(slug)
        if self._connected.is_set() and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._subscribe_slug(slug), self._loop)
            except Exception as e:
                log.debug("PolyStream track %s subscribe deferred: %s", slug, e)

    async def _subscribe_slug(self, slug: str) -> None:
        if not self._markets_ws or not self._private_ws:
            return
        req_id = f"stream-single-{int(time.time() * 1000)}"
        await self._markets_ws.subscribe_market_data_lite(req_id, [slug])
        await self._private_ws.subscribe_orders(req_id, [slug])
        await self._private_ws.subscribe_positions(req_id, [slug])

    # ── message handlers ─────────────────────────────────────────────

    def _mark_message(self) -> None:
        """Record that the feed delivered something (L5 dead-man switch)."""
        self.last_message_at = time.time()

    def _on_market_data_lite(self, message: dict) -> None:
        """Handle market data lite (best bid/ask) messages."""
        self._mark_message()
        lite = message.get("marketDataLite") or {}
        slug = lite.get("marketSlug")
        if not slug:
            return
        best_bid = lite.get("bestBid") or {}
        best_ask = lite.get("bestAsk") or {}
        yes_bid = self._amount_float(best_bid)
        yes_ask = self._amount_float(best_ask)
        with self._bbo_lock:
            self._bbo[slug] = BBO(
                slug=slug, yes_bid=yes_bid, yes_ask=yes_ask, updated_at=time.time()
            )

    @staticmethod
    def _amount_float(amount: dict | None) -> Optional[float]:
        if not amount or "value" not in amount:
            return None
        try:
            return float(amount["value"])
        except (TypeError, ValueError):
            return None

    def _on_trade(self, message: dict) -> None:
        self._mark_message()
        trade = message.get("trade") or {}
        slug = trade.get("marketSlug")
        if slug:
            log.debug(
                "PolyStream trade %s: %s @ %s",
                slug,
                trade.get("quantity"),
                trade.get("price"),
            )

    def _on_order_update(self, message: dict) -> None:
        """Handle order update messages from PrivateWebSocket.

        SDK Execution type has:
        - id: execution id
        - order: {id, marketSlug, state, cumQuantity, leavesQuantity, avgPx}
        - lastShares, lastPx, type
        """
        self._mark_message()
        update = message.get("orderSubscriptionUpdate") or {}
        execution = update.get("execution") or {}
        # Execution id (not order id)
        exec_id = execution.get("id")
        # Nested order object has the order details
        order = execution.get("order") or {}
        order_id = order.get("id")
        market_slug = order.get("marketSlug") or ""
        state = order.get("state") or ""  # ORDER_STATE_PENDING_NEW, ORDER_STATE_FILLED, etc.
        cum_qty = order.get("cumQuantity") or 0
        leaves_qty = order.get("leavesQuantity") or 0
        avg_px = order.get("avgPx")
        last_shares = execution.get("lastShares")
        last_px = execution.get("lastPx")
        exec_type = execution.get("type")

        if order_id:
            with self._private_lock:
                self._order_cache[str(order_id)] = {
                    "order_id": order_id,
                    "execution_id": exec_id,
                    "market_slug": market_slug,
                    "state": state,
                    "cum_quantity": cum_qty,
                    "leaves_quantity": leaves_qty,
                    "avg_price": avg_px,
                    "last_shares": last_shares,
                    "last_price": last_px,
                    "execution_type": exec_type,
                    "updated_at": time.time(),
                }
            # Call order state change callback outside the lock
            self._call_order_callback(self._order_cache[str(order_id)])

    def _on_position_update(self, message: dict) -> None:
        self._mark_message()
        update = message.get("positionSubscription") or {}
        after = update.get("afterPosition") or {}
        security_id = after.get("securityId") or ""
        if not security_id:
            before = update.get("beforePosition") or {}
            security_id = before.get("securityId") or ""
        if security_id:
            with self._private_lock:
                # Add updated_at for freshness tracking
                pos_data = dict(after)
                pos_data["updated_at"] = time.time()
                self._position_cache[str(security_id)] = pos_data

    def _on_balance_update(self, message: dict) -> None:
        self._mark_message()
        update = message.get("accountBalancesUpdate") or {}
        change = update.get("balanceChange") or {}
        after = change.get("afterBalance") or {}
        if after and after.get("currency"):
            with self._private_lock:
                # Add updated_at for freshness tracking
                bal_data = dict(after)
                bal_data["updated_at"] = time.time()
                self._balance_cache[after["currency"]] = bal_data

    def _on_ws_error(self, *args) -> None:
        log.warning("PolyStream websocket error: %s", args)

    def _on_ws_close(self, *args) -> None:
        self._connected.clear()

    async def _close_ws(self) -> None:
        if self._markets_ws:
            try:
                await self._markets_ws.close()
            except Exception:
                pass
        if self._private_ws:
            try:
                await self._private_ws.close()
            except Exception:
                pass

    # ── thread-safe reads ─────────────────────────────────────────────

    def get_bbo(self, slug: str) -> Optional[BBO]:
        """Return the latest cached BBO for a slug, or None."""
        with self._bbo_lock:
            b = self._bbo.get(slug)
            if b is None:
                return None
            return BBO(
                slug=b.slug, yes_bid=b.yes_bid, yes_ask=b.yes_ask, updated_at=b.updated_at
            )

    def get_all_bbos(self) -> dict[str, BBO]:
        with self._bbo_lock:
            return {
                s: BBO(slug=b.slug, yes_bid=b.yes_bid, yes_ask=b.yes_ask, updated_at=b.updated_at)
                for s, b in self._bbo.items()
            }

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def has_credentials(self) -> bool:
        return self._has_creds

    # J2: New methods to expose private stream data with freshness
    def get_order(self, order_id: str) -> Optional[dict]:
        """Return cached order by id with freshness age (seconds), or None."""
        with self._private_lock:
            cached = self._order_cache.get(order_id)
            if cached is None:
                return None
            result = dict(cached)
            result["age_seconds"] = time.time() - cached["updated_at"]
            return result

    def get_balance(self, currency: str = "USD") -> Optional[dict]:
        """Return cached balance by currency with freshness age, or None."""
        with self._private_lock:
            cached = self._balance_cache.get(currency)
            if cached is None:
                return None
            result = dict(cached)
            result["age_seconds"] = time.time() - cached.get("updated_at", 0) or 0
            return result

    def set_order_callback(self, callback) -> None:
        """Set a callback for order state changes: callback(order_dict).
        Called when an order update is processed. Non-raising."""
        self._order_callback = callback

    def _call_order_callback(self, order: dict) -> None:
        cb = getattr(self, "_order_callback", None)
        if cb:
            try:
                cb(order)
            except Exception as e:
                log.warning("PolyStream order callback failed: %s", e)

# Module-level singleton (created lazily by get_poly_stream)
_poly_stream: Optional[PolyStream] = None

def get_poly_stream() -> Optional[PolyStream]:
    """Return the module-level PolyStream, creating it if needed.

    Credentials are read from polymarket_us_client (config → env → BW mirror).
    Returns None when no credentials are configured — callers fall back to REST.
    """
    global _poly_stream
    if _poly_stream is None:
        key_id = polymarket_us_client._load_key_id()
        secret_key = polymarket_us_client._load_secret_key()
        _poly_stream = PolyStream(key_id=key_id, secret_key=secret_key)
    return _poly_stream
