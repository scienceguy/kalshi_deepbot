"""Quote freshness gates adapted from hu553in/polybot's Chainlink RTDS gates.

Tracks the last price and timestamp per symbol (last_price_ts) and exposes
freshness checks: a quote is fresh when its age is within max_age_seconds, and
stale quotes fall back to "no price" (None) so a strategy never trades on a
dead feed.
"""

import time


class QuoteFreshness:
    """Track last_price_ts per symbol with staleness gating.

    update(symbol, price, timestamp) records a quote (timestamp defaults to
    now). is_fresh(symbol, max_age_seconds) is True when the last update is
    within the threshold. get_price(symbol) returns the price only while fresh
    against the instance's default staleness horizon, else None.
    staleness_seconds(symbol) returns the age of the last quote, or None when
    the symbol has never been seen.
    """

    def __init__(self, default_max_age_seconds=60.0, now=None):
        self._quotes = {}
        self.default_max_age_seconds = float(default_max_age_seconds)
        self._now = now if now is not None else time.time

    def update(self, symbol, price, timestamp=None):
        """Record price and timestamp for symbol. timestamp defaults to now."""
        ts = self._now() if timestamp is None else float(timestamp)
        self._quotes[symbol] = {"price": float(price), "ts": ts}

    def is_fresh(self, symbol, max_age_seconds):
        """True if symbol's last update is within max_age_seconds."""
        q = self._quotes.get(symbol)
        if q is None:
            return False
        return (self._now() - q["ts"]) <= float(max_age_seconds)

    def get_price(self, symbol):
        """Return last price if fresh within the default horizon, else None."""
        q = self._quotes.get(symbol)
        if q is None:
            return None
        if (self._now() - q["ts"]) > self.default_max_age_seconds:
            return None
        return q["price"]

    def staleness_seconds(self, symbol):
        """Seconds since the last update, or None if the symbol is unknown."""
        q = self._quotes.get(symbol)
        if q is None:
            return None
        return self._now() - q["ts"]
