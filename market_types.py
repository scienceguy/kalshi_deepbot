"""market_types.py - what a cycle could trade vs what it will trade.

These are deliberately different numbers. `available` counts the markets this
process actually fetched and that passed the static price band; `tradable`
counts the markets that passed selection and are open for an entry now.

A healthy idle cycle has a large `available` and `tradable == 0` - that is the
strategy choosing not to trade, not a bug. Collapsing the two into one "found N
markets" line is how an empty book goes unnoticed for thousands of cycles.

The split is per-process on purpose: a cycle that queries several series can
have one series fail to fetch while the others still report markets, so only a
count taken inside this process may be called `available`.
"""

from dataclasses import dataclass, field


@dataclass
class MarketCounts:
    """One cycle's market census."""

    available: int = 0        # fetched, passed the static band, has a quote
    tradable: int = 0         # passed selection, open for an entry now
    fetch_errors: int = 0     # series whose /markets fetch failed outright
    by_series: dict = field(default_factory=dict)  # series -> (available, tradable)

    def add_series(self, series: str, fetched: int, tradable: int) -> None:
        self.available += fetched
        self.tradable += tradable
        self.by_series[series] = (fetched, tradable)

    def fail_series(self, series: str) -> None:
        self.fetch_errors += 1
        self.by_series[series] = (0, 0)

    @property
    def empty_book(self) -> bool:
        """True when markets existed but none had a quotable price.

        This is the failure that a `tradable == 0` count alone cannot show: it
        looks exactly like a quiet market. It deserves a warning, not a debug.
        """
        return self.available == 0 and self.fetch_errors == 0

    def __str__(self) -> str:
        return "available=%d tradable=%d fetch_errors=%d" % (
            self.available, self.tradable, self.fetch_errors)
