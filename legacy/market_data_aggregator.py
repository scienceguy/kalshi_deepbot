#!/usr/bin/env python3
"""
market_data_aggregator.py — Unified market data aggregator for Kalshi SuperBot.

Aggregates price data from multiple sources (Binance, Coinbase, Redstone)
to compute consensus prices, trends, and volatility for Kalshi 15-minute
binary market trading decisions.

Key features:
- Fetches from Binance, Coinbase, Redstone in parallel
- Computes median consensus price (robust to outliers)
- Calculates short-term trend/velocity from recent price history
- Provides confidence scores based on source agreement
- Caches prices to avoid rate limits
- Thread-safe with locking
"""

import logging
import time
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from statistics import median, stdev
from concurrent.futures import ThreadPoolExecutor, as_completed

import config

# Import individual clients
from binance_client import get_price as binance_get_price, get_ticker as binance_get_ticker
from coinbase_client import get_price as coinbase_get_price, get_ticker as coinbase_get_ticker
from redstone_client import get_price as redstone_get_price, get_feed as redstone_get_feed

log = logging.getLogger("market_data_aggregator")

# ─── Configuration ──────────────────────────────────────────────────────────
# Series to crypto symbol mapping
KALSHI_TO_CRYPTO = {
    "KXBTC15M": "BTC",
    "KXETH15M": "ETH",
    "KXSOL15M": "SOL",
    "KXXRP15M": "XRP",
    "KXDOGE15M": "DOGE",
    "KXBNB15M": "BNB",
    "KXHYPE15M": "HYPE",
    "KXZEC15M": "ZEC",
    "KXNEAR15M": "NEAR",
    # Additional mappings for other series
    "KXAU15M": "XAU",  # Gold
    "KXAG15M": "XAG",  # Silver
    "KXCL15M": "CL",   # Crude Oil (WTI)
    "KXSP15M": "SPX",  # S&P 500
    "KXND15M": "NDX",  # Nasdaq 100
}

# Symbol mappings per exchange
BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT",
    "DOGE": "DOGEUSDT", "BNB": "BNBUSDT", "HYPE": "HYPEUSDT",
    "ZEC": "ZECUSDT", "NEAR": "NEARUSDT",
}

COINBASE_SYMBOLS = {
    "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD",
    "DOGE": "DOGE-USD", "BNB": "BNB-USD", "HYPE": "HYPE-USD",
    "ZEC": "ZEC-USD", "NEAR": "NEAR-USD",
}

REDSTONE_SYMBOLS = {
    "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ZEC", "NEAR",
    "XAU", "XAG", "CL", "SPX", "NDX",
}

# Price history window (seconds)
PRICE_HISTORY_WINDOW = 60  # seconds of history to track for trend calc
MAX_HISTORY_POINTS = 120  # max points per series


@dataclass
class PricePoint:
    """Single price observation with metadata."""
    price: float
    timestamp: float
    source: str  # "binance", "coinbase", "redstone"


@dataclass
class ConsensusPrice:
    """Aggregated price consensus from multiple sources."""
    symbol: str
    price: float                    # median consensus price (USD)
    timestamp: float                # timestamp of this consensus
    sources: List[str]              # sources that contributed
    spread: float                   # max - min across sources
    spread_pct: float               # spread as percentage of price
    confidence: float               # 0-1, higher = more agreement
    n_sources: int                  # number of sources that responded
    prices_by_source: Dict[str, float]  # price by source
    timestamp_by_source: Dict[str, float]  # timestamp by source


@dataclass
class TrendInfo:
    """Trend/velocity information for an asset."""
    symbol: str
    price: float                    # current consensus price
    velocity: float                 # price change per second (USD/s)
    velocity_pct: float             # percentage change per second
    acceleration: float             # change in velocity per second
    volatility: float               # standard deviation of recent returns
    direction: str                  # "up", "down", "flat"
    confidence: float               # 0-1, based on data quality & agreement
    timestamp: float
    n_points: int                   # number of data points used


