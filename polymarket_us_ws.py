#!/usr/bin/env python3
"""polymarket_us_ws.py — Polymarket US WebSocket streaming client.

Connects to wss://api.polymarket.us/v1/ws/markets (public market data)
and wss://api.polymarket.us/v1/ws/private (private: orders, positions, balances).

Authentication: Ed25519 signature via polymarket_us.auth.create_auth_headers,
sent as connection headers.

Subscriptions (per Polymarket US WebSocket API):
- MARKET_DATA: full order book (bids, offers, stats)
- MARKET_DATA_LITE: best bid/ask + last trade
- TRADE: public trade notifications
- ORDER: user order updates (private)
- POSITION: user position updates (private)
- ACCOUNT_BALANCE: user balance updates (private)
- RFQ: RFQ events (private)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import config

log = logging.getLogger(__name__)

# WebSocket URLs per environment
WS_URLS = {
    "production": "wss://api.polymarket.us",
    "demo": "wss://api.polymarket.us",  # Same for demo
}

# Polymarket US WS message types
WS_PATH_MARKETS = "/v1/ws/markets"
WS_PATH_PRIVATE = "/v1/ws/private"

SUB_MARKET_DATA = "SUBSCRIPTION_TYPE_MARKET_DATA"
SUB_MARKET_DATA_LITE = "SUBSCRIPTION_TYPE_MARKET_DATA_LITE"
SUB_TRADE = "SUBSCRIPTION_TYPE_TRADE"
SUB_ORDER = "SUBSCRIPTION_TYPE_ORDER"
SUB_POSITION = "SUBSCRIPTION_TYPE_POSITION"
SUB_ACCOUNT_BALANCE = "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE"
SUB_RFQ = "SUBSCRIPTION_TYPE_RFQ"


@dataclass
class MarketData:
    """Normalized market data from WebSocket (SUBSCRIPTION_TYPE_MARKET_DATA)."""
    market_slug: str
    bids: List[Dict[str, float]]  # [{"px": 0.95, "qty": "100"}, ...]
    offers: List[Dict[str, float]]
    state: str
    stats: Optional[Dict] = None
    transact_time: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class MarketDataLite:
    """Normalized lite market data from WebSocket (SUBSCRIPTION_TYPE_MARKET_DATA_LITE)."""
    market_slug: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    last_trade_px: Optional[float] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class TradeData:
    """Normalized trade data from WebSocket (SUBSCRIPTION_TYPE_TRADE)."""
    market_slug: str
    price: float
    quantity: float
    trade_time: str
    maker_side: str
    taker_side: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class OrderUpdate:
    """Normalized order update from WebSocket (SUBSCRIPTION_TYPE_ORDER)."""
    order_id: str
    market_slug: str
    side: str
    price: float
    size: float
    status: str
    filled_size: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class PositionUpdate:
    """Normalized position update from WebSocket (SUBSCRIPTION_TYPE_POSITION)."""
    market_slug: str
    size: float
    avg_price: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class AccountBalanceUpdate:
    """Normalized account balance update from WebSocket (SUBSCRIPTION_TYPE_ACCOUNT_BALANCE)."""
    currency: str
    before: Optional[float] = None
    after: Optional[float] = None
    description: str = ""
    update_time: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class PolymarketUSWebSocket:
    """
    Polymarket US WebSocket client with auto-reconnect and background thread.

    Usage:
        ws = PolymarketUSWebSocket(
            key_id="...",
            secret_key="..."
        )
        ws.set_market_data_callback(on_market_data)
        ws.set_order_callback(on_order)
        ws.start()
        ws.subscribe_market_data(["cpc-btc-updown-15m-..."])
        ...
        ws.stop()
    """

    def __init__(
        self,
        *,
        key_id: Optional[str] = None,
        secret_key: Optional[str] = None,
        environment: str = "production",
        ping_interval: float = 20.0,
        reconnect_backoff: float = 5.0,
        max_reconnect_backoff: float = 60.0,
    ):
        """
        Initialize the WebSocket client.

        Args:
            key_id: POLYMARKET_KEY_ID (from config if not provided)
            secret_key: POLYMARKET_SECRET_KEY (from config if not provided)
            environment: "production" or "demo" (both use same WS URL)
            ping_interval: Seconds between ping frames
            reconnect_backoff: Initial backoff on disconnect (seconds)
            max_reconnect_backoff: Maximum backoff cap
        """
        self.key_id = key_id or config.POLYMARKET_KEY_ID
        self.secret_key = secret_key or config.POLYMARKET_SECRET_KEY
        self.environment = environment
        self.ws_base_url = WS_URLS.get(environment, WS_URLS["production"])
        self._ping_interval = ping_interval
        self._reconnect_backoff = reconnect_backoff
        self._max_reconnect_backoff = max_reconnect_backoff

        # Internal state
        self._markets_ws = None
        self._private_ws = None
        self._markets_thread: Optional[threading.Thread] = None
        self._private_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = False
        self._markets_connected = False
        self._private_connected = False

        # Callbacks
        self._market_data_callback: Optional[Callable[[MarketData], None]] = None
        self._market_data_lite_callback: Optional[Callable[[MarketDataLite], None]] = None
        self._trade_callback: Optional[Callable[[TradeData], None]] = None
        self._order_callback: Optional[Callable[[OrderUpdate], None]] = None
        self._position_callback: Optional[Callable[[PositionUpdate], None]] = None
        self._balance_callback: Optional[Callable[[AccountBalanceUpdate], None]] = None
        self._connected_callback: Optional[Callable[[], None]] = None
        self._disconnected_callback: Optional[Callable[[Exception], None]] = None

        # Caches
        self._market_data_cache: Dict[str, MarketData] = {}
        self._market_data_lite_cache: Dict[str, MarketDataLite] = {}
        self._trade_cache: List[TradeData] = []
        self._order_cache: Dict[str, OrderUpdate] = {}
        self._position_cache: Dict[str, PositionUpdate] = {}
        self._balance_cache: List[AccountBalanceUpdate] = {}

        # Subscription state
        self._market_subscriptions: Dict[str, Set[str]] = {
            "market_data": set(),
            "market_data_lite": set(),
            "trade": set(),
        }
        self._private_subscriptions: Dict[str, Set[str]] = {
            "order": set(),
            "position": set(),
            "account_balance": set(),
            "rfq": set(),
        }

    def _load_credentials(self) -> tuple[str, str]:
        """Load credentials from config or environment."""
        key_id = self.key_id
        secret_key = self.secret_key

        if not key_id:
            key_id = os.environ.get("POLYMARKET_KEY_ID", "")
        if not secret_key:
            secret_key = os.environ.get("POLYMARKET_SECRET_KEY", "")

        # Fall back to BW mirror
        if not key_id or not secret_key:
            from polymarket_us_client import _load_key_id, _load_secret_key
            if not key_id:
                key_id = _load_key_id()
            if not secret_key:
                secret_key = _load_secret_key()

        if not key_id or not secret_key:
            raise RuntimeError(
                "Polymarket US credentials not found. Set POLYMARKET_KEY_ID and "
                "POLYMARKET_SECRET_KEY in environment or ~/.secrets/bw/ mirror."
            )

        return key_id, secret_key

    # ── Callback setters ──────────────────────────────────────────────

    def set_market_data_callback(self, callback: Callable[[MarketData], None]) -> None:
        """Set callback for full order book updates."""
        self._market_data_callback = callback

    def set_market_data_lite_callback(self, callback: Callable[[MarketDataLite], None]) -> None:
        """Set callback for lightweight price updates."""
        self._market_data_lite_callback = callback

    def set_trade_callback(self, callback: Callable[[TradeData], None]) -> None:
        """Set callback for trade notifications."""
        self._trade_callback = callback

    def set_order_callback(self, callback: Callable[[OrderUpdate], None]) -> None:
        """Set callback for order updates."""
        self._order_callback = callback

    def set_position_callback(self, callback: Callable[[PositionUpdate], None]) -> None:
        """Set callback for position updates."""
        self._position_callback = callback

    def set_balance_callback(self, callback: Callable[[AccountBalanceUpdate], None]) -> None:
        """Set callback for balance updates."""
        self._balance_callback = callback

    def set_connected_callback(self, callback: Callable[[], None]) -> None:
        """Set callback for connection established."""
        self._connected_callback = callback

    def set_disconnected_callback(self, callback: Callable[[Exception], None]) -> None:
        """Set callback for disconnection."""
        self._disconnected_callback = callback

    # ── Connection management ─────────────────────────────────────────

    def start(self) -> None:
        """Start both WebSocket background threads."""
        if self._markets_thread and self._markets_thread.is_alive():
            log.warning("Markets WebSocket thread already running")
        else:
            self._stop_event.clear()
            self._markets_thread = threading.Thread(
                target=self._run_markets_loop, daemon=True, name="PolyUSWS-Markets"
            )
            self._markets_thread.start()

        if self._private_thread and self._private_thread.is_alive():
            log.warning("Private WebSocket thread already running")
        else:
            self._private_thread = threading.Thread(
                target=self._run_private_loop, daemon=True, name="PolyUSWS-Private"
            )
            self._private_thread.start()

        log.info("Polymarket US WebSocket threads started")

    def stop(self, timeout: float = 10.0) -> None:
        """Stop both WebSocket threads gracefully."""
        log.info("Stopping Polymarket US WebSocket...")
        self._stop_event.set()

        if self._markets_thread and self._markets_thread.is_alive():
            self._markets_thread.join(timeout=timeout)
        if self._private_thread and self._private_thread.is_alive():
            self._private_thread.join(timeout=timeout)

        self._markets_connected = False
        self._private_connected = False
        self._connected = False
        log.info("Polymarket US WebSocket stopped")

    def is_connected(self) -> bool:
        """Check if both WebSockets are currently connected."""
        return self._markets_connected and self._private_connected

    def is_markets_connected(self) -> bool:
        """Check if markets WebSocket is connected."""
        return self._markets_connected

    def is_private_connected(self) -> bool:
        """Check if private WebSocket is connected."""
        return self._private_connected

    # ── Subscription methods (markets) ────────────────────────────────

    def subscribe_market_data(self, market_slugs: List[str]) -> None:
        """Subscribe to full order book data for the given market slugs."""
        for slug in market_slugs:
            self._market_subscriptions["market_data"].add(slug)
        if self._markets_connected and self._markets_ws:
            self._send_subscribe_markets("market_data", market_slugs)

    def subscribe_market_data_lite(self, market_slugs: List[str]) -> None:
        """Subscribe to lightweight price data for the given market slugs."""
        for slug in market_slugs:
            self._market_subscriptions["market_data_lite"].add(slug)
        if self._markets_connected and self._markets_ws:
            self._send_subscribe_markets("market_data_lite", market_slugs)

    def subscribe_trades(self, market_slugs: List[str]) -> None:
        """Subscribe to trade notifications for the given market slugs."""
        for slug in market_slugs:
            self._market_subscriptions["trade"].add(slug)
        if self._markets_connected and self._markets_ws:
            self._send_subscribe_markets("trade", market_slugs)

    # ── Subscription methods (private) ────────────────────────────────

    def subscribe_orders(self, market_slugs: Optional[List[str]] = None) -> None:
        """Subscribe to user order updates (private channel)."""
        slugs = market_slugs or ["*"]
        for slug in slugs:
            self._private_subscriptions["order"].add(slug)
        if self._private_connected and self._private_ws:
            self._send_subscribe_private("order", slugs)

    def subscribe_positions(self, market_slugs: Optional[List[str]] = None) -> None:
        """Subscribe to user position updates (private channel)."""
        slugs = market_slugs or ["*"]
        for slug in slugs:
            self._private_subscriptions["position"].add(slug)
        if self._private_connected and self._private_ws:
            self._send_subscribe_private("position", slugs)

    def subscribe_account_balance(self) -> None:
        """Subscribe to user account balance updates (private channel)."""
        self._private_subscriptions["account_balance"].add("*")
        if self._private_connected and self._private_ws:
            self._send_subscribe_private("account_balance", ["*"])

    def subscribe_rfq(self) -> None:
        """Subscribe to RFQ events (private channel)."""
        self._private_subscriptions["rfq"].add("*")
        if self._private_connected and self._private_ws:
            self._send_subscribe_private("rfq", ["*"])

    def unsubscribe_markets(self, channel: str, market_slugs: List[str]) -> None:
        """Unsubscribe from a markets channel for the given slugs."""
        for slug in market_slugs:
            self._market_subscriptions.get(channel, set()).discard(slug)
        if self._markets_connected and self._markets_ws:
            # Note: unsubscribe requires request_id, which we track per-subscription
            pass  # TODO: implement unsubscribe with request_id tracking

    def unsubscribe_private(self, channel: str, market_slugs: List[str]) -> None:
        """Unsubscribe from a private channel for the given slugs."""
        for slug in market_slugs:
            self._private_subscriptions.get(channel, set()).discard(slug)
        # TODO: implement unsubscribe with request_id tracking

    # ── Cache getters ─────────────────────────────────────────────────

    def get_market_data(self, market_slug: str) -> Optional[MarketData]:
        """Get latest cached full order book data for a market."""
        return self._market_data_cache.get(market_slug)

    def get_market_data_lite(self, market_slug: str) -> Optional[MarketDataLite]:
        """Get latest cached lightweight price data for a market."""
        return self._market_data_lite_cache.get(market_slug)

    def get_trades(self, since: Optional[float] = None) -> List[TradeData]:
        """Get cached trades, optionally since a timestamp."""
        if since is None:
            return list(self._trade_cache)
        return [t for t in self._trade_cache if t.timestamp >= since]

    def get_order(self, order_id: str) -> Optional[OrderUpdate]:
        """Get latest cached order update."""
        return self._order_cache.get(order_id)

    def get_orders(self) -> List[OrderUpdate]:
        """Get all cached orders."""
        return list(self._order_cache.values())

    def get_position(self, market_slug: str) -> Optional[PositionUpdate]:
        """Get latest cached position."""
        return self._position_cache.get(market_slug)

    def get_balance(self, currency: str = "USD") -> Optional[AccountBalanceUpdate]:
        """Get latest cached balance for a currency."""
        for b in reversed(self._balance_cache):
            if b.currency == currency:
                return b
        return None

    # ── Internal message sending ──────────────────────────────────────

    def _send_subscribe_markets(self, channel: str, market_slugs: List[str]) -> None:
        """Send a subscribe message to markets WebSocket."""
        import uuid
        request_id = str(uuid.uuid4())
        sub_type_map = {
            "market_data": SUB_MARKET_DATA,
            "market_data_lite": SUB_MARKET_DATA_LITE,
            "trade": SUB_TRADE,
        }
        sub_type = sub_type_map.get(channel)
        if not sub_type:
            log.warning("Unknown markets subscription type: %s", channel)
            return

        msg = {
            "subscribe": {
                "requestId": request_id,
                "subscriptionType": sub_type,
                "marketSlugs": market_slugs,
            }
        }
        if self._markets_ws:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._markets_ws.send(msg), self._markets_loop
                )
            except Exception as e:
                log.warning("Failed to send markets subscribe: %s", e)

    def _send_subscribe_private(self, channel: str, market_slugs: List[str]) -> None:
        """Send a subscribe message to private WebSocket."""
        import uuid
        request_id = str(uuid.uuid4())
        sub_type_map = {
            "order": SUB_ORDER,
            "position": SUB_POSITION,
            "account_balance": SUB_ACCOUNT_BALANCE,
            "rfq": SUB_RFQ,
        }
        sub_type = sub_type_map.get(channel)
        if not sub_type:
            log.warning("Unknown private subscription type: %s", channel)
            return

        msg = {
            "subscribe": {
                "requestId": request_id,
                "subscriptionType": sub_type,
            }
        }
        if market_slugs and market_slugs != ["*"]:
            msg["subscribe"]["marketSlugs"] = market_slugs

        if self._private_ws:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._private_ws.send(msg), self._private_loop
                )
            except Exception as e:
                log.warning("Failed to send private subscribe: %s", e)

    # ── Background thread loops ───────────────────────────────────────

    def _run_markets_loop(self) -> None:
        """Main async loop for markets WebSocket running in background thread."""
        self._markets_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._markets_loop)
        try:
            self._markets_loop.run_until_complete(self._markets_main())
        except Exception as e:
            log.error("Markets WebSocket loop crashed: %s", e)
        finally:
            self._markets_loop.close()

    async def _markets_main(self) -> None:
        """Main markets WebSocket connection and message handling."""
        backoff = self._reconnect_backoff
        while not self._stop_event.is_set():
            try:
                await self._connect_and_listen_markets()
                backoff = self._reconnect_backoff  # Reset on successful connection
            except Exception as e:
                log.error("Markets WebSocket error: %s", e)
            finally:
                self._markets_connected = False
                if self._disconnected_callback:
                    try:
                        self._disconnected_callback(e if 'e' in locals() else None)
                    except Exception:
                        pass

            if self._stop_event.is_set():
                break

            log.info("Markets WebSocket reconnecting in %.1fs...", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_reconnect_backoff)

    async def _connect_and_listen_markets(self) -> None:
        """Connect to markets WebSocket and listen for messages."""
        from polymarket_us.websocket import MarketsWebSocket

        key_id, secret_key = self._load_credentials()

        log.info("Connecting to Polymarket US Markets WebSocket: %s%s", self.ws_base_url, WS_PATH_MARKETS)

        self._markets_ws = MarketsWebSocket(
            key_id=key_id,
            secret_key=secret_key,
            base_url=self.ws_base_url,
        )

        # Set up event handlers
        self._markets_ws.on("market_data", self._handle_market_data)
        self._markets_ws.on("market_data_lite", self._handle_market_data_lite)
        self._markets_ws.on("trade", self._handle_trade)
        self._markets_ws.on("open", self._on_markets_open)
        self._markets_ws.on("close", lambda: self._on_markets_close())
        self._markets_ws.on("error", lambda e: self._on_markets_error(e))

        await self._markets_ws.connect()
        self._markets_connected = True

        # Resubscribe to all previously subscribed channels
        await self._resubscribe_markets()

        if self._connected_callback:
            try:
                self._connected_callback()
            except Exception:
                pass

        # Keep the connection alive - the message loop runs in the background
        while self._markets_connected and not self._stop_event.is_set():
            await asyncio.sleep(1.0)

    def _on_markets_open(self) -> None:
        """Called when markets WebSocket connects."""
        self._markets_connected = True
        self._connected = self._markets_connected and self._private_connected
        log.info("Polymarket US Markets WebSocket connected")

    def _on_markets_close(self) -> None:
        """Called when markets WebSocket closes."""
        self._markets_connected = False
        self._connected = False

    def _on_markets_error(self, error: Exception) -> None:
        """Called when markets WebSocket errors."""
        log.error("Markets WebSocket error: %s", error)

    async def _resubscribe_markets(self) -> None:
        """Resubscribe to all previously subscribed markets channels."""
        if self._market_subscriptions["market_data"]:
            self._send_subscribe_markets("market_data", list(self._market_subscriptions["market_data"]))
        if self._market_subscriptions["market_data_lite"]:
            self._send_subscribe_markets("market_data_lite", list(self._market_subscriptions["market_data_lite"]))
        if self._market_subscriptions["trade"]:
            self._send_subscribe_markets("trade", list(self._market_subscriptions["trade"]))

    # ── Markets message handlers ──────────────────────────────────────

    def _handle_market_data(self, message: Dict) -> None:
        """Handle full order book message."""
        try:
            md = message.get("marketData", {})
            market_slug = md.get("marketSlug", "")
            if not market_slug:
                return

            bids = md.get("bids", [])
            offers = md.get("offers", [])
            state = md.get("state", "")
            stats = md.get("stats")
            transact_time = md.get("transactTime")

            data = MarketData(
                market_slug=market_slug,
                bids=bids,
                offers=offers,
                state=state,
                stats=stats,
                transact_time=transact_time,
            )
            self._market_data_cache[market_slug] = data

            if self._market_data_callback:
                try:
                    self._market_data_callback(data)
                except Exception as e:
                    log.warning("Market data callback error: %s", e)
        except Exception as e:
            log.warning("Failed to parse market_data: %s", e)

    def _handle_market_data_lite(self, message: Dict) -> None:
        """Handle lightweight price message."""
        try:
            md = message.get("marketDataLite", {})
            market_slug = md.get("marketSlug", "")
            if not market_slug:
                return

            best_bid = md.get("bestBid")
            best_ask = md.get("bestAsk")
            last_trade_px = md.get("lastTradePx")

            data = MarketDataLite(
                market_slug=market_slug,
                best_bid=float(best_bid) if best_bid is not None else None,
                best_ask=float(best_ask) if best_ask is not None else None,
                last_trade_px=float(last_trade_px) if last_trade_px is not None else None,
            )
            self._market_data_lite_cache[market_slug] = data

            if self._market_data_lite_callback:
                try:
                    self._market_data_lite_callback(data)
                except Exception as e:
                    log.warning("Market data lite callback error: %s", e)
        except Exception as e:
            log.warning("Failed to parse market_data_lite: %s", e)

    def _handle_trade(self, message: Dict) -> None:
        """Handle trade notification."""
        try:
            trade = message.get("trade", {})
            market_slug = trade.get("marketSlug", "")
            if not market_slug:
                return

            data = TradeData(
                market_slug=market_slug,
                price=float(trade.get("price", 0)),
                quantity=float(trade.get("quantity", 0)),
                trade_time=trade.get("tradeTime", ""),
                maker_side=trade.get("maker", {}).get("side", ""),
                taker_side=trade.get("taker", {}).get("side", ""),
            )
            self._trade_cache.append(data)
            # Keep only last 1000 trades
            if len(self._trade_cache) > 1000:
                self._trade_cache = self._trade_cache[-1000:]

            if self._trade_callback:
                try:
                    self._trade_callback(data)
                except Exception as e:
                    log.warning("Trade callback error: %s", e)
        except Exception as e:
            log.warning("Failed to parse trade: %s", e)

    # ── Private WebSocket loop ────────────────────────────────────────

    def _run_private_loop(self) -> None:
        """Main async loop for private WebSocket running in background thread."""
        self._private_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._private_loop)
        try:
            self._private_loop.run_until_complete(self._private_main())
        except Exception as e:
            log.error("Private WebSocket loop crashed: %s", e)
        finally:
            self._private_loop.close()

    async def _private_main(self) -> None:
        """Main private WebSocket connection and message handling."""
        backoff = self._reconnect_backoff
        while not self._stop_event.is_set():
            try:
                await self._connect_and_listen_private()
                backoff = self._reconnect_backoff
            except Exception as e:
                log.error("Private WebSocket error: %s", e)
            finally:
                self._private_connected = False
                if self._disconnected_callback:
                    try:
                        self._disconnected_callback(e if 'e' in locals() else None)
                    except Exception:
                        pass

            if self._stop_event.is_set():
                break

            log.info("Private WebSocket reconnecting in %.1fs...", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_reconnect_backoff)

    async def _connect_and_listen_private(self) -> None:
        """Connect to private WebSocket and listen for messages."""
        from polymarket_us.websocket import PrivateWebSocket

        key_id, secret_key = self._load_credentials()

        log.info("Connecting to Polymarket US Private WebSocket: %s%s", self.ws_base_url, WS_PATH_PRIVATE)

        self._private_ws = PrivateWebSocket(
            key_id=key_id,
            secret_key=secret_key,
            base_url=self.ws_base_url,
        )

        # Set up event handlers
        self._private_ws.on("order_snapshot", self._handle_order_snapshot)
        self._private_ws.on("order_update", self._handle_order_update)
        self._private_ws.on("position_snapshot", self._handle_position_snapshot)
        self._private_ws.on("position_update", self._handle_position_update)
        self._private_ws.on("account_balance_snapshot", self._handle_balance_snapshot)
        self._private_ws.on("account_balance_update", self._handle_balance_update)
        self._private_ws.on("rfq_event", self._handle_rfq_event)
        self._private_ws.on("open", self._on_private_open)
        self._private_ws.on("close", lambda: self._on_private_close())
        self._private_ws.on("error", lambda e: self._on_private_error(e))

        await self._private_ws.connect()
        self._private_connected = True

        # Resubscribe to all previously subscribed channels
        await self._resubscribe_private()

        # Keep the connection alive
        while self._private_connected and not self._stop_event.is_set():
            await asyncio.sleep(1.0)

    def _on_private_open(self) -> None:
        """Called when private WebSocket connects."""
        self._private_connected = True
        self._connected = self._markets_connected and self._private_connected
        log.info("Polymarket US Private WebSocket connected")

    def _on_private_close(self) -> None:
        """Called when private WebSocket closes."""
        self._private_connected = False
        self._connected = False

    def _on_private_error(self, error: Exception) -> None:
        """Called when private WebSocket errors."""
        log.error("Private WebSocket error: %s", error)

    async def _resubscribe_private(self) -> None:
        """Resubscribe to all previously subscribed private channels."""
        if self._private_subscriptions["order"]:
            self._send_subscribe_private("order", list(self._private_subscriptions["order"]))
        if self._private_subscriptions["position"]:
            self._send_subscribe_private("position", list(self._private_subscriptions["position"]))
        if self._private_subscriptions["account_balance"]:
            self._send_subscribe_private("account_balance", list(self._private_subscriptions["account_balance"]))
        if self._private_subscriptions["rfq"]:
            self._send_subscribe_private("rfq", list(self._private_subscriptions["rfq"]))

    # ── Private message handlers ──────────────────────────────────────

    def _handle_order_snapshot(self, message: Dict) -> None:
        """Handle order snapshot message."""
        try:
            snapshot = message.get("orderSubscriptionSnapshot") or message.get("ordersSnapshot", {})
            orders = snapshot.get("orders", [])
            for order in orders:
                self._process_order(order)
        except Exception as e:
            log.warning("Failed to parse order_snapshot: %s", e)

    def _handle_order_update(self, message: Dict) -> None:
        """Handle order update message."""
        try:
            update = message.get("orderSubscriptionUpdate") or message.get("orderUpdate", {})
            execution = update.get("execution", {})
            self._process_order(execution)
        except Exception as e:
            log.warning("Failed to parse order_update: %s", e)

    def _process_order(self, order: Dict) -> None:
        """Process and cache an order from snapshot or update."""
        try:
            order_id = order.get("id", "")
            market_slug = order.get("marketSlug", "")
            side = order.get("side", "")
            price = float(order.get("price", {}).get("value", 0)) if isinstance(order.get("price"), dict) else float(order.get("price", 0))
            size = float(order.get("quantity", 0))
            status = order.get("status", "")
            filled_size = float(order.get("filledQuantity", {}).get("value", 0)) if isinstance(order.get("filledQuantity"), dict) else float(order.get("filledQuantity", 0))

            data = OrderUpdate(
                order_id=order_id,
                market_slug=market_slug,
                side=side,
                price=price,
                size=size,
                status=status,
                filled_size=filled_size,
            )
            self._order_cache[order_id] = data

            if self._order_callback:
                try:
                    self._order_callback(data)
                except Exception as e:
                    log.warning("Order callback error: %s", e)
        except Exception as e:
            log.warning("Failed to process order: %s", e)

    def _handle_position_snapshot(self, message: Dict) -> None:
        """Handle position snapshot message."""
        try:
            snapshot = message.get("positionSubscriptionSnapshot") or message.get("positionsSnapshot", {})
            positions = snapshot.get("positions", {})
            for market_slug, pos in positions.items():
                self._process_position(market_slug, pos)
        except Exception as e:
            log.warning("Failed to parse position_snapshot: %s", e)

    def _handle_position_update(self, message: Dict) -> None:
        """Handle position update message."""
        try:
            # Handle both old and new format
            pos_sub = message.get("positionSubscription") or message.get("positionSubscriptionUpdate") or message.get("positionUpdate", {})
            after = pos_sub.get("afterPosition", {})
            market_slug = after.get("marketSlug", "") if after else ""
            if market_slug:
                self._process_position(market_slug, after)
        except Exception as e:
            log.warning("Failed to parse position_update: %s", e)

    def _process_position(self, market_slug: str, pos: Dict) -> None:
        """Process and cache a position."""
        try:
            size = float(pos.get("size", {}).get("value", 0)) if isinstance(pos.get("size"), dict) else float(pos.get("size", 0))
            avg_price = float(pos.get("avgPrice", {}).get("value", 0)) if isinstance(pos.get("avgPrice"), dict) else float(pos.get("avgPrice", 0))

            data = PositionUpdate(
                market_slug=market_slug,
                size=size,
                avg_price=avg_price,
            )
            self._position_cache[market_slug] = data

            if self._position_callback:
                try:
                    self._position_callback(data)
                except Exception as e:
                    log.warning("Position callback error: %s", e)
        except Exception as e:
            log.warning("Failed to process position: %s", e)

    def _handle_balance_snapshot(self, message: Dict) -> None:
        """Handle account balance snapshot message."""
        try:
            snapshot = message.get("accountBalancesSnapshot") or message.get("accountBalanceSubscriptionSnapshot", {})
            balances = snapshot.get("balances", [])
            for bal in balances:
                self._process_balance(bal)
        except Exception as e:
            log.warning("Failed to parse balance_snapshot: %s", e)

    def _handle_balance_update(self, message: Dict) -> None:
        """Handle account balance update message."""
        try:
            update = message.get("accountBalancesUpdate") or message.get("accountBalanceSubscriptionUpdate") or message.get("accountBalanceUpdate", {})
            balance_change = update.get("balanceChange", {})
            self._process_balance(balance_change.get("afterBalance", {}))
        except Exception as e:
            log.warning("Failed to parse balance_update: %s", e)

    def _process_balance(self, bal: Dict) -> None:
        """Process and cache a balance."""
        try:
            currency = bal.get("currency", "")
            current = float(bal.get("currentBalance", {}).get("value", 0)) if isinstance(bal.get("currentBalance"), dict) else float(bal.get("currentBalance", 0))

            data = AccountBalanceUpdate(
                currency=currency,
                after=current,
            )
            self._balance_cache.append(data)
            if len(self._balance_cache) > 1000:
                self._balance_cache = self._balance_cache[-1000:]

            if self._balance_callback:
                try:
                    self._balance_callback(data)
                except Exception as e:
                    log.warning("Balance callback error: %s", e)
        except Exception as e:
            log.warning("Failed to process balance: %s", e)

    def _handle_rfq_event(self, message: Dict) -> None:
        """Handle RFQ event message."""
        # RFQ events are not directly used by the trader but logged for debugging
        log.debug("RFQ event: %s", message)


# ── Module-level convenience functions ────────────────────────────────

_default_ws: Optional[PolymarketUSWebSocket] = None


def get_default_ws() -> PolymarketUSWebSocket:
    """Get or create the default Polymarket US WebSocket client."""
    global _default_ws
    if _default_ws is None:
        _default_ws = PolymarketUSWebSocket()
    return _default_ws


def start_default_ws(market_slugs: Optional[List[str]] = None) -> None:
    """Start the default WebSocket and optionally subscribe to market data."""
    ws = get_default_ws()
    ws.start()
    if market_slugs:
        ws.subscribe_market_data_lite(market_slugs)
        ws.subscribe_trades(market_slugs)


def stop_default_ws() -> None:
    """Stop the default WebSocket."""
    global _default_ws
    if _default_ws:
        _default_ws.stop()
        _default_ws = None