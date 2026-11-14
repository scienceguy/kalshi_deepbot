"""
polymarket_ws.py — Resilient, auto-reconnecting async WebSocket worker for
Polymarket US live top-of-book quotes.

Streams SUBSCRIPTION_TYPE_MARKET_DATA_LITE (best bid/ask) from
wss://api.polymarket.us/v1/ws/markets for a configured set of market slugs.
On disconnect (transport close, heartbeat silence, or transient error) the
worker reconnects with exponential backoff and automatically re-subscribes
every active market. Read-only: no orders are placed.

Auth reuses the SDK's Ed25519 signer (polymarket_us.auth.create_auth_headers)
so the WebSocket handshake signature is byte-identical to the REST client's:
signature = base64(Ed25519(timestamp + "GET" + /v1/ws/markets)).

Typical usage:

    worker = PolymarketWSWorker(
        market_slugs=["cpc-btc-updown-15m-abc"],
        on_quote=my_async_callback,
    )
    await worker.start()
    ...
    await worker.stop()
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import config

# Lazily imported so the module imports even without the optional dep.
websockets = None  # type: ignore[assignment]

from polymarket_us.auth import create_auth_headers  # noqa: E402  (proven Ed25519 signer)

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "wss://api.polymarket.us"
DEFAULT_PATH = "/v1/ws/markets"
DEFAULT_SUB_TYPE = "SUBSCRIPTION_TYPE_MARKET_DATA_LITE"
FULL_SUB_TYPE = "SUBSCRIPTION_TYPE_MARKET_DATA"
TRADE_SUB_TYPE = "SUBSCRIPTION_TYPE_TRADE"
MAX_MARKETS_PER_SUBSCRIPTION = 100

_DEFAULT_BW_DIR = Path.home() / ".secrets" / "bw"


class PolymarketWSError(Exception):
    """Fatal or transient worker error."""


@dataclass
class MarketQuote:
    """Top-of-book quote for one market. Prices are dollars (0-1 probability)."""

    market_slug: str
    best_bid: float | None
    best_ask: float | None
    last_trade_px: float | None
    current_px: float | None
    bid_depth: int | None
    ask_depth: int | None
    shares_traded: str | None
    open_interest: str | None
    state: str | None
    currency: str
    transact_time: str | None
    received_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_slug": self.market_slug,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "last_trade_px": self.last_trade_px,
            "current_px": self.current_px,
            "bid_depth": self.bid_depth,
            "ask_depth": self.ask_depth,
            "shares_traded": self.shares_traded,
            "open_interest": self.open_interest,
            "state": self.state,
            "currency": self.currency,
            "transact_time": self.transact_time,
            "received_at": self.received_at,
        }


@dataclass
class _Subscription:
    request_id: str
    sub_type: str
    slugs: list[str]


class PolymarketWSWorker:
    """Auto-reconnecting WebSocket worker for Polymarket US market data."""

    def __init__(
        self,
        *,
        market_slugs: list[str] | None = None,
        key_id: str | None = None,
        secret_key: str | None = None,
        on_quote: Callable[[MarketQuote], Any] | None = None,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
        base_url: str = DEFAULT_BASE_URL,
        path: str = DEFAULT_PATH,
        sub_type: str = DEFAULT_SUB_TYPE,
        heartbeat_timeout: float = 30.0,
        ping_interval: float = 20.0,
        ping_timeout: float = 30.0,
        open_timeout: float = 10.0,
        close_timeout: float = 10.0,
        reconnect_base: float = 1.0,
        reconnect_max: float = 60.0,
        reconnect_max_attempts: int | None = None,
        auth_max_failures: int = 3,
    ):
        self._key_id, self._secret_key = self._resolve_credentials(key_id, secret_key)
        self._base_url = base_url
        self._path = path
        self._sub_type = sub_type
        self._heartbeat_timeout = heartbeat_timeout
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._open_timeout = open_timeout
        self._close_timeout = close_timeout
        self._reconnect_base = reconnect_base
        self._reconnect_max = reconnect_max
        self._reconnect_max_attempts = reconnect_max_attempts
        self._auth_max_failures = auth_max_failures

        self._on_quote = on_quote
        self._on_event = on_event

        self._subscriptions: list[_Subscription] = _build_subscriptions(
            market_slugs or [], sub_type
        )
        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop = False

        self._stats: dict[str, Any] = {
            "connects": 0,
            "reconnects": 0,
            "messages_received": 0,
            "quotes_emitted": 0,
            "trades": 0,
            "heartbeats": 0,
            "errors": 0,
            "auth_failures": 0,
            "stopped": False,
            "stopped_auth": False,
            "stopped_max_attempts": False,
        }
        self._auth_failures = 0
        self._last_message_at: float | None = None
        self._last_heartbeat_at: float | None = None

    # ── credential resolution (same 3-source order as polymarket_us_client) ──
    @staticmethod
    def _resolve_credentials(key_id, secret_key):
        if key_id and secret_key:
            return key_id, secret_key
        env_key = os.environ.get("POLYMARKET_KEY_ID", "")
        env_secret = os.environ.get("POLYMARKET_SECRET_KEY", "")
        if env_key and env_secret:
            return env_key, env_secret
        bw_dir = Path(os.environ.get("BW_DIR", str(_DEFAULT_BW_DIR)))
        key_file = bw_dir / "polymarket_api_key.json"
        secret_file = bw_dir / "polymarket_secret_key.json"
        if key_file.exists() and secret_file.exists():
            try:
                kd = json.loads(key_file.read_text())
                sd = json.loads(secret_file.read_text())
                k, s = kd.get("value", ""), sd.get("value", "")
                if k and s:
                    return k, s
            except (json.JSONDecodeError, OSError):
                pass
        return config.POLYMARKET_KEY_ID or "", config.POLYMARKET_SECRET_KEY or ""

    # ── lifecycle ──
    async def start(self) -> None:
        if self._task is not None:
            return
        if not self._key_id or not self._secret_key:
            raise PolymarketWSError(
                "Polymarket US credentials not configured: set POLYMARKET_KEY_ID / "
                "POLYMARKET_SECRET_KEY (env or config) or place Bitwarden mirror "
                "files at ~/.secrets/bw/polymarket_*.json"
            )
        self._stop = False
        self._task = asyncio.create_task(self._run_loop(), name="polymarket-ws")
        log.info("Polymarket WS worker started for %d market(s)", len(self._subscriptions))

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._stats["stopped"] = True
        log.info("Polymarket WS worker stopped")

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and getattr(self._ws, "open", True)

    def stats(self) -> dict[str, Any]:
        return dict(self._stats)

    # ── subscription management ──
    async def subscribe(self, slugs: list[str], sub_type: str | None = None) -> None:
        """Add markets to stream (re-subscribes immediately if connected)."""
        sub_type = sub_type or self._sub_type
        new_subs = _build_subscriptions(slugs, sub_type)
        self._subscriptions.extend(new_subs)
        if self._ws is not None:
            for sub in new_subs:
                await self._send(sub)

    async def unsubscribe(self, request_id: str) -> None:
        """Remove a subscription by its requestId (unsubscribes if connected)."""
        self._subscriptions = [s for s in self._subscriptions if s.request_id != request_id]
        if self._ws is not None:
            await self._ws.send(json.dumps({"unsubscribe": {"requestId": request_id}}))

    async def _send(self, sub: _Subscription) -> None:
        await self._ws.send(
            json.dumps(
                {
                    "subscribe": {
                        "requestId": sub.request_id,
                        "subscriptionType": sub.sub_type,
                        "marketSlugs": sub.slugs,
                    }
                }
            )
        )

    # ── connection loop ──
    async def _run_loop(self) -> None:
        _import_websockets()
        ws_mod = websockets
        attempt = 0
        try:
            while not self._stop:
                try:
                    await self._connect_once()
                    attempt = 0
                    self._auth_failures = 0
                except asyncio.CancelledError:
                    raise
                except ws_mod.ConnectionClosed:
                    pass  # transport dropped; reconnect below
                except PolymarketWSError as exc:
                    log.warning("WS session ended: %s", exc)
                except ws_mod.InvalidStatus as exc:
                    sc = getattr(exc, "status_code", None)
                    if sc in (401, 403):
                        self._auth_failures += 1
                        self._stats["auth_failures"] = self._auth_failures
                        log.warning(
                            "WS auth rejected (HTTP %s): %d consecutive auth failures",
                            sc,
                            self._auth_failures,
                        )
                    else:
                        log.warning("WS handshake failed (HTTP %s): %s", sc, exc)
                except Exception as exc:  # noqa: BLE001 — any transient error reconnects
                    log.warning("WS session error: %r — reconnecting", exc)

                if self._stop:
                    break
                if self._auth_failures >= self._auth_max_failures:
                    self._stats["stopped_auth"] = True
                    log.error(
                        "stopping WS worker: %d consecutive auth failures",
                        self._auth_failures,
                    )
                    break
                if (
                    self._reconnect_max_attempts is not None
                    and attempt >= self._reconnect_max_attempts
                ):
                    self._stats["stopped_max_attempts"] = True
                    log.error("stopping WS worker: max reconnect attempts reached")
                    break
                delay = min(self._reconnect_base * (2.0**attempt), self._reconnect_max)
                delay *= 0.8 + 0.4 * (time.time() % 1.0)  # jitter
                attempt += 1
                self._stats["reconnects"] += 1
                log.info("reconnecting in %.1fs (attempt %d)", delay, attempt)
                await asyncio.sleep(delay)
        finally:
            self._stats["stopped"] = True
        log.info("Polymarket WS worker run loop exited")

    async def _connect_once(self) -> None:
        _import_websockets()
        ws_mod = websockets
        url = f"{self._base_url}{self._path}"
        headers = create_auth_headers(self._key_id, self._secret_key, "GET", self._path)
        log.debug("connecting %s", url)
        async with ws_mod.connect(
            url,
            additional_headers=headers,
            open_timeout=self._open_timeout,
            close_timeout=self._close_timeout,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
        ) as ws:
            self._ws = ws
            await self._subscribe_all()
            self._stats["connects"] += 1
            self._last_message_at = time.monotonic()
            self._last_heartbeat_at = self._last_message_at
            while True:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=self._heartbeat_timeout
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    raise PolymarketWSError(
                        "heartbeat timeout: no message within window"
                    )
                if raw is None:
                    return  # closed normally
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                await self._handle(raw)
        self._ws = None

    async def _subscribe_all(self) -> None:
        for sub in self._subscriptions:
            await self._send(sub)
            log.debug("subscribed %d market(s) to %s", len(sub.slugs), sub.sub_type)

    # ── message handling ──
    async def _handle(self, raw: str) -> None:
        self._stats["messages_received"] += 1
        self._last_message_at = time.monotonic()
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("unparseable WS message: %r", raw[:200])
            return
        if not isinstance(message, dict):
            return

        if "heartbeat" in message:
            self._stats["heartbeats"] += 1
            self._last_heartbeat_at = time.monotonic()
            return

        if "error" in message:
            self._stats["errors"] += 1
            err = str(message.get("error", ""))
            log.warning("WS error: %s (request %s)", err, message.get("requestId"))
            if self._looks_auth_error(err):
                self._auth_failures += 1
                self._stats["auth_failures"] = self._auth_failures
            return

        sub_type = message.get("subscriptionType")
        if sub_type in (DEFAULT_SUB_TYPE, FULL_SUB_TYPE) and (
            "marketDataLite" in message or "marketData" in message
        ):
            await self._emit_quote(message)
        elif sub_type == TRADE_SUB_TYPE or "trade" in message:
            self._stats["trades"] += 1
            await self._dispatch(self._on_event, message)

    async def _emit_quote(self, message: dict[str, Any]) -> None:
        quote = _extract_quote(message, self._sub_type)
        if quote is None:
            return
        self._stats["quotes_emitted"] += 1
        await self._dispatch(self._on_quote, quote)

    @staticmethod
    async def _dispatch(callback: Callable[[Any], Any] | None, payload: Any) -> None:
        if callback is None:
            return
        try:
            if inspect.iscoroutinefunction(callback):
                await callback(payload)
            else:
                callback(payload)
        except Exception:
            log.exception("WS callback raised")

    @staticmethod
    def _looks_auth_error(err: str) -> bool:
        haystack = err.lower()
        return any(
            token in haystack
            for token in ("auth", "signature", "401", "403", "permission", "key")
        )


def _import_websockets() -> None:
    global websockets
    if websockets is None:
        import websockets as _ws  # type: ignore

        websockets = _ws


def _build_subscriptions(slugs: list[str], sub_type: str) -> list[_Subscription]:
    subs: list[_Subscription] = []
    for i in range(0, len(slugs), MAX_MARKETS_PER_SUBSCRIPTION):
        chunk = slugs[i : i + MAX_MARKETS_PER_SUBSCRIPTION]
        subs.append(
            _Subscription(
                request_id=f"sub-{uuid.uuid4().hex[:12]}",
                sub_type=sub_type,
                slugs=list(chunk),
            )
        )
    return subs


def _px(amount: Any) -> float | None:
    if not isinstance(amount, dict):
        return None
    try:
        return float(amount.get("value"))
    except (TypeError, ValueError, AttributeError):
        return None


def _extract_quote(message: dict[str, Any], sub_type: str) -> MarketQuote | None:
    """Pull a top-of-book quote from a market-data message."""
    if sub_type == DEFAULT_SUB_TYPE and "marketDataLite" in message:
        payload = message["marketDataLite"]
        slug = payload.get("marketSlug")
        if not slug:
            return None
        return MarketQuote(
            market_slug=slug,
            best_bid=_px(payload.get("bestBid")),
            best_ask=_px(payload.get("bestAsk")),
            last_trade_px=_px(payload.get("lastTradePx")),
            current_px=_px(payload.get("currentPx")),
            bid_depth=payload.get("bidDepth"),
            ask_depth=payload.get("askDepth"),
            shares_traded=payload.get("sharesTraded"),
            open_interest=payload.get("openInterest"),
            state=None,
            currency="USD",
            transact_time=payload.get("transactTime"),
            received_at=time.monotonic(),
        )

    if "marketData" in message:
        payload = message["marketData"]
        slug = payload.get("marketSlug")
        if not slug:
            return None
        bids = payload.get("bids") or []
        offers = payload.get("offers") or []
        stats = payload.get("stats") or {}
        return MarketQuote(
            market_slug=slug,
            best_bid=_px(bids[0].get("px")) if bids else None,
            best_ask=_px(offers[0].get("px")) if offers else None,
            last_trade_px=_px(stats.get("lastTradePx")),
            current_px=None,
            bid_depth=None,
            ask_depth=None,
            shares_traded=stats.get("sharesTraded"),
            open_interest=stats.get("openInterest"),
            state=payload.get("state"),
            currency="USD",
            transact_time=payload.get("transactTime"),
            received_at=time.monotonic(),
        )

    return None


__all__ = [
    "PolymarketWSWorker",
    "MarketQuote",
    "PolymarketWSError",
    "DEFAULT_SUB_TYPE",
    "FULL_SUB_TYPE",
    "TRADE_SUB_TYPE",
]
