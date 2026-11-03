"""
binance_client.py — Binance REST API client for crypto spot price data.

Provides real-time price feeds from Binance to cross-reference Kalshi
prediction market prices against underlying crypto spot markets.

No authentication required for public market data endpoints.
All methods degrade gracefully — network errors are caught and logged.

Usage:
    client = BinanceClient()
    price = client.get_price("BTCUSDT")  # returns float in USD
    ticker = client.get_ticker("BTCUSDT")  # returns full ticker dict
"""

import logging
import time
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter

import config

log = logging.getLogger("binance_client")

# Binance public API base URL
BASE_URL = "https://api.binance.com/api/v3"

# Rate limit: Binance allows ~10 requests/second for public endpoints.
_MIN_REQUEST_GAP = 0.12  # seconds between requests


class BinanceClient:
    """
    Minimal unsigned REST client for Binance spot market data.

    Fetches latest prices and 24h ticker statistics for crypto pairs.
    No API key required for public endpoints.
    """

    def __init__(self):
        self.base_url = BASE_URL
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "kalshi-superbot/1.0",
        })
        self.session.mount("https://", HTTPAdapter(pool_connections=10, pool_maxsize=10))
        self._last_req = 0.0

    def _rate_limit(self) -> None:
        """Enforce minimum gap between requests to avoid rate limits."""
        elapsed = time.time() - self._last_req
        if elapsed < _MIN_REQUEST_GAP:
            time.sleep(_MIN_REQUEST_GAP - elapsed)

    def get_price(self, symbol: str) -> Optional[float]:
        """
        Get the latest price for a trading symbol.

        Args:
            symbol: Trading pair symbol (e.g. "BTCUSDT", "ETHUSDT").

        Returns:
            Price in USD as a float, or None if the request fails.
        """
        try:
            self._rate_limit()
            resp = self.session.get(
                f"{self.base_url}/ticker/price",
                params={"symbol": symbol.upper()},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self._last_req = time.time()
            return float(data["price"])
        except Exception as e:
            log.warning("Binance get_price(%s) failed: %s", symbol, e)
            return None

    def get_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get 24h ticker statistics for a trading symbol.

        Returns dict with price, bid, ask, volume, etc., or None on failure.
        """
        try:
            self._rate_limit()
            resp = self.session.get(
                f"{self.base_url}/ticker/24hr",
                params={"symbol": symbol.upper()},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self._last_req = time.time()
            return data
        except Exception as e:
            log.warning("Binance get_ticker(%s) failed: %s", symbol, e)
            return None

    def get_orderbook(self, symbol: str, limit: int = 100) -> Optional[Dict[str, Any]]:
        """
        Get the order book for a trading symbol.

        Returns dict with bids and asks arrays, or None on failure.
        """
        try:
            self._rate_limit()
            resp = self.session.get(
                f"{self.base_url}/depth",
                params={"symbol": symbol.upper(), "limit": limit},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self._last_req = time.time()
            return data
        except Exception as e:
            log.warning("Binance get_orderbook(%s) failed: %s", symbol, e)
            return None


# Global lazy instance
_client: Optional[BinanceClient] = None


def get_client() -> BinanceClient:
    """Get or create the global BinanceClient instance."""
    global _client
    if _client is None:
        _client = BinanceClient()
    return _client


def get_price(symbol: str) -> Optional[float]:
    """Convenience wrapper: get latest price for a symbol."""
    return get_client().get_price(symbol)


def get_ticker(symbol: str) -> Optional[Dict[str, Any]]:
    """Convenience wrapper: get 24h ticker for a symbol."""
    return get_client().get_ticker(symbol)


def get_orderbook(symbol: str, limit: int = 100) -> Optional[Dict[str, Any]]:
    """Convenience wrapper: get order book for a symbol."""
    return get_client().get_orderbook(symbol, limit)
