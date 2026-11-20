"""
coinbase_feed.py — Coinbase Advanced Trade WebSocket spot feed for BTC/ETH/SOL.

Provides sub-second spot prices via Coinbase WS ticker channel.
Read-only. No keys, no orders.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import List, Optional

import config

log = logging.getLogger("coinbase_feed")

# Coinbase WS endpoint
COINBASE_WS_URL = os.environ.get("COINBASE_WS_URL", "wss://advanced-trade-ws.coinbase.com")

# Known bases we care about
_KNOWN_BASES = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ZEC", "NEAR"}

# Max WS reconnection attempts before giving up
_COINBASE_WS_MAX_RETRIES = int(os.environ.get("COINBASE_WS_MAX_RETRIES", "100"))

# WS price cache (shared with binance_feed via module-level dict)
# {symbol: (price, ts)} — updated by background WS thread; read by spot_price()
_ws_prices: dict[str, tuple[float, float]] = {}
_ws_lock = threading.Lock()
_ws_started = False

# Staleness threshold (seconds)
_WS_STALE_SECONDS = float(os.environ.get("WS_SPOT_STALE_SECONDS", "1.0"))


def _ws_set(symbol: str, price: float) -> None:
    """Update the shared WS price cache."""
    now = time.monotonic()
    with _ws_lock:
        _ws_prices[symbol.upper()] = (price, now)
        base = _base_asset(symbol)
        if base and base != symbol.upper():
            _ws_prices[base] = (price, now)
        # Periodic cleanup: evict entries stale > 2x threshold
        stale_cutoff = now - _WS_STALE_SECONDS * 2
        fresh = {k: v for k, v in _ws_prices.items() if v[1] >= stale_cutoff}
        _ws_prices.clear()
        _ws_prices.update(fresh)


def spot_with_age(symbol: str) -> tuple[Optional[float], Optional[float]]:
    """Latest WS tick and its age in seconds, regardless of staleness.

    Returns (price, age) from the live stream, or (None, None) if the symbol
    has never ticked. Callers choose their own maximum age.
    """
    with _ws_lock:
        entry = _ws_prices.get(symbol.upper())
    if not entry:
        return None, None
    price, ts = entry
    return price, max(0.0, time.monotonic() - ts)


def _base_asset(symbol: str) -> Optional[str]:
    """Strip USD/USDT/-USD suffix and validate against known bases."""
    base = symbol.upper()
    for suffix in ("USDT", "USDC", "-USD", "USD"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    return base if base in _KNOWN_BASES else None


def _coinbase_ws_loop(symbols: list[str]) -> None:
    """Background thread: Coinbase Advanced Trade ticker channel."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    url = COINBASE_WS_URL
    products = [f"{s}-USD" for s in symbols]
    subscribe = {
        "type": "subscribe",
        "channel": "ticker",
        "product_ids": products,
    }
    backoff = 2.0
    retries = 0
    max_retries = _COINBASE_WS_MAX_RETRIES
    while True:
        try:
            loop.run_until_complete(_coinbase_ws_async(url, subscribe))
            backoff = 2.0
            retries = 0
        except Exception as e:
            retries += 1
            if max_retries is not None and retries >= max_retries:
                log.error("coinbase WS max retries (%d) exceeded — giving up", max_retries)
                return
            sleep_sec = min(backoff * (0.8 + 0.4 * (time.time() % 1.0)), 60.0)
            log.warning("coinbase WS disconnected: %s — reconnecting in %.1fs (retry %d/%s)",
                        e, sleep_sec, retries, max_retries if max_retries else "inf")
            time.sleep(sleep_sec)
            backoff = min(backoff * 2.0, 60.0)


async def _coinbase_ws_async(url: str, subscribe: dict) -> None:
    import websockets
    async with websockets.connect(url, open_timeout=10, close_timeout=10, ping_interval=20) as ws:
        await ws.send(json.dumps(subscribe))
        async for raw in ws:
            try:
                msg = json.loads(raw)
                if msg.get("channel") == "ticker":
                    for ev in msg.get("events", []):
                        for t in ev.get("tickers", []):
                            sym = t.get("product_id", "").replace("-USD", "")
                            px = t.get("price")
                            if sym and px is not None:
                                _ws_set(sym, float(px))
            except Exception:
                continue


def start_ws_streams(symbols: list[str] | None = None) -> None:
    """Start background WebSocket price stream for the given symbols.

    Safe to call multiple times — streams start only once. If `websockets` is
    not installed the stream is silently skipped.
    """
    global _ws_started
    if _ws_started:
        return
    symbols = symbols or list(_KNOWN_BASES)
    try:
        import websockets
        log.debug("websockets %s available", getattr(websockets, "__version__", "?"))
    except ImportError:
        log.warning("websockets not installed — WS price stream disabled")
        _ws_started = True
        return
    _ws_started = True
    t = threading.Thread(target=_coinbase_ws_loop, args=(symbols,),
                         daemon=True, name="ws-coinbase")
    t.start()
    log.info("WS price stream started: coinbase")


def spot_price(symbol: str = "BTC") -> Optional[float]:
    """Spot price for `symbol`: freshest WS price if available, else None.

    Coinbase feed only provides WS prices. Use composite_price() from
    binance_feed when you need REST fallback.
    """
    base = _base_asset(symbol)
    if not base:
        return None
    with _ws_lock:
        entry = _ws_prices.get(base)
    if not entry:
        return None
    price, ts = entry
    if time.monotonic() - ts > _WS_STALE_SECONDS:
        return None
    return price