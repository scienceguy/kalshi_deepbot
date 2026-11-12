"""kalshi_price_data_client.py — Optional wrapper for vcorp-dev/kalshi-price-data.

Provides programmatic access to historical Kalshi Level-2 order book depth
data via the community kalshi-price-data SDK.

Install:
    pip install kalshi-price-data

If not installed, the module degrades gracefully — all methods raise
ImportError with a helpful message.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("kalshi_price_data_client")


class KalshiPriceDataClient:
    """Wrapper around the kalshi-price-data Python SDK.

    This client fetches historical Level-2 order book snapshots for Kalshi
    15-minute markets, enabling accurate backtesting without lookahead bias.

    Requires: pip install kalshi-price-data
    Source: https://github.com/vcorp-dev/kalshi-price-data
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self._client = None
        self._available = self._try_import()

    def _try_import(self) -> bool:
        """Attempt to import the kalshi-price-data SDK."""
        try:
            import kalshi_price_data  # type: ignore
            self._client = kalshi_price_data.Client(api_key=self.api_key)
            log.info("kalshi-price-data SDK loaded successfully")
            return True
        except ImportError:
            log.debug(
                "kalshi-price-data not installed. "
                "Install with: pip install kalshi-price-data"
            )
            return False

    @property
    def available(self) -> bool:
        """Whether the SDK is installed and functional."""
        return self._available

    def get_historical_snapshots(self, series_ticker: str,
                                 start_time: str,
                                 end_time: str,
                                 market_ticker: Optional[str] = None) -> list[Dict[str, Any]]:
        """Fetch historical order book snapshots for a series or market.

        Args:
            series_ticker: Series ticker (e.g. "KXBTC15M").
            start_time: ISO 8601 start timestamp (UTC).
            end_time: ISO 8601 end timestamp (UTC).
            market_ticker: Optional specific market ticker to filter.

        Returns list of snapshot dicts with order book depth.
        """
        if not self._available:
            raise ImportError(
                "kalshi-price-data is not installed. "
                "Install with: pip install kalshi-price-data"
            )
        snapshots = self._client.get_historical_snapshots(
            series_ticker=series_ticker,
            start_time=start_time,
            end_time=end_time,
            market_ticker=market_ticker,
        )
        if not isinstance(snapshots, list):
            raise ValueError(
                f"kalshi-price-data returned unexpected type {type(snapshots).__name__}, "
                f"expected list of snapshots"
            )
        return snapshots

    def get_market_snapshots(self, market_ticker: str,
                             start_time: str,
                             end_time: str) -> list[Dict[str, Any]]:
        """Fetch historical order book snapshots for a single market.

        Args:
            market_ticker: Market ticker (e.g. "KXBTC15M-26NOV011145-45").
            start_time: ISO 8601 start timestamp (UTC).
            end_time: ISO 8601 end timestamp (UTC).

        Returns list of snapshot dicts with order book depth.
        """
        if not self._available:
            raise ImportError(
                "kalshi-price-data is not installed. "
                "Install with: pip install kalshi-price-data"
            )
        snapshots = self._client.get_market_snapshots(
            market_ticker=market_ticker,
            start_time=start_time,
            end_time=end_time,
        )
        if not isinstance(snapshots, list):
            raise ValueError(
                f"kalshi-price-data returned unexpected type {type(snapshots).__name__}, "
                f"expected list of snapshots"
            )
        return snapshots

    def save_to_parquet(self, snapshots: list[Dict[str, Any]],
                        output_path: str) -> Path:
        """Save snapshots to a Parquet file.

        Args:
            snapshots: List of snapshot dicts from get_historical_snapshots
                or get_market_snapshots.
            output_path: Path to write the Parquet file.

        Returns the Path object of the written file.
        """
        import pandas as pd
        df = pd.DataFrame(snapshots)
        out_path = Path(output_path)
        df.to_parquet(out_path, index=False)
        log.info("Saved %d snapshots to %s", len(snapshots), out_path)
        return out_path

    def health_check(self) -> bool:
        """Check if the SDK is installed and can reach the API."""
        if not self._available:
            return False
        try:
            if hasattr(self._client, "health_check"):
                self._client.health_check()
                return True
            log.warning(
                "kalshi-price-data SDK has no health_check method"
            )
            return False
        except Exception as e:
            log.warning("kalshi-price-data health check failed: %s", e)
            return False


# Global lazy instance
_client: Optional[KalshiPriceDataClient] = None


def get_client(api_key: Optional[str] = None) -> KalshiPriceDataClient:
    """Get or create the global KalshiPriceDataClient instance."""
    global _client
    if _client is None:
        _client = KalshiPriceDataClient(api_key=api_key)
    return _client


def is_available() -> bool:
    """Whether the kalshi-price-data SDK is installed."""
    try:
        import kalshi_price_data  # type: ignore
        return True
    except ImportError:
        return False