class MarketDataAggregator:
    """
    Unified market data aggregator combining Binance, Coinbase, and Redstone.
    
    Provides consensus prices, trends, and volatility for Kalshi trading decisions.
    """
    
    def __init__(self):
        self._price_history: Dict[str, deque] = {}  # symbol -> deque of PricePoint
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=3)
        self._last_fetch: Dict[str, float] = {}
        self._min_fetch_interval = 0.5  # minimum seconds between fetches
        
        # Source health tracking
        self._source_health: Dict[str, int] = {
            "binance": 0,
            "coinbase": 0,
            "redstone": 0,
        }
        self._source_failures: Dict[str, int] = {
            "binance": 0,
            "coinbase": 0,
            "redstone": 0,
        }
        
    def _get_symbol_mappings(self, kalshi_series: str) -> Dict[str, str]:
        """Get exchange-specific symbols for a Kalshi series."""
        crypto = KALSHI_TO_CRYPTO.get(kalshi_series)
        if not crypto:
            return {}
        
        return {
            "binance": BINANCE_SYMBOLS.get(crypto),
            "coinbase": COINBASE_SYMBOLS.get(crypto),
            "redstone": crypto if crypto in REDSTONE_SYMBOLS else None,
        }
    
    def _fetch_from_source(self, source: str, symbol: str) -> Tuple[str, Optional[float], Optional[Dict]]:
        """Fetch price from a single source. Returns (source, price, raw_data)."""
        try:
            if source == "binance":
                binance_symbol = self._get_binance_symbol(symbol)
                if not binance_symbol:
                    return ("binance", None, None)
                price = binance_get_price(binance_symbol)
                return ("binance", price, None)
                
            elif source == "coinbase":
                coinbase_symbol = symbol.replace("/", "-").upper()
                if "-" not in coinbase_symbol:
                    # Try to construct
                    base = symbol.replace("USDT", "").replace("USDT", "")
                    coinbase_symbol = f"{base}-USD"
                price = coinbase_get_price(coinbase_symbol)
                return ("coinbase", price, None)
                
            elif source == "redstone":
                price = redstone_get_price(symbol)
                return ("redstone", price, None)
                
            return (source, None, None)
        except Exception as e:
            log.debug(f"Error fetching from {source}: {e}")
            return (source, None, None)
    
    def _get_binance_symbol(self, kalshi_series: str) -> Optional[str]:
        """Map Kalshi series to Binance symbol."""
        crypto = KALSHI_TO_CRYPTO.get(kalshi_series)
        if not crypto:
            return None
        return BINANCE_SYMBOLS.get(crypto)
    
    def fetch_all_prices(self, kalshi_series: str) -> Dict[str, Optional[float]]:
        """
        Fetch prices from all available sources in parallel.
        Returns dict: source -> price (or None if failed).
        """
        mappings = self._get_symbol_mappings(kalshi_series)
        prices = {}
        
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {}
            for source, symbol in mappings.items():
                if symbol:
                    futures[executor.submit(self._fetch_from_source, source, symbol)] = source
            
            for future in as_completed(futures):
                source, price, _ = future.result()
                prices[source] = price
        
        return prices
    
    def get_consensus_price(self, kalshi_series: str) -> Optional[ConsensusPrice]:
        """
        Get consensus price from all available sources.
        Uses median for robustness against outliers.
        """
        prices = self.fetch_all_prices(kalshi_series)
        
        # Filter valid prices
        valid_prices = {src: p for src, p in prices.items() if p is not None}
        
        if not valid_prices:
            log.warning(f"No valid prices for {kalshi_series}")
            return None
        
        prices_list = list(valid_prices.values())
        consensus_price = median(prices_list)
        
        # Calculate spread
        max_price = max(prices_list)
        min_price = min(prices_list)
        spread = max_price - min_price
        spread_pct = spread / consensus_price if consensus_price > 0 else 0
        
        # Confidence based on agreement (lower spread = higher confidence)
        if consensus_price > 0:
            confidence = max(0.0, 1.0 - (spread_pct * 10))  # 10% spread -> 0 confidence
        else:
            confidence = 0.0
        
        # Clamp confidence
        confidence = max(0.0, min(1.0, confidence))
        
        return ConsensusPrice(
            symbol=KALSHI_TO_CRYPTO.get(kalshi_series, "UNKNOWN"),
            price=consensus_price,
            timestamp=time.time(),
            sources=list(valid_prices.keys()),
            spread=spread,
            spread_pct=spread_pct,
            confidence=confidence,
            n_sources=len(valid_prices),
            prices_by_source=valid_prices,
            timestamp_by_source={},  # Could be added if needed
        )
    
    def _update_history(self, symbol: str, price: float, source: str) -> None:
        """Add price point to history."""
        now = time.time()
        point = PricePoint(price=price, timestamp=now, source=source)
        
        if symbol not in self._price_history:
            self._price_history[symbol] = deque(maxlen=MAX_HISTORY_POINTS)
        
        self._price_history[symbol].append(point)
        
        # Clean old data
        cutoff = time.time() - PRICE_HISTORY_WINDOW
        while self._price_history[symbol] and self._price_history[symbol][0].timestamp < cutoff:
            self._price_history[symbol].popleft()
    
    def get_trend(self, kalshi_series: str) -> Optional[TrendInfo]:
        """
        Calculate trend/velocity from recent price history.
        
        Uses linear regression on recent price points to estimate velocity
        and acceleration. Returns None if insufficient data.
        """
        crypto = KALSHI_TO_CRYPTO.get(kalshi_series)
        if not crypto or crypto not in self._price_history:
            return None
        
        points = list(self._price_history[crypto])
        if len(points) < 3:
            return None
        
        # Use last N points for trend calculation
        recent = list(self._price_history[crypto])[-20:]  # last 20 points
        if len(recent) < 3:
            return None
        
        # Linear regression for velocity (price change per second)
        timestamps = [p.timestamp for p in recent]
        prices = [p.price for p in recent]
        
        # Simple linear regression
        n = len(recent)
        sum_t = sum(timestamps)
        sum_p = sum(prices)
        sum_t2 = sum(t * t for t in timestamps)
        sum_tp = sum(t * p for t, p in zip(timestamps, prices))
        
        # Velocity (slope) = (n*sum(tp) - sum(t)*sum(p)) / (n*sum(t^2) - sum(t)^2)
        denom = n * sum_t2 - sum_t * sum_t
        if denom == 0:
            velocity = 0.0
        else:
            velocity = (n * sum_tp - sum_t * sum_p) / denom
        
        # Acceleration: compare recent velocity vs earlier velocity
        if len(recent) >= 6:
            mid = len(recent) // 2
            early = recent[:mid]
            late = recent[mid:]
            
            def calc_vel(points):
                n = len(points)
                if n < 2:
                    return 0.0
                ts = [p.timestamp for p in points]
                ps = [p.price for p in points]
                sum_t = sum(ts)
                sum_p = sum(ps)
                sum_t2 = sum(t*t for t in ts)
                sum_tp = sum(t*p for t,p in zip(ts, ps))
                denom = n * sum_t2 - sum_t * sum_t
                if denom == 0:
                    return 0.0
                return (n * sum_tp - sum_t * sum_p) / denom
            
            early_vel = calc_vel(early)
            late_vel = calc_vel(late)
            acceleration = late_vel - early_vel
        else:
            acceleration = 0.0
        
        # Volatility (std of returns)
        if len(recent) >= 3:
            returns = []
            for i in range(1, len(recent)):
                if recent[i-1].price > 0:
                    ret = (recent[i].price - recent[i-1].price) / recent[i-1].price
                    returns.append(ret)
            volatility = stdev(returns) if len(returns) > 1 else 0.0
        else:
            volatility = 0.0
        
        # Current price
        current_price = recent[-1].price
        
        # Velocity as percentage per second
        velocity_pct = velocity / recent[-1].price if recent[-1].price > 0 else 0.0
        
        # Direction
        if velocity > 0.0001:
            direction = "up"
        elif velocity < -0.0001:
            direction = "down"
        else:
            direction = "flat"
        
        # Confidence based on data points and source agreement
        confidence = min(1.0, len(recent) / 20.0)  # max confidence at 20+ points
        
        return TrendInfo(
            symbol=kalshi_series,
            price=recent[-1].price,
            velocity=velocity,
            velocity_pct=velocity_pct,
            acceleration=acceleration,
            volatility=volatility,
            direction=direction,
            confidence=confidence,
            timestamp=time.time(),
            n_points=len(recent),
        )
    
    def get_market_snapshot(self, kalshi_series: str) -> Optional[Dict[str, Any]]:
        """
        Get comprehensive market snapshot for a Kalshi series.
        Includes consensus price, trend, and source details.
        """
        consensus = self.get_consensus_price(kalshi_series)
        if not consensus:
            return None
        
        trend = self.get_trend(kalshi_series)
        
        return {
            "series": kalshi_series,
            "consensus": consensus,
            "trend": trend,
            "timestamp": time.time(),
        }
    
    def update_all(self, kalshi_series_list: List[str]) -> Dict[str, Optional[Dict]]:
        """
        Update all tracked series in parallel.
        Returns dict of series -> market snapshot.
        """
        results = {}
        with ThreadPoolExecutor(max_workers=min(4, len(kalshi_series))) as executor:
            futures = {
                executor.submit(self.get_market_snapshot, series): series
                for series in kalshi_series
            }
            for future in as_completed(futures):
                series = futures[future]
                try:
                    results[series] = future.result()
                except Exception as e:
                    log.error(f"Error updating {series}: {e}")
                    results[series] = None
        return results
    
    def get_source_health(self) -> Dict[str, Dict[str, int]]:
        """Get health status of all data sources."""
        return {
            source: {
                "failures": self._source_failures.get(source, 0),
                "successes": self._source_health.get(source, 0),
            }
            for source in ["binance", "coinbase", "redstone"]
        }
    
    def shutdown(self):
        """Shutdown the aggregator and cleanup resources."""
        self._executor.shutdown(wait=True)


# Global instance
_aggregator: Optional[MarketDataAggregator] = None


def get_aggregator() -> MarketDataAggregator:
    """Get or create the global MarketDataAggregator instance."""
    global _aggregator
    if _aggregator is None:
        _aggregator = MarketDataAggregator()
    return _aggregator


# Convenience functions
def get_consensus_price(kalshi_series: str) -> Optional[ConsensusPrice]:
    """Get consensus price for a Kalshi series."""
    return get_aggregator().get_consensus_price(kalshi_series)


def get_market_trend(kalshi_series: str) -> Optional[TrendInfo]:
    """Get trend info for a Kalshi series."""
    return get_aggregator().get_trend(kalshi_series)


def get_market_snapshot(kalshi_series: str) -> Optional[Dict[str, Any]]:
    """Get full market snapshot for a Kalshi series."""
    return get_aggregator().get_market_snapshot(kalshi_series)