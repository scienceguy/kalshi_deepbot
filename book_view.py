"""book_view.py — D5: Unified order-book view across venues.

Provides a consolidated view of order books from multiple venues (Kalshi, Polymarket US,
Binance, Coinbase) for cross-venue arbitrage and edge detection.

Uses the existing WebSocket streams (kalshi_ws, poly_us_stream) and REST fallbacks.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import config

log = logging.getLogger(__name__)


@dataclass
class Level:
    """Single price level in the order book."""
    price: float
    size: float


@dataclass
class VenueBook:
    """Order book from a single venue."""
    venue: str
    market_id: str
    yes_bids: List[Level] = field(default_factory=list)
    yes_asks: List[Level] = field(default_factory=list)
    no_bids: List[Level] = field(default_factory=list)
    no_asks: List[Level] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    sequence: int = 0


@dataclass
class ConsolidatedBook:
    """Consolidated order book across venues for a given market/strike."""
    market_id: str
    strike: float
    venue_books: Dict[str, VenueBook] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def best_yes_bid(self) -> Optional[Level]:
        """Best YES bid across all venues."""
        best = None
        for vb in self.venue_books.values():
            if vb.yes_bids:
                top = vb.yes_bids[0]
                if best is None or top.price > best.price:
                    best = Level(top.price, top.size)
        return best

    def best_yes_ask(self) -> Optional[Level]:
        """Best YES ask across all venues."""
        best = None
        for vb in self.venue_books.values():
            if vb.yes_asks:
                top = vb.yes_asks[0]
                if best is None or top.price < best.price:
                    best = Level(top.price, top.size)
        return best

    def best_no_bid(self) -> Optional[Level]:
        """Best NO bid across all venues."""
        best = None
        for vb in self.venue_books.values():
            if vb.no_bids:
                top = vb.no_bids[0]
                if best is None or top.price > best.price:
                    best = Level(top.price, top.size)
        return best

    def best_no_ask(self) -> Optional[Level]:
        """Best NO ask across all venues."""
        best = None
        for vb in self.venue_books.values():
            if vb.no_asks:
                top = vb.no_asks[0]
                if best is None or top.price < best.price:
                    best = Level(top.price, top.size)
        return best


class BookView:
    """
    Unified order-book view across venues.

    Maintains per-market consolidated books by pulling from:
    - Kalshi WebSocket (kalshi_ws.KalshiWebSocket)
    - Polymarket US WebSocket (poly_us_stream.PolyStream)
    - REST fallbacks for initial snapshot

    Thread-safe: background threads update internal caches; callers read
    snapshots without blocking.
    """

    def __init__(
        self,
        kalshi_ws=None,
        poly_stream=None,
        binance_feed=None,
        coinbase_feed=None,
    ):
        """
        Initialize BookView with venue data sources.

        Args:
            kalshi_ws: KalshiWebSocket instance (optional)
            poly_stream: PolyStream instance (optional)
            binance_feed: binance_feed module (optional, for spot reference)
            coinbase_feed: coinbase_feed module (optional, for spot reference)
        """
        self._kalshi_ws = kalshi_ws
        self._poly_stream = poly_stream
        self._binance_feed = binance_feed
        self._coinbase_feed = coinbase_feed

        # Consolidated books by market_id
        self._books: Dict[str, ConsolidatedBook] = {}
        self._lock = threading.RLock()

        # Kalshi: map ticker -> (strike, side) for orderbook
        self._kalshi_orderbooks: Dict[str, Dict] = {}
        self._kalshi_lock = threading.RLock()

        # Polymarket: map slug -> OrderBook
        self._poly_orderbooks: Dict[str, Dict] = {}
        self._poly_lock = threading.RLock()

    # ── Kalshi integration ────────────────────────────────────────────

    def update_kalshi_orderbook(self, ticker: str, snapshot: Dict) -> None:
        """Update Kalshi orderbook from WebSocket snapshot message."""
        with self._kalshi_lock:
            self._kalshi_orderbooks[ticker] = snapshot

    def update_kalshi_delta(self, ticker: str, delta: Dict) -> None:
        """Update Kalshi orderbook from WebSocket delta message."""
        with self._kalshi_lock:
            if ticker in self._kalshi_orderbooks:
                # Apply delta to existing snapshot
                self._apply_kalshi_delta(self._kalshi_orderbooks[ticker], delta)
            else:
                log.debug("Delta received before snapshot for %s", ticker)

    def _apply_kalshi_delta(self, snapshot: Dict, delta: Dict) -> None:
        """Apply incremental delta to Kalshi orderbook snapshot."""
        # Kalshi delta format: {"yes_changes": [...], "no_changes": [...]}
        for change in delta.get("yes_changes", []):
            price = change.get("price")
            size = change.get("size")
            side = change.get("side")  # "bid" or "ask"
            if price is None or size is None:
                continue
            if size == 0:
                # Remove level
                self._remove_level(snapshot, "yes", side, price)
            else:
                # Update/insert level
                self._upsert_level(snapshot, "yes", side, price, size)

        for change in delta.get("no_changes", []):
            price = change.get("price")
            size = change.get("size")
            side = change.get("side")
            if price is None or size is None:
                continue
            if size == 0:
                self._remove_level(snapshot, "no", side, price)
            else:
                self._upsert_level(snapshot, "no", side, price, size)

    def _remove_level(self, book: Dict, contract: str, side: str, price: float) -> None:
        """Remove a price level from book."""
        levels = book.get(f"{contract}_{side}s", [])
        book[f"{contract}_{side}s"] = [l for l in levels if l.get("price") != price]

    def _upsert_level(self, book: Dict, contract: str, side: str, price: float, size: float) -> None:
        """Insert or update a price level in book."""
        key = f"{contract}_{side}s"
        levels = book.get(key, [])
        found = False
        for l in levels:
            if l.get("price") == price:
                l["size"] = size
                found = True
                break
        if not found:
            levels.append({"price": price, "size": size})
        # Sort: bids descending, asks ascending
        levels.sort(key=lambda x: x["price"], reverse=(side == "bid"))
        book[key] = levels

    # ── Polymarket US integration ────────────────────────────────────

    def update_poly_orderbook(self, slug: str, orderbook: Dict) -> None:
        """Update Polymarket orderbook from WebSocket (market_data or market_data_lite)."""
        with self._poly_lock:
            self._poly_orderbooks[slug] = orderbook

    # ── Consolidation ────────────────────────────────────────────────

    def consolidate(self, market_id: str, strike: float) -> Optional[ConsolidatedBook]:
        """Build a consolidated book for a market/strike from all available sources."""
        with self._lock:
            venue_books: Dict[str, VenueBook] = {}

            # Kalshi: find matching ticker for this strike
            with self._kalshi_lock:
                for ticker, ob in self._kalshi_orderbooks.items():
                    if strike in str(ticker) or ticker.endswith(str(int(strike))):
                        vb = self._convert_kalshi_book(ticker, ob)
                        if vb:
                            venue_books["kalshi"] = vb
                            break

            # Polymarket US
            with self._poly_lock:
                for slug, ob in self._poly_orderbooks.items():
                    if strike in slug or slug.endswith(str(int(strike))):
                        vb = self._convert_poly_book(slug, ob)
                        if vb:
                            venue_books["polymarket_us"] = vb
                            break

            if not venue_books:
                return None

            book = ConsolidatedBook(
                market_id=market_id,
                strike=strike,
                venue_books=venue_books,
                timestamp=time.time(),
            )
            self._books[market_id] = book
            return book

    def _convert_kalshi_book(self, ticker: str, ob: Dict) -> Optional[VenueBook]:
        """Convert Kalshi orderbook format to VenueBook."""
        try:
            yes_bids = [Level(l["price"], l["size"]) for l in ob.get("yes_bids", [])]
            yes_asks = [Level(l["price"], l["size"]) for l in ob.get("yes_asks", [])]
            no_bids = [Level(l["price"], l["size"]) for l in ob.get("no_bids", [])]
            no_asks = [Level(l["price"], l["size"]) for l in ob.get("no_asks", [])]

            # Sort properly
            yes_bids.sort(key=lambda x: x.price, reverse=True)
            yes_asks.sort(key=lambda x: x.price)
            no_bids.sort(key=lambda x: x.price, reverse=True)
            no_asks.sort(key=lambda x: x.price)

            return VenueBook(
                venue="kalshi",
                market_id=ticker,
                yes_bids=yes_bids,
                yes_asks=yes_asks,
                no_bids=no_bids,
                no_asks=no_asks,
                timestamp=time.time(),
            )
        except Exception as e:
            log.warning("Failed to convert Kalshi book %s: %s", ticker, e)
            return None

    def _convert_poly_book(self, slug: str, ob: Dict) -> Optional[VenueBook]:
        """Convert Polymarket orderbook format to VenueBook."""
        try:
            # Polymarket provides YES side bids/asks directly
            yes_bids = [Level(l["price"], l["size"]) for l in ob.get("yes_bids", [])]
            yes_asks = [Level(l["price"], l["size"]) for l in ob.get("yes_asks", [])]

            # NO side is complementary (1 - price)
            no_bids = [Level(1.0 - l.price, l.size) for l in yes_asks]  # NO bid = YES ask complement
            no_asks = [Level(1.0 - l.price, l.size) for l in yes_bids]  # NO ask = YES bid complement

            return VenueBook(
                venue="polymarket_us",
                market_id=slug,
                yes_bids=yes_bids,
                yes_asks=yes_asks,
                no_bids=no_bids,
                no_asks=no_asks,
                timestamp=time.time(),
            )
        except Exception as e:
            log.warning("Failed to convert Polymarket book %s: %s", slug, e)
            return None

    # ── Query methods ────────────────────────────────────────────────

    def get_book(self, market_id: str) -> Optional[ConsolidatedBook]:
        """Get consolidated book for a market."""
        with self._lock:
            return self._books.get(market_id)

    def best_yes_bid(self, market_id: str) -> Optional[Level]:
        """Best YES bid for a market across all venues."""
        book = self.get_book(market_id)
        return book.best_yes_bid() if book else None

    def best_yes_ask(self, market_id: str) -> Optional[Level]:
        """Best YES ask for a market across all venues."""
        book = self.get_book(market_id)
        return book.best_yes_ask() if book else None

    def best_no_bid(self, market_id: str) -> Optional[Level]:
        """Best NO bid for a market across all venues."""
        book = self.get_book(market_id)
        return book.best_no_bid() if book else None

    def best_no_ask(self, market_id: str) -> Optional[Level]:
        """Best NO ask for a market across all venues."""
        book = self.get_book(market_id)
        return book.best_no_ask() if book else None

    def cross_venue_spread(self, market_id: str) -> Optional[Dict[str, float]]:
        """Calculate cross-venue spreads for a market."""
        book = self.get_book(market_id)
        if not book:
            return None

        result = {}
        bid = book.best_yes_bid()
        ask = book.best_yes_ask()
        if bid and ask:
            result["yes_spread"] = ask.price - bid.price

        bid = book.best_no_bid()
        ask = book.best_no_ask()
        if bid and ask:
            result["no_spread"] = ask.price - bid.price

        return result if result else None

    def venue_prices(self, market_id: str) -> Dict[str, Dict[str, Optional[float]]]:
        """Get best bid/ask per venue for a market."""
        book = self.get_book(market_id)
        if not book:
            return {}

        result = {}
        for venue, vb in book.venue_books.items():
            result[venue] = {
                "yes_bid": vb.yes_bids[0].price if vb.yes_bids else None,
                "yes_ask": vb.yes_asks[0].price if vb.yes_asks else None,
                "no_bid": vb.no_bids[0].price if vb.no_bids else None,
                "no_ask": vb.no_asks[0].price if vb.no_asks else None,
            }
        return result


# Module-level default instance
_default_book_view: Optional[BookView] = None


def get_book_view() -> BookView:
    """Get or create the default BookView."""
    global _default_book_view
    if _default_book_view is None:
        _default_book_view = BookView()
    return _default_book_view