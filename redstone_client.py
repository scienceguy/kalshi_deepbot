"""
redstone_client.py — Redstone oracle REST API client for crypto price data.

Provides real-time price oracle data from Redstone to cross-reference
Kalshi prediction market prices against decentralized oracle feeds.

Redstone aggregates prices from multiple sources and provides
cryptographically signed price feeds for crypto assets.

No authentication required for public price feed endpoints.
All methods degrade gracefully — network errors are caught and logged.

Usage:
    client = RedstoneClient()
    price = client.get_price("BTC")  # returns float in USD
    feed = client.get_feed("BTC")  # returns full feed dict with metadata
"""

import logging
import time
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter

import config

log = logging.getLogger("redstone_client")

# Redstone public API base URL
BASE_URL = "https://api.redstone.finance"

# Rate limit: Redstone allows reasonable request rates for public endpoints.
_MIN_REQUEST_GAP = 0.12  # seconds between requests


class RedstoneClient:
    """
    Minimal unsigned REST client for Redstone oracle price feeds.

    Fetches cryptographically signed price feeds for crypto assets.
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
        Get the latest price for a crypto asset from Redstone oracle.

        Redstone uses base symbols (e.g. "BTC", "ETH", "SOL").

        Args:
            symbol: Asset symbol (e.g. "BTC", "ETH", "SOL").

        Returns:
            Price in USD as a float, or None if the request fails.
        """
        try:
            self._rate_limit()
            resp = self.session.get(
                f"{self.base_url}/prices/{symbol.upper()}",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self._last_req = time.time()
            # Redstone returns price as "value" field
            return float(data.get("value", data.get("price", 0)))
        except Exception as e:
            log.warning("Redstone get_price(%s) failed: %s", symbol, e)
            return None

    def get_feed(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get the full oracle feed for a crypto asset.

        Returns dict with price, timestamp, providers, signatures, etc.,
        or None on failure.
        """
        try:
            self._rate_limit()
            resp = self.session.get(
                f"{self.base_url}/feeds/{symbol.upper()}",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self._last_req = time.time()
            return data
        except Exception as e:
            log.warning("Redstone get_feed(%s) failed: %s", symbol, e)
            return None

    def get_multiple_prices(self, symbols: list[str]) -> Dict[str, Optional[float]]:
        """
        Get prices for multiple assets in one batch.

        Args:
            symbols: List of asset symbols (e.g. ["BTC", "ETH", "SOL"]).

        Returns dict mapping symbol -> price (or None if unavailable).
        """
        results: Dict[str, Optional[float]] = {}
        for symbol in symbols:
            results[symbol] = self.get_price(symbol)
        return results


# Global lazy instance
_client: Optional[RedstoneClient] = None


def get_client() -> RedstoneClient:
    """Get or create the global RedstoneClient instance."""
    global _client
    if _client is None:
        _client = RedstoneClient()
    return _client


def get_price(symbol: str) -> Optional[float]:
    """Convenience wrapper: get latest price for an asset."""
    return get_client().get_price(symbol)


def get_feed(symbol: str) -> Optional[Dict[str, Any]]:
    """Convenience wrapper: get full oracle feed for an asset."""
    return get_client().get_feed(symbol)


def get_multiple_prices(symbols: list[str]) -> Dict[str, Optional[float]]:
    """Convenience wrapper: get prices for multiple assets."""
    return get_client().get_multiple_prices(symbols)
