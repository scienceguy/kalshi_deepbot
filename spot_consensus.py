"""spot_consensus.py — D3: Cross-venue spot consensus with staleness filter.

Provides a unified spot price from multiple venues (Binance, Coinbase, OKX, Kraken)
using median of mids, with staleness detection and cross-venue deviation flagging.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import config

log = logging.getLogger(__name__)

# Maximum age of a venue price before it's considered stale (seconds)
MAX_VENUE_AGE_SECONDS = float(config.SPOT_MAX_AGE_SECONDS if hasattr(config, 'SPOT_MAX_AGE_SECONDS') else 5.0)
# Maximum deviation between venues before flagging (cents)
MAX_VENUE_DEVIATION_CENTS = float(config.SPOT_MAX_DEVIATION_CENTS if hasattr(config, 'SPOT_MAX_DEVIATION_CENTS') else 50.0)


@dataclass
class VenuePrice:
    """Price from a single venue with metadata."""
    venue: str
    price: float
    age_seconds: float
    bid: Optional[float] = None
    ask: Optional[float] = None


@dataclass
class SpotConsensus:
    """Result of spot consensus calculation."""
    price: float                    # Median of mids
    venues_used: list[VenuePrice]   # Venue prices that contributed
    cross_venue_flag: bool          # True if deviation > threshold
    staleness_flag: bool            # True if any venue price is stale
    timestamp: float                # time.monotonic() when computed


def _median(values: list[float]) -> Optional[float]:
    """Return median of a list, or None if empty."""
    if not values:
        return None
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n % 2 == 1:
        return sorted_vals[n // 2]
    return (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0


def compute_spot_consensus(
    venue_prices: dict[str, VenuePrice],
    max_age_seconds: float = MAX_VENUE_AGE_SECONDS,
    max_deviation_cents: float = MAX_VENUE_DEVIATION_CENTS,
) -> Optional[SpotConsensus]:
    """
    Compute consensus spot price from venue prices.
    
    Args:
        venue_prices: Dict of venue_name -> VenuePrice
        max_age_seconds: Max age before a venue price is considered stale
        max_deviation_cents: Max deviation between venues (in cents) before flagging
    
    Returns:
        SpotConsensus with median price, flags, and metadata, or None if no valid prices.
    """
    now = time.monotonic()
    fresh_prices: list[VenuePrice] = []
    staleness_flag = False
    
    for venue, vp in venue_prices.items():
        age = now - vp.age_seconds if vp.age_seconds > 0 else float('inf')
        if age > max_age_seconds:
            log.debug("Venue %s price is stale (age=%.1fs > %.1fs)", venue, age, max_age_seconds)
            staleness_flag = True
            continue
        fresh_prices.append(VenuePrice(
            venue=venue,
            price=vp.price,
            age_seconds=age,
            bid=vp.bid,
            ask=vp.ask,
        ))
    
    if not fresh_prices:
        log.warning("No fresh venue prices available for spot consensus")
        return None
    
    # Compute median of mids
    mids = [vp.price for vp in fresh_prices]
    median_price = _median(mids)
    
    if median_price is None:
        return None
    
    # Check cross-venue deviation
    prices_cents = [vp.price * 100 for vp in fresh_prices]
    min_price = min(prices_cents)
    max_price = max(prices_cents)
    deviation_cents = max_price - min_price
    cross_venue_flag = deviation_cents > max_deviation_cents
    
    if cross_venue_flag:
        log.warning("Cross-venue deviation %.1f cents > threshold %.1f cents (venues: %s)",
                    deviation_cents, max_deviation_cents,
                    [vp.venue for vp in fresh_prices])
    
    return SpotConsensus(
        price=median_price,
        venues_used=fresh_prices,
        cross_venue_flag=cross_venue_flag,
        staleness_flag=staleness_flag,
        timestamp=now,
    )


class SpotConsensusProvider:
    """
    Provides spot consensus by aggregating prices from multiple venue feeds.
    
    Usage:
        provider = SpotConsensusProvider()
        # Register venue price getters
        provider.register_venue("binance", binance_feed.spot_with_age)
        provider.register_venue("coinbase", coinbase_feed.spot_with_age)
        # Get consensus
        consensus = provider.get_consensus("BTC")
    """
    
    def __init__(self):
        self._venue_getters: dict[str, callable] = {}
    
    def register_venue(self, name: str, getter: callable) -> None:
        """Register a venue price getter function.
        
        Getter should return (price, age_seconds) or (None, None).
        """
        self._venue_getters[name] = getter
    
    def unregister_venue(self, name: str) -> None:
        """Unregister a venue."""
        self._venue_getters.pop(name, None)
    
    def get_consensus(self, symbol: str) -> Optional[SpotConsensus]:
        """Get spot consensus for a symbol."""
        venue_prices: dict[str, VenuePrice] = {}
        
        for venue, getter in self._venue_getters.items():
            try:
                price, age = getter(symbol)
                if price is not None and age is not None:
                    venue_prices[venue] = VenuePrice(
                        venue=venue,
                        price=price,
                        age_seconds=age,
                    )
            except Exception as e:
                log.debug("Venue %s getter failed for %s: %s", venue, symbol, e)
        
        return compute_spot_consensus(venue_prices)


# Global provider instance
_default_provider: Optional[SpotConsensusProvider] = None


def get_provider() -> SpotConsensusProvider:
    """Get or create the default spot consensus provider."""
    global _default_provider
    if _default_provider is None:
        _default_provider = SpotConsensusProvider()
    return _default_provider


def get_spot_consensus(symbol: str) -> Optional[SpotConsensus]:
    """Convenience function to get spot consensus using the default provider."""
    return get_provider().get_consensus(symbol)