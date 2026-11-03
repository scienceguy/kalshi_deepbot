"""
polymarket_client.py — Polymarket CLOB client for cross-market arbitrage.

Polymarket (https://polymarket.com) runs a Central Limit Order Book (CLOB)
at https://clob.polymarket.com. Each market has outcome tokens (e.g., "Yes",
"No") whose price in USDC represents the implied probability (0-1) of that
outcome occurring.

No authentication is required for public read endpoints (prices, order book).
Order placement requires a signed API key, which is optional — the bot can
read prices for arbitrage detection without placing Polymarket orders.

Usage:
    client = PolymarketClient()
    prices = client.get_prices()           # dict of token_id -> price
    book = client.get_order_book(token_id) # order book for a token
"""

import logging
import time
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter

import config

log = logging.getLogger("polymarket_client")

# Polymarket CLOB API base URL
BASE_URL = "https://clob.polymarket.com"

# Rate limit: Polymarket allows ~10 requests/second for public endpoints.
_MIN_REQUEST_GAP = 0.15  # seconds between requests


class PolymarketClient:
    """Minimal unsigned REST client for Polymarket CLOB market data.

    Fetches current prices and order books for Polymarket prediction markets.
    No API key required for public read endpoints.
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

    def get_prices(self) -> Optional[Dict[str, float]]:
        """Get current prices for all Polymarket outcome tokens.

        Returns dict mapping token_id -> price (0.0 to 1.0), or None on error.
        """
        try:
            self._rate_limit()
            resp = self.session.get(f"{BASE_URL}/prices", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            self._last_req = time.time()
            prices = {}
            for token_id, price_str in data.items():
                try:
                    prices[token_id] = float(price_str)
                except (ValueError, TypeError):
                    continue
            return prices
        except Exception as e:
            log.warning("Polymarket get_prices failed: %s", e)
            return None

    def get_order_book(self, token_id: str) -> Optional[Dict[str, Any]]:
        """Get the order book for a Polymarket outcome token.

        Returns dict with 'bids' and 'asks' lists, or None on error.
        Each bid/ask entry has 'price' (float) and 'size' (float).
        """
        try:
            self._rate_limit()
            resp = self.session.get(
                f"{BASE_URL}/book",
                params={"token_id": token_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self._last_req = time.time()
            return data
        except Exception as e:
            log.warning("Polymarket get_order_book(%s) failed: %s", token_id, e)
            return None

    def get_market(self, condition_id: str) -> Optional[Dict[str, Any]]:
        """Get market details for a Polymarket condition ID.

        Returns market dict or None on error.
        """
        try:
            self._rate_limit()
            resp = self.session.get(
                f"{BASE_URL}/market",
                params={"condition_id": condition_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self._last_req = time.time()
            return data
        except Exception as e:
            log.warning("Polymarket get_market(%s) failed: %s", condition_id, e)
            return None


# Global lazy instance
_client: Optional[PolymarketClient] = None


def get_client() -> PolymarketClient:
    """Get or create the global PolymarketClient instance."""
    global _client
    if _client is None:
        _client = PolymarketClient()
    return _client


def get_prices() -> Optional[Dict[str, float]]:
    """Convenience wrapper: get all Polymarket token prices."""
    return get_client().get_prices()


def get_order_book(token_id: str) -> Optional[Dict[str, Any]]:
    """Convenience wrapper: get order book for a token."""
    return get_client().get_order_book(token_id)


def get_market(condition_id: str) -> Optional[Dict[str, Any]]:
    """Convenience wrapper: get market details for a condition ID."""
    return get_client().get_market(condition_id)