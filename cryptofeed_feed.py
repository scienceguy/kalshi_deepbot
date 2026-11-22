"""
cryptofeed_feed.py - T15-11: Cryptofeed normalized spot feeds (Coinbase, Binance).

Upstream docs: https://github.com/bmoscon/cryptofeed and https://cryptofeed.readthedocs.io/ (2026-11-22)

LICENSE: cryptofeed is AGPL-3.0 - internal use only. This module wraps it
for internal market data ingestion and does not distribute the AGPL work.

Cross-loop note: cryptofeed runs its OWN asyncio event loop in its own
thread via FeedHandler.run(). tick_bus runs a SEPARATE uvloop in its own
background thread. Callbacks MUST NOT call tick_bus's asyncio loop directly.
Use tick_bus's thread-safe ingestion API: ingest_tick() and ingest_trade(),
callable cross-thread with no event loop running.

Cryptofeed default logging is noisy; configure its loggers to WARNING.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

log = logging.getLogger("cryptofeed_feed")

# -- Configuration ----------------------------------------------------------
# Enable cryptofeed feeds (default off to avoid mandatory dependency at import time)
USE_CRYPTOFEED = os.environ.get("USE_CRYPTOFEED", "").lower() in ("1", "true", "yes", "on")
# Comma-separated venues to subscribe to
CRYPTOFEED_VENUES = [v.strip().upper() for v in os.environ.get("CRYPTOFEED_VENUES", "COINBASE,BINANCE").split(",") if v.strip()]
# Comma-separated symbols in cryptofeed format (e.g., "BTC-USD,ETH-USD")
CRYPTOFEED_SYMBOLS = [s.strip() for s in os.environ.get("CRYPTOFEED_SYMBOLS", "BTC-USD,ETH-USD").split(",") if s.strip()]

# Module state
_cryptofeed_available: bool = False
_feed_handler: Any = None
_feed_thread: threading.Thread | None = None
_running: bool = False
_ticks_ingested: int = 0
_trades_ingested: int = 0
# Test mode: skips real exchange symbol-mapping REST calls so no sockets/HTTP open
_test_mode: bool = False


def set_test_mode(enabled: bool) -> None:
    """Enable/disable test mode (skips exchange construction, no HTTP/socket)."""
    global _test_mode
    _test_mode = enabled


def is_test_mode() -> bool:
    """Check if test mode is enabled."""
    return _test_mode

# -- Import guard -----------------------------------------------------------
try:
    from cryptofeed import FeedHandler
    from cryptofeed.exchanges import Coinbase, Binance
    from cryptofeed.types import Ticker, Trade
    _cryptofeed_available = True
except Exception as e:  # pragma: no cover - import failure path
    log.warning("cryptofeed not available: %s - cryptofeed_feed will no-op", e)
    _cryptofeed_available = False

    # Stubs so the module imports cleanly and public API exists
    class _NoOpFeedHandler:
        def add_feed(self, *args, **kwargs):
            pass
        def run(self):
            pass

    FeedHandler = _NoOpFeedHandler  # type: ignore[assignment,misc]
    Coinbase = object  # type: ignore[assignment,misc]
    Binance = object  # type: ignore[assignment,misc]
    Ticker = object  # type: ignore[assignment,misc]
    Trade = object  # type: ignore[assignment,misc]

# -- Helpers ----------------------------------------------------------------

def _symbol_to_base(symbol: str) -> str:
    """Map cryptofeed normalized symbol (e.g., 'BTC-USD') to base asset ('BTC')."""
    return symbol.split("-")[0].upper()


# -- Callback implementations -----------------------------------------------

async def _on_ticker(t: Ticker, receipt_timestamp: float) -> None:
    """Normalized ticker callback -> tick_bus.ingest_tick.

    cryptofeed Ticker carries bid/ask/symbol/exchange/timestamp (no .price or
    .sequence fields). Price is derived as the bid/ask mid.
    """
    global _ticks_ingested
    if not _cryptofeed_available:
        return
    try:
        from tick_bus import get_tick_bus
    except Exception as e:
        log.debug("tick_bus not available in _on_ticker: %s", e)
        return
    symbol = _symbol_to_base(getattr(t, "symbol", ""))
    venue = getattr(t, "exchange", "").lower()
    bid = float(getattr(t, "bid", 0)) if getattr(t, "bid", None) else None
    ask = float(getattr(t, "ask", 0)) if getattr(t, "ask", None) else None
    price = (bid + ask) / 2.0 if (bid is not None and ask is not None) else None
    venue_timestamp = float(receipt_timestamp) if receipt_timestamp else None
    if symbol and venue and price is not None:
        get_tick_bus().ingest_tick(
            symbol=symbol,
            venue=venue,
            price=price,
            bid=bid,
            ask=ask,
            venue_timestamp=venue_timestamp,
        )
        _ticks_ingested += 1


async def _on_trade(t: Trade, receipt_timestamp: float) -> None:
    """Normalized trade callback -> tick_bus.ingest_trade."""
    global _trades_ingested
    if not _cryptofeed_available:
        return
    try:
        from tick_bus import get_tick_bus
    except Exception as e:
        log.debug("tick_bus not available in _on_trade: %s", e)
        return
    symbol = _symbol_to_base(getattr(t, "symbol", ""))
    venue = getattr(t, "exchange", "").lower()
    price = float(getattr(t, "price", 0) or 0)
    size = float(getattr(t, "amount", 0) or 0)
    side = getattr(t, "side", "").lower()
    if symbol and venue and price > 0:
        get_tick_bus().ingest_trade(
            symbol=symbol,
            venue=venue,
            price=price,
            size=size,
            side=side,
        )
        _trades_ingested += 1


# -- Public API -------------------------------------------------------------

def start_cryptofeed() -> None:
    """
    Build FeedHandler with selected exchanges/symbols and start in daemon thread.
    Idempotent: second call while running is a no-op.
    """
    global _feed_handler, _feed_thread, _running, _ticks_ingested, _trades_ingested

    if _running:
        return

    if not _cryptofeed_available:
        log.warning("cryptofeed not available - start_cryptofeed() no-op")
        return

    # Quiet cryptofeed's default loggers
    for name in ("cryptofeed", "cryptofeed.exchanges", "cryptofeed.callbacks"):
        logging.getLogger(name).setLevel(logging.WARNING)

    _feed_handler = FeedHandler(config=None)

    if _test_mode:
        # In test mode skip exchange construction and the real FeedHandler run
        # loop (no HTTP/symbol-mapping calls, no sockets). Start a daemon stub
        # thread so the start/stop state machine is fully exercised and
        # is_running() reports True while the stub is alive. No socket is ever
        # opened and cryptofeed's real FeedHandler.run() is not invoked.
        log.info("cryptofeed_feed test mode: running feed stub")
        _running = True
        _ticks_ingested = 0
        _trades_ingested = 0
        _feed_thread = threading.Thread(
            target=_run_test_stub,
            daemon=True,
            name="cryptofeed-feed-test",
        )
        _feed_thread.start()
        log.info("cryptofeed_feed started (venues=%s, symbols=%s)", CRYPTOFEED_VENUES, CRYPTOFEED_SYMBOLS)
        return

    # Map venue name to exchange class and default channel list
    venue_map = {
        "COINBASE": (Coinbase, ["ticker", "trade"]),
        "BINANCE": (Binance, ["ticker", "trade"]),
    }

    feeds_added = 0
    for venue in CRYPTOFEED_VENUES:
        entry = venue_map.get(venue)
        if not entry:
            log.warning("Unknown cryptofeed venue: %s - skipping", venue)
            continue
        exchange_cls, channels = entry
        try:
            feed = exchange_cls(symbols=CRYPTOFEED_SYMBOLS, channels=channels, callbacks={
                "ticker": _on_ticker,
                "trade": _on_trade,
            })
            _feed_handler.add_feed(feed)
            log.info("Added cryptofeed feed: %s symbols=%s channels=%s", venue, CRYPTOFEED_SYMBOLS, channels)
            feeds_added += 1
        except Exception as e:
            log.warning("Failed to add cryptofeed feed %s: %s - skipping", venue, e)
            continue

    if feeds_added == 0:
        log.warning("No cryptofeed feeds added successfully - not starting feed thread")
        _feed_handler = None
        return

    _feed_thread = threading.Thread(
        target=_run_feed_handler,
        daemon=True,
        name="cryptofeed-feed",
    )
    _running = True
    _ticks_ingested = 0
    _trades_ingested = 0
    _feed_thread.start()
    log.info("cryptofeed_feed started (venues=%s, symbols=%s)", CRYPTOFEED_VENUES, CRYPTOFEED_SYMBOLS)


def _run_feed_handler() -> None:
    """Thread target: runs FeedHandler.run() (blocking)."""
    global _running
    try:
        if _feed_handler is not None:
            _feed_handler.run()
    except Exception as e:
        log.error("cryptofeed FeedHandler.run() raised: %s", e)
    finally:
        _running = False


def _run_test_stub() -> None:
    """Test-mode stub run loop.

    Blocks until stop_cryptofeed() clears _running, then exits. Never opens a
    socket or invokes cryptofeed's real FeedHandler.run(). Daemon thread, so it
    cannot keep the process alive.
    """
    while _running:
        threading.Event().wait(0.05)


def stop_cryptofeed() -> None:
    """Stop the feed handler and thread."""
    global _feed_handler, _feed_thread, _running
    if not _running:
        return
    _running = False
    # FeedHandler has no clean shutdown; thread is daemon so it dies with process
    _feed_handler = None
    _feed_thread = None
    log.info("cryptofeed_feed stopped")


def is_running() -> bool:
    """Return True if the feed thread is alive."""
    global _running, _feed_thread
    if not _running:
        return False
    if _feed_thread is None:
        _running = False
        return False
    return _feed_thread.is_alive()


def get_status() -> dict:
    """Return status dict: running, venues, symbols, ticks_ingested, trades_ingested."""
    return {
        "running": is_running(),
        "venues": CRYPTOFEED_VENUES,
        "symbols": CRYPTOFEED_SYMBOLS,
        "ticks_ingested": _ticks_ingested,
        "trades_ingested": _trades_ingested,
    }


# Import-time log
if not _cryptofeed_available:
    log.info("cryptofeed_feed imported (cryptofeed unavailable - stubs active)")
else:
    log.info("cryptofeed_feed imported (cryptofeed available, USE_CRYPTOFEED=%s)", USE_CRYPTOFEED)