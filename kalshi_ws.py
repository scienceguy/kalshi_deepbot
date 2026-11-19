#!/usr/bin/env python3
"""kalshi_ws.py — Kalshi WebSocket streaming client.

Connects to wss://external-api-ws.kalshi.com/trade-api/ws/v2 (production)
or wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2 (demo).

Authentication: Same RSA-PSS signing as REST, sent as connection headers:
    KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE

Subscriptions (per Kalshi WS API):
- "ticker": real-time ticker updates (yes_bid_dollars, yes_ask_dollars, etc.)
- "orderbook_delta": incremental orderbook updates (requires snapshot first)
- "trade": public trade notifications
- "market_lifecycle_v2": market open/close/settlement events
- "fill": user fill notifications (private)
- "order": user order updates (private)
- "market_positions": user position updates (private)
"""

import asyncio
import base64
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse

import websockets

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import config

log = logging.getLogger(__name__)

# WebSocket URLs per environment
WS_URLS = {
    "production": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    "demo": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
}

# Kalshi WS message types (from actual API)
WS_CMD_SUBSCRIBE = "subscribe"
WS_CMD_UNSUBSCRIBE = "unsubscribe"
WS_MSG_TICKER = "ticker"
WS_MSG_ORDERBOOK_SNAPSHOT = "orderbook_snapshot"
WS_MSG_ORDERBOOK_DELTA = "orderbook_delta"
WS_MSG_TRADE = "trade"
WS_MSG_MARKET_LIFECYCLE = "market_lifecycle_v2"
WS_MSG_FILL = "fill"
WS_MSG_ORDER = "order"
WS_MSG_MARKET_POSITIONS = "market_positions"
WS_MSG_ERROR = "error"
WS_MSG_WELCOME = "welcome"
WS_MSG_SUBSCRIBED = "subscribed"
WS_MSG_UNSUBSCRIBED = "unsubscribed"


@dataclass
class MarketData:
    """Normalized market data from WebSocket ticker channel."""
    ticker: str
    yes_bid: Optional[float] = None      # yes_bid_dollars
    yes_ask: Optional[float] = None      # yes_ask_dollars
    no_bid: Optional[float] = None       # no_bid_dollars (complement)
    no_ask: Optional[float] = None       # no_ask_dollars (complement)
    last_price: Optional[float] = None   # last_price_dollars
    volume: Optional[float] = None
    open_interest: Optional[float] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class OrderbookSnapshot:
    """Orderbook snapshot from WebSocket."""
    ticker: str
    yes_levels: List[Dict[str, float]]  # [{"price": 0.95, "size": 100}, ...]
    no_levels: List[Dict[str, float]]
    timestamp: float = field(default_factory=time.time)


@dataclass
class OrderbookDelta:
    """Incremental orderbook delta from WebSocket."""
    ticker: str
    yes_changes: List[Dict[str, Any]]  # [{"price": 0.95, "size": 50, "side": "bid"}, ...]
    no_changes: List[Dict[str, Any]]
    timestamp: float = field(default_factory=time.time)


@dataclass
class FillData:
    """Normalized fill data from WebSocket."""
    fill_id: str
    order_id: str
    market_ticker: str
    side: str
    price: float
    size: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class OrderUpdate:
    """Normalized order update from WebSocket."""
    order_id: str
    market_ticker: str
    side: str
    price: float
    size: float
    status: str
    filled_size: float = 0.0
    timestamp: float = field(default_factory=time.time)


class KalshiWebSocket:
    """
    Kalshi WebSocket client with auto-reconnect and background thread.

    Usage:
        ws = KalshiWebSocket(key_id="...", key_file="~/.secrets/kalshi_private.pem")
        ws.set_market_callback(on_market_update)
        ws.set_fill_callback(on_fill)
        ws.start()
        ws.subscribe_ticker(["KXBTC15M-..."])
        ...
        ws.stop()
    """

    def __init__(
        self,
        *,
        key_id: Optional[str] = None,
        key_file: Optional[str] = None,
        environment: str = "production",
        private_key_content: Optional[str] = None,
        ping_interval: float = 20.0,
        reconnect_backoff: float = 5.0,
        max_reconnect_backoff: float = 60.0,
    ):
        """
        Initialize the WebSocket client.

        Args:
            key_id: KALSHI_KEY_ID (from config if not provided)
            key_file: Path to private key PEM (from config if not provided)
            environment: "production" or "demo"
            private_key_content: Inline PEM content (from config if not provided)
            ping_interval: Seconds between ping frames
            reconnect_backoff: Initial backoff on disconnect (seconds)
            max_reconnect_backoff: Maximum backoff cap
        """
        self.key_id = key_id or config.KALSHI_KEY_ID
        self.key_file = key_file or config.KALSHI_KEY_FILE
        self.environment = environment
        self.private_key_content = private_key_content or config.KALSHI_PRIVATE_KEY_CONTENT
        self.ws_url = WS_URLS.get(environment, WS_URLS["production"])

        self._privkey: Optional[rsa.RSAPrivateKey] = None
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._reconnect_backoff = reconnect_backoff
        self._max_reconnect_backoff = max_reconnect_backoff
        self._ping_interval = ping_interval

        # Callbacks
        self._ticker_callback: Optional[Callable[[MarketData], None]] = None
        self._orderbook_snapshot_callback: Optional[Callable[[OrderbookSnapshot], None]] = None
        self._orderbook_delta_callback: Optional[Callable[[OrderbookDelta], None]] = None
        self._trade_callback: Optional[Callable[[Dict], None]] = None
        self._market_lifecycle_callback: Optional[Callable[[Dict], None]] = None
        self._fill_callback: Optional[Callable[[FillData], None]] = None
        self._order_callback: Optional[Callable[[OrderUpdate], None]] = None
        self._market_positions_callback: Optional[Callable[[Dict], None]] = None
        self._connected_callback: Optional[Callable[[], None]] = None
        self._disconnected_callback: Optional[Callable[[Exception], None]] = None

        # State
        self._connected = False
        self._subscriptions: Dict[str, Set[str]] = {
            "ticker": set(),
            "orderbook_delta": set(),
            "trade": set(),
            "market_lifecycle_v2": set(),
            "fill": set(),
            "order": set(),
            "market_positions": set(),
        }

        # Caches
        self._ticker_cache: Dict[str, MarketData] = {}
        self._orderbook_cache: Dict[str, OrderbookSnapshot] = {}
        self._fill_cache: List[FillData] = []
        self._order_cache: Dict[str, OrderUpdate] = {}
        self._position_cache: Dict[str, Dict] = {}

    def _load_private_key(self) -> rsa.RSAPrivateKey:
        """Load RSA private key from file or inline content."""
        # Try file path first
        if self.key_file:
            pem_path = Path(self.key_file).expanduser()
            if pem_path.exists():
                mode = os.stat(str(pem_path)).st_mode & 0o777
                if mode & 0o077:
                    log.error(
                        "Kalshi private key file %s has insecure permissions (mode %o)",
                        pem_path, mode
                    )
                    raise PermissionError(f"Private key file {pem_path} is world-readable")
                pem_data = pem_path.read_bytes()
                return serialization.load_pem_private_key(pem_data, password=None)

        # Fall back to inline PEM content
        if self.private_key_content:
            pem_str = self.private_key_content.replace("\\n", "\n")
            if not pem_str.startswith("-----"):
                pem_str = (
                    "-----BEGIN RSA PRIVATE KEY-----\n"
                    + pem_str
                    + "\n-----END RSA PRIVATE KEY-----"
                )
            try:
                return serialization.load_pem_private_key(pem_str.encode(), password=None)
            except Exception as e:
                raise ValueError(f"Could not parse private key: {e}")

        raise RuntimeError("No private key found. Set key_file or private_key_content.")

    def _auth_headers(self) -> Dict[str, str]:
        """Build WebSocket authentication headers (same as REST but for WS path)."""
        ts_ms = str(int(time.time() * 1000))
        # WS path is /trade-api/ws/v2 (from the URL)
        parsed = urlparse(self.ws_url)
        sign_path = parsed.path
        msg = f"{ts_ms}GET{sign_path}".encode()
        sig = self._privkey.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256()
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    # ── Callback setters ──────────────────────────────────────────────

    def set_ticker_callback(self, callback: Callable[[MarketData], None]) -> None:
        """Set callback for ticker updates (market data)."""
        self._ticker_callback = callback

    def set_orderbook_snapshot_callback(self, callback: Callable[[OrderbookSnapshot], None]) -> None:
        """Set callback for orderbook snapshots."""
        self._orderbook_snapshot_callback = callback

    def set_orderbook_delta_callback(self, callback: Callable[[OrderbookDelta], None]) -> None:
        """Set callback for orderbook deltas."""
        self._orderbook_delta_callback = callback

    def set_trade_callback(self, callback: Callable[[Dict], None]) -> None:
        """Set callback for public trade notifications."""
        self._trade_callback = callback

    def set_market_lifecycle_callback(self, callback: Callable[[Dict], None]) -> None:
        """Set callback for market lifecycle events (open/close/settle)."""
        self._market_lifecycle_callback = callback

    def set_fill_callback(self, callback: Callable[[FillData], None]) -> None:
        """Set callback for fill notifications."""
        self._fill_callback = callback

    def set_order_callback(self, callback: Callable[[OrderUpdate], None]) -> None:
        """Set callback for order updates."""
        self._order_callback = callback

    def set_market_positions_callback(self, callback: Callable[[Dict], None]) -> None:
        """Set callback for position updates."""
        self._market_positions_callback = callback

    def set_connected_callback(self, callback: Callable[[], None]) -> None:
        """Set callback for connection established."""
        self._connected_callback = callback

    def set_disconnected_callback(self, callback: Callable[[Exception], None]) -> None:
        """Set callback for disconnection."""
        self._disconnected_callback = callback

    # ── Connection management ─────────────────────────────────────────

    def start(self) -> None:
        """Start the WebSocket background thread."""
        if self._thread and self._thread.is_alive():
            log.warning("WebSocket thread already running")
            return
        self._stop_event.clear()
        self._privkey = self._load_private_key()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="KalshiWS")
        self._thread.start()
        log.info("Kalshi WebSocket thread started")

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the WebSocket thread gracefully."""
        log.info("Stopping Kalshi WebSocket...")
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._connected = False
        log.info("Kalshi WebSocket stopped")

    def is_connected(self) -> bool:
        """Check if WebSocket is currently connected."""
        return self._connected

    # ── Subscription methods ──────────────────────────────────────────

    def subscribe_ticker(self, tickers: List[str]) -> None:
        """Subscribe to ticker updates for the given market tickers."""
        for ticker in tickers:
            self._subscriptions["ticker"].add(ticker)
        if self._connected:
            self._send_subscribe("ticker", list(tickers))

    def subscribe_orderbook_delta(self, tickers: List[str]) -> None:
        """Subscribe to orderbook delta updates for the given market tickers."""
        for ticker in tickers:
            self._subscriptions["orderbook_delta"].add(ticker)
        if self._connected:
            self._send_subscribe("orderbook_delta", list(tickers))

    def subscribe_trade(self, tickers: List[str]) -> None:
        """Subscribe to public trade notifications for the given market tickers."""
        for ticker in tickers:
            self._subscriptions["trade"].add(ticker)
        if self._connected:
            self._send_subscribe("trade", list(tickers))

    def subscribe_market_lifecycle(self, tickers: List[str]) -> None:
        """Subscribe to market lifecycle events for the given market tickers."""
        for ticker in tickers:
            self._subscriptions["market_lifecycle_v2"].add(ticker)
        if self._connected:
            self._send_subscribe("market_lifecycle_v2", list(tickers))

    def subscribe_fills(self) -> None:
        """Subscribe to user fill notifications (private channel)."""
        self._subscriptions["fill"].add("*")
        if self._connected:
            self._send_subscribe("fill", ["*"])

    def subscribe_orders(self) -> None:
        """Subscribe to user order updates (private channel)."""
        self._subscriptions["order"].add("*")
        if self._connected:
            self._send_subscribe("order", ["*"])

    def subscribe_market_positions(self) -> None:
        """Subscribe to user position updates (private channel)."""
        self._subscriptions["market_positions"].add("*")
        if self._connected:
            self._send_subscribe("market_positions", ["*"])

    def unsubscribe(self, channel: str, tickers: List[str]) -> None:
        """Unsubscribe from a channel for the given tickers."""
        for ticker in tickers:
            self._subscriptions[channel].discard(ticker)
        if self._connected:
            self._send_unsubscribe(channel, tickers)

    # ── Cache getters ─────────────────────────────────────────────────

    def get_ticker(self, ticker: str) -> Optional[MarketData]:
        """Get latest cached ticker data for a market."""
        return self._ticker_cache.get(ticker)

    def get_orderbook(self, ticker: str) -> Optional[OrderbookSnapshot]:
        """Get latest cached orderbook snapshot."""
        return self._orderbook_cache.get(ticker)

    def get_fills(self, since: Optional[float] = None) -> List[FillData]:
        """Get cached fills, optionally since a timestamp."""
        if since is None:
            return list(self._fill_cache)
        return [f for f in self._fill_cache if f.timestamp >= since]

    def get_order(self, order_id: str) -> Optional[OrderUpdate]:
        """Get latest cached order update."""
        return self._order_cache.get(order_id)

    def get_orders(self) -> List[OrderUpdate]:
        """Get all cached orders."""
        return list(self._order_cache.values())

    def get_position(self, ticker: str) -> Optional[Dict]:
        """Get latest cached position."""
        return self._position_cache.get(ticker)

    # ── Internal message sending ──────────────────────────────────────

    def _send_subscribe(self, channel: str, params: List[str]) -> None:
        """Send a subscribe message per Kalshi WS API format."""
        msg = {
            "cmd": WS_CMD_SUBSCRIBE,
            "params": {
                "channels": [channel],
                "market_tickers": params,
            }
        }
        if self._ws:
            asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(msg)), self._loop)

    def _send_unsubscribe(self, channel: str, params: List[str]) -> None:
        """Send an unsubscribe message per Kalshi WS API format."""
        msg = {
            "cmd": WS_CMD_UNSUBSCRIBE,
            "params": {
                "channels": [channel],
                "market_tickers": params,
            }
        }
        if self._ws:
            asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(msg)), self._loop)

    # ── Background thread loop ────────────────────────────────────────

    def _run_loop(self) -> None:
        """Main async loop running in background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ws_main())
        except Exception as e:
            log.error("WebSocket loop crashed: %s", e)
        finally:
            self._loop.close()

    async def _ws_main(self) -> None:
        """Main WebSocket connection and message handling."""
        backoff = self._reconnect_backoff
        while not self._stop_event.is_set():
            try:
                await self._connect_and_listen()
                backoff = self._reconnect_backoff  # Reset on successful connection
            except websockets.exceptions.ConnectionClosed as e:
                log.warning("WebSocket connection closed: %s", e)
            except Exception as e:
                log.error("WebSocket error: %s", e)
            finally:
                self._connected = False
                if self._disconnected_callback:
                    try:
                        self._disconnected_callback(e if 'e' in locals() else None)
                    except Exception:
                        pass

            if self._stop_event.is_set():
                break

            log.info("Reconnecting in %.1fs...", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_reconnect_backoff)

    async def _connect_and_listen(self) -> None:
        """Connect to WebSocket and listen for messages."""
        headers = self._auth_headers()
        log.info("Connecting to Kalshi WebSocket: %s", self.ws_url)

        async with websockets.connect(
            self.ws_url,
            additional_headers=headers,
            ping_interval=self._ping_interval,
            ping_timeout=10,
            close_timeout=5,
            max_size=2**20,  # 1MB max message
        ) as ws:
            self._ws = ws
            self._connected = True
            log.info("Kalshi WebSocket connected")

            # Resubscribe to all previously subscribed channels
            await self._resubscribe_all()

            if self._connected_callback:
                try:
                    self._connected_callback()
                except Exception:
                    pass

            # Message loop
            async for message in ws:
                if self._stop_event.is_set():
                    break
                await self._handle_message(message)

    async def _resubscribe_all(self) -> None:
        """Resubscribe to all previously subscribed channels."""
        for channel, params in self._subscriptions.items():
            if params:
                await self._ws.send(json.dumps({
                    "cmd": WS_CMD_SUBSCRIBE,
                    "params": {
                        "channels": [channel],
                        "market_tickers": list(params),
                    }
                }))
                log.debug("Resubscribed to %s: %s", channel, params)

    async def _handle_message(self, message: str) -> None:
        """Handle incoming WebSocket message per Kalshi WS API format."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError as e:
            log.warning("Failed to parse WS message: %s", e)
            return

        msg_type = data.get("type", "")

        if msg_type == WS_MSG_WELCOME:
            log.debug("WebSocket welcome: %s", data)
        elif msg_type == WS_MSG_SUBSCRIBED:
            log.info("Subscribed to %s: %s", data.get("channel"), data.get("market_tickers"))
        elif msg_type == WS_MSG_UNSUBSCRIBED:
            log.info("Unsubscribed from %s: %s", data.get("channel"), data.get("market_tickers"))
        elif msg_type == WS_MSG_ERROR:
            log.error("WebSocket error: %s", data)
        elif msg_type == WS_MSG_TICKER:
            await self._handle_ticker(data)
        elif msg_type == WS_MSG_ORDERBOOK_SNAPSHOT:
            await self._handle_orderbook_snapshot(data)
        elif msg_type == WS_MSG_ORDERBOOK_DELTA:
            await self._handle_orderbook_delta(data)
        elif msg_type == WS_MSG_TRADE:
            await self._handle_trade(data)
        elif msg_type == WS_MSG_MARKET_LIFECYCLE:
            await self._handle_market_lifecycle(data)
        elif msg_type == WS_MSG_FILL:
            await self._handle_fill(data)
        elif msg_type == WS_MSG_ORDER:
            await self._handle_order(data)
        elif msg_type == WS_MSG_MARKET_POSITIONS:
            await self._handle_market_positions(data)
        else:
            log.debug("Unknown WS message type: %s", msg_type)

    async def _handle_ticker(self, data: Dict) -> None:
        """Handle ticker market data update."""
        try:
            msg = data.get("msg", {})
            ticker = msg.get("market_ticker")
            if not ticker:
                return

            # Kalshi returns prices in dollars (e.g., yes_bid_dollars: 0.95)
            md = MarketData(
                ticker=ticker,
                yes_bid=msg.get("yes_bid_dollars"),
                yes_ask=msg.get("yes_ask_dollars"),
                no_bid=msg.get("no_bid_dollars"),
                no_ask=msg.get("no_ask_dollars"),
                last_price=msg.get("last_price_dollars"),
                volume=msg.get("volume"),
                open_interest=msg.get("open_interest"),
            )
            self._ticker_cache[ticker] = md

            if self._ticker_callback:
                try:
                    self._ticker_callback(md)
                except Exception as e:
                    log.warning("Ticker callback error: %s", e)
        except Exception as e:
            log.warning("Failed to handle ticker: %s", e)

    async def _handle_orderbook_snapshot(self, data: Dict) -> None:
        """Handle orderbook snapshot."""
        try:
            msg = data.get("msg", {})
            ticker = msg.get("market_ticker")
            if not ticker:
                return

            snapshot = OrderbookSnapshot(
                ticker=ticker,
                yes_levels=msg.get("yes", []),
                no_levels=msg.get("no", []),
            )
            self._orderbook_cache[ticker] = snapshot

            if self._orderbook_snapshot_callback:
                try:
                    self._orderbook_snapshot_callback(snapshot)
                except Exception as e:
                    log.warning("Orderbook snapshot callback error: %s", e)
        except Exception as e:
            log.warning("Failed to handle orderbook snapshot: %s", e)

    async def _handle_orderbook_delta(self, data: Dict) -> None:
        """Handle orderbook delta update."""
        try:
            msg = data.get("msg", {})
            ticker = msg.get("market_ticker")
            if not ticker:
                return

            delta = OrderbookDelta(
                ticker=ticker,
                yes_changes=msg.get("yes", []),
                no_changes=msg.get("no", []),
            )

            if self._orderbook_delta_callback:
                try:
                    self._orderbook_delta_callback(delta)
                except Exception as e:
                    log.warning("Orderbook delta callback error: %s", e)
        except Exception as e:
            log.warning("Failed to handle orderbook delta: %s", e)

    async def _handle_trade(self, data: Dict) -> None:
        """Handle public trade notification."""
        try:
            msg = data.get("msg", {})
            if self._trade_callback:
                try:
                    self._trade_callback(msg)
                except Exception as e:
                    log.warning("Trade callback error: %s", e)
        except Exception as e:
            log.warning("Failed to handle trade: %s", e)

    async def _handle_market_lifecycle(self, data: Dict) -> None:
        """Handle market lifecycle event (open/close/settle)."""
        try:
            msg = data.get("msg", {})
            if self._market_lifecycle_callback:
                try:
                    self._market_lifecycle_callback(msg)
                except Exception as e:
                    log.warning("Market lifecycle callback error: %s", e)
        except Exception as e:
            log.warning("Failed to handle market lifecycle: %s", e)

    async def _handle_fill(self, data: Dict) -> None:
        """Handle fill notification."""
        try:
            msg = data.get("msg", data)
            fd = FillData(
                fill_id=str(msg.get("fill_id") or msg.get("id")),
                order_id=str(msg.get("order_id")),
                market_ticker=msg.get("market_ticker") or msg.get("ticker"),
                side=msg.get("side", ""),
                price=float(msg.get("price", 0)),
                size=float(msg.get("size", 0)),
            )
            self._fill_cache.append(fd)
            if len(self._fill_cache) > 1000:
                self._fill_cache = self._fill_cache[-1000:]

            if self._fill_callback:
                try:
                    self._fill_callback(fd)
                except Exception as e:
                    log.warning("Fill callback error: %s", e)
        except Exception as e:
            log.warning("Failed to handle fill: %s", e)

    async def _handle_order(self, data: Dict) -> None:
        """Handle order update."""
        try:
            msg = data.get("msg", data)
            oid = str(msg.get("order_id") or msg.get("id"))
            ou = OrderUpdate(
                order_id=oid,
                market_ticker=msg.get("market_ticker") or msg.get("ticker"),
                side=msg.get("side", ""),
                price=float(msg.get("price", 0)),
                size=float(msg.get("size", 0)),
                status=msg.get("status", ""),
                filled_size=float(msg.get("filled_size", 0)),
            )
            self._order_cache[oid] = ou

            if self._order_callback:
                try:
                    self._order_callback(ou)
                except Exception as e:
                    log.warning("Order callback error: %s", e)
        except Exception as e:
            log.warning("Failed to handle order: %s", e)

    async def _handle_market_positions(self, data: Dict) -> None:
        """Handle position update."""
        try:
            msg = data.get("msg", data)
            ticker = msg.get("market_ticker") or msg.get("ticker")
            if ticker:
                self._position_cache[ticker] = msg
                if self._market_positions_callback:
                    try:
                        self._market_positions_callback(msg)
                    except Exception as e:
                        log.warning("Market positions callback error: %s", e)
        except Exception as e:
            log.warning("Failed to handle market positions: %s", e)


# Convenience function for getting a singleton instance (like the REST client)
_kalshi_ws_instance: Optional[KalshiWebSocket] = None
_kalshi_ws_lock = threading.Lock()


def get_kalshi_ws(**kwargs) -> KalshiWebSocket:
    """Get or create the singleton KalshiWebSocket instance."""
    global _kalshi_ws_instance
    with _kalshi_ws_lock:
        if _kalshi_ws_instance is None:
            _kalshi_ws_instance = KalshiWebSocket(**kwargs)
        return _kalshi_ws_instance


def close_kalshi_ws() -> None:
    """Close the singleton WebSocket instance."""
    global _kalshi_ws_instance
    with _kalshi_ws_lock:
        if _kalshi_ws_instance:
            _kalshi_ws_instance.stop()
            _kalshi_ws_instance = None


if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.INFO)
    ws = KalshiWebSocket(environment="demo")
    ws.set_ticker_callback(lambda m: print(f"Ticker: {m.ticker} yes_bid={m.yes_bid} yes_ask={m.yes_ask}"))
    ws.set_fill_callback(lambda f: print(f"Fill: {f.market_ticker} {f.side}@{f.price} x{f.size}"))
    ws.start()
    ws.subscribe_ticker(["KXBTC15M-26DEC3123-BTC"])
    ws.subscribe_fills()
    try:
        import time
        time.sleep(30)
    finally:
        ws.stop()