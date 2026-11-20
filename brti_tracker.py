"""
brti_tracker.py — T15-11: Kalshi reference index (BRTI) tracker.

Kalshi 15-minute crypto markets settle on the CF Benchmarks BRTI (Bitcoin Real-Time Index)
60-second average. The strike is the BRTI 60s average at window OPEN, and the settlement
value is the BRTI 60s average at window CLOSE. YES wins if settlement >= strike (ties go YES).

Since we don't have direct real-time BRTI access under our license, this module builds
a proxy from constituent exchanges (Binance, Coinbase, OKX) that we can reach, and measures
proxy-vs-settlement error by hour to quantify basis risk.

CF Benchmarks BRTI methodology: https://www.cfbenchmarks.com/indices/brti (2026-11-20)
Kalshi KXBTC15M settlement rules: https://kalshi.com/markets/kxbtc15m (2026-11-20)

The proxy is a median-of-mids across available venues (matching binance_feed.composite_price),
which is a reasonable approximation of a volume-weighted index when weights are unknown.
We track the running 60-second average in the final minute so the model prices the
AVERAGE near expiry, not the last tick (locked_share logic).

Acceptance criteria from AGENT_TASKSv15 T15-11:
- docs/INDEX_PROXY_ERROR.md (MAE in bps by hour)
- proxy and BRTI-style average exposed as `index_ref` with Binance/Coinbase as fallback
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import config

log = logging.getLogger("brti_tracker")

# ── Configuration ──────────────────────────────────────────────────────────

# BRTI window size (60 seconds)
BRTI_WINDOW_SECONDS = 60
# Update interval for running average (seconds)
BRTI_UPDATE_INTERVAL = float(os.environ.get("BRTI_UPDATE_INTERVAL", "1.0"))
# Maximum age of venue price before considering it stale (seconds)
MAX_VENUE_AGE = float(os.environ.get("BRTI_MAX_VENUE_AGE", "5.0"))
# Minimum venues required for a valid proxy
MIN_VENUES = int(os.environ.get("BRTI_MIN_VENUES", "2"))
# Path for persisting proxy-vs-settlement error data
INDEX_PROXY_ERROR_PATH = os.path.join(
    getattr(config, "SUPERBOT_DATA_DIR", "/opt/superbot/local_data"),
    "brti_proxy_error.jsonl"
)

# Known bases for crypto
KNOWN_BASES = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ZEC", "NEAR"}

# ── Data structures ────────────────────────────────────────────────────────

@dataclass
class VenuePrice:
    """Price from a single venue with timestamp."""
    venue: str
    price: float
    timestamp: float  # time.monotonic()
    bid: Optional[float] = None
    ask: Optional[float] = None


@dataclass
class BRTIProxy:
    """Running BRTI-style 60-second average proxy."""
    symbol: str
    current_average: float           # Current 60s running average
    current_spot: float              # Latest median-of-mids spot
    venues_used: list[VenuePrice]    # Venue prices contributing to spot
    window_start: float              # time.monotonic() when window started
    last_update: float               # time.monotonic() of last update
    samples_in_window: int           # Number of spot samples in current 60s window
    staleness_flag: bool = False     # True if any venue data is stale
    cross_venue_deviation_bps: float = 0.0  # Cross-venue deviation in basis points


@dataclass
class ProxyErrorRecord:
    """Record of proxy vs actual settlement error."""
    timestamp: float
    symbol: str
    proxy_average: float
    settlement_value: float
    error_bps: float                 # Error in basis points (proxy - settlement) / settlement * 10000
    hour_of_day: int                 # UTC hour
    minutes_to_expiry: float         # Minutes left when proxy was recorded


# ── BRTI Proxy Tracker ─────────────────────────────────────────────────────

class BRTITracker:
    """
    Tracks BRTI-style 60-second running average for each symbol.

    Uses median-of-mids from multiple venues as a proxy for the volume-weighted
    BRTI index. Maintains a rolling 60-second window of spot samples.
    """

    def __init__(
        self,
        symbols: list[str] | None = None,
        venue_getters: dict[str, Callable[[str], tuple[Optional[float], Optional[float]]]] | None = None
    ):
        self.symbols = [s.upper() for s in (symbols or list(KNOWN_BASES))]
        self.venue_getters = venue_getters or {}
        self._running = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Per-symbol state
        self._proxies: dict[str, BRTIProxy] = {}
        self._spot_history: dict[str, deque] = {
            s: deque(maxlen=int(BRTI_WINDOW_SECONDS / BRTI_UPDATE_INTERVAL) + 10)
            for s in self.symbols
        }
        self._lock = threading.RLock()

        # Error tracking
        self._error_records: list[ProxyErrorRecord] = []
        self._error_lock = threading.RLock()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background tracker thread."""
        if self._running:
            return
        if not self.venue_getters:
            log.warning("BRTITracker: no venue getters configured — tracker disabled")
            return
        self._running = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="brti-tracker"
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the background thread."""
        self._running = False
        self._stop.set()
        if self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=5)
            except Exception as e:
                log.debug("BRTITracker shutdown error: %s", e)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _run_loop(self) -> None:
        """Background thread entry: runs asyncio event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main_async())
        finally:
            self._loop.close()
            self._loop = None

    async def _main_async(self) -> None:
        """Main async loop: polls venues and updates running averages."""
        while self._running and not self._stop.is_set():
            try:
                await self._poll_and_update()
            except Exception as e:
                log.warning("BRTITracker poll error: %s", e)
            await asyncio.sleep(BRTI_UPDATE_INTERVAL)

    async def _shutdown(self) -> None:
        """Cancel all running tasks."""
        for task in asyncio.all_tasks(self._loop):
            task.cancel()
        await asyncio.gather(*asyncio.all_tasks(self._loop), return_exceptions=True)

    # ── Core polling and update ────────────────────────────────────────────

    async def _poll_and_update(self) -> None:
        """Poll all venue getters and update proxies."""
        now = time.monotonic()
        with self._lock:
            for symbol in self.symbols:
                # Collect prices from all venues
                venue_prices: list[VenuePrice] = []
                for venue_name, getter in self.venue_getters.items():
                    try:
                        price, age = getter(symbol)
                        if price is not None and age is not None:
                            venue_prices.append(VenuePrice(
                                venue=venue_name,
                                price=price,
                                timestamp=now - age,  # Convert age back to absolute time
                            ))
                    except Exception as e:
                        log.debug("BRTITracker venue %s getter failed for %s: %s",
                                 venue_name, symbol, e)

                if not venue_prices:
                    continue

                # Compute median-of-mids spot
                prices = [vp.price for vp in venue_prices]
                prices_sorted = sorted(prices)
                n = len(prices_sorted)
                if n % 2 == 1:
                    spot = prices_sorted[n // 2]
                else:
                    spot = (prices_sorted[n // 2 - 1] + prices_sorted[n // 2]) / 2.0

                # Check cross-venue deviation
                prices_cents = [p * 100 for p in prices]
                deviation_cents = max(prices_cents) - min(prices_cents)
                deviation_bps = (deviation_cents / spot) * 10000 if spot > 0 else 0

                # Check staleness
                staleness = any(now - vp.timestamp > MAX_VENUE_AGE for vp in venue_prices)

                # Add to spot history
                self._spot_history[symbol].append((now, spot))

                # Compute 60-second running average
                window_start = now - BRTI_WINDOW_SECONDS
                window_samples = [
                    s for t, s in self._spot_history[symbol]
                    if t >= window_start
                ]
                if window_samples:
                    running_avg = statistics.mean(window_samples)
                else:
                    running_avg = spot

                # Update or create proxy
                proxy = self._proxies.get(symbol)
                if proxy is None:
                    proxy = BRTIProxy(
                        symbol=symbol,
                        current_average=running_avg,
                        current_spot=spot,
                        venues_used=venue_prices,
                        window_start=window_start,
                        last_update=now,
                        samples_in_window=len(window_samples),
                        staleness_flag=staleness,
                        cross_venue_deviation_bps=deviation_bps,
                    )
                    self._proxies[symbol] = proxy
                else:
                    proxy.current_average = running_avg
                    proxy.current_spot = spot
                    proxy.venues_used = venue_prices
                    proxy.window_start = window_start
                    proxy.last_update = now
                    proxy.samples_in_window = len(window_samples)
                    proxy.staleness_flag = staleness
                    proxy.cross_venue_deviation_bps = deviation_bps

    # ── Settlement recording ───────────────────────────────────────────────

    def record_settlement(
        self,
        symbol: str,
        settlement_value: float,
        minutes_to_expiry: float = 0.0
    ) -> None:
        """
        Record actual settlement value and compute proxy error.

        Call this when a market settles to build the proxy error dataset.
        """
        with self._lock:
            proxy = self._proxies.get(symbol.upper())
            if proxy is None:
                log.warning("BRTITracker: no proxy for %s at settlement", symbol)
                return

            proxy_average = proxy.current_average
            error_bps = ((proxy_average - settlement_value) / settlement_value) * 10000

        # Record error
        record = ProxyErrorRecord(
            timestamp=time.time(),
            symbol=symbol.upper(),
            proxy_average=proxy_average,
            settlement_value=settlement_value,
            error_bps=error_bps,
            hour_of_day=time.gmtime().tm_hour,
            minutes_to_expiry=minutes_to_expiry,
        )

        with self._error_lock:
            self._error_records.append(record)
            # Persist to file
            self._persist_error_record(record)

    def _persist_error_record(self, record: ProxyErrorRecord) -> None:
        """Append error record to JSONL file."""
        try:
            import json
            os.makedirs(os.path.dirname(INDEX_PROXY_ERROR_PATH), exist_ok=True)
            with open(INDEX_PROXY_ERROR_PATH, "a") as f:
                f.write(json.dumps({
                    "timestamp": record.timestamp,
                    "symbol": record.symbol,
                    "proxy_average": record.proxy_average,
                    "settlement_value": record.settlement_value,
                    "error_bps": record.error_bps,
                    "hour_of_day": record.hour_of_day,
                    "minutes_to_expiry": record.minutes_to_expiry,
                }) + "\n")
        except Exception as e:
            log.debug("BRTITracker persist error: %s", e)

    # ── Readers ────────────────────────────────────────────────────────────

    def get_proxy(self, symbol: str) -> Optional[BRTIProxy]:
        """Get current BRTI proxy for a symbol."""
        with self._lock:
            proxy = self._proxies.get(symbol.upper())
            if proxy is None:
                return None
            # Return a copy
            return BRTIProxy(
                symbol=proxy.symbol,
                current_average=proxy.current_average,
                current_spot=proxy.current_spot,
                venues_used=list(proxy.venues_used),
                window_start=proxy.window_start,
                last_update=proxy.last_update,
                samples_in_window=proxy.samples_in_window,
                staleness_flag=proxy.staleness_flag,
                cross_venue_deviation_bps=proxy.cross_venue_deviation_bps,
            )

    def get_index_ref(self, symbol: str) -> Optional[float]:
        """
        Get the BRTI-style reference price for a symbol.

        This is the running 60-second average, which is what the locked_share
        model should use for pricing near expiry.
        """
        proxy = self.get_proxy(symbol)
        return proxy.current_average if proxy else None

    def get_spot(self, symbol: str) -> Optional[float]:
        """Get the current median-of-mids spot price."""
        proxy = self.get_proxy(symbol)
        return proxy.current_spot if proxy else None

    def get_error_stats(self, symbol: str | None = None) -> dict:
        """Get proxy vs settlement error statistics."""
        with self._error_lock:
            records = [
                r for r in self._error_records
                if symbol is None or r.symbol == symbol.upper()
            ]

        if not records:
            return {"count": 0}

        errors = [r.error_bps for r in records]
        mae = statistics.mean(abs(e) for e in errors)
        mse = statistics.mean(e * e for e in errors)
        rmse = math.sqrt(mse)

        # By hour
        by_hour: dict[int, list[float]] = {}
        for r in records:
            by_hour.setdefault(r.hour_of_day, []).append(abs(r.error_bps))

        return {
            "count": len(records),
            "mae_bps": mae,
            "rmse_bps": rmse,
            "mean_error_bps": statistics.mean(errors),
            "std_error_bps": statistics.stdev(errors) if len(errors) > 1 else 0,
            "by_hour": {h: statistics.mean(v) for h, v in by_hour.items()},
        }

    def export_error_report(self, path: str = INDEX_PROXY_ERROR_PATH) -> None:
        """Export error analysis to markdown file."""
        with self._error_lock:
            records = list(self._error_records)

        if not records:
            return

        stats = self.get_error_stats()
        by_symbol: dict[str, list[float]] = {}
        by_hour: dict[int, list[float]] = {}

        for r in records:
            by_symbol.setdefault(r.symbol, []).append(abs(r.error_bps))
            by_hour.setdefault(r.hour_of_day, []).append(abs(r.error_bps))

        try:
            os.makedirs(os.path.dirname(path.replace(".jsonl", ".md")), exist_ok=True)
            md_path = path.replace(".jsonl", ".md")
            with open(md_path, "w") as f:
                f.write("# BRTI Proxy vs Settlement Error Report\n\n")
                f.write(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n\n")
                f.write("## Overall Statistics\n\n")
                f.write(f"- **Total settlements**: {stats['count']}\n")
                f.write(f"- **MAE (bps)**: {stats['mae_bps']:.2f}\n")
                f.write(f"- **RMSE (bps)**: {stats['rmse_bps']:.2f}\n")
                f.write(f"- **Mean Error (bps)**: {stats['mean_error_bps']:.2f}\n")
                f.write(f"- **Std Error (bps)**: {stats['std_error_bps']:.2f}\n\n")

                f.write("## By Symbol\n\n")
                f.write("| Symbol | Count | MAE (bps) |\n")
                f.write("|--------|-------|-----------|\n")
                for sym, errs in by_symbol.items():
                    f.write(f"| {sym} | {len(errs)} | {statistics.mean(errs):.2f} |\n")
                f.write("\n")

                f.write("## By Hour (UTC)\n\n")
                f.write("| Hour | Count | MAE (bps) |\n")
                f.write("|------|-------|-----------|\n")
                for hour in sorted(by_hour.keys()):
                    errs = by_hour[hour]
                    f.write(f"| {hour:02d}:00 | {len(errs)} | {statistics.mean(errs):.2f} |\n")

            log.info("BRTI error report exported to %s", md_path)
        except Exception as e:
            log.warning("BRTITracker export error: %s", e)

    def is_healthy(self) -> bool:
        """Check if tracker has recent data for at least one symbol."""
        with self._lock:
            now = time.monotonic()
            for proxy in self._proxies.values():
                if now - proxy.last_update < MAX_VENUE_AGE * 2:
                    return True
        return False


# ── Module-level singleton ─────────────────────────────────────────────────

_brti_tracker: Optional[BRTITracker] = None


def get_brti_tracker(
    symbols: list[str] | None = None,
    venue_getters: dict[str, Callable] | None = None
) -> BRTITracker:
    """Get or create the module-level BRTITracker."""
    global _brti_tracker
    if _brti_tracker is None:
        _brti_tracker = BRTITracker(symbols, venue_getters)
    return _brti_tracker


def start_brti_tracker(
    symbols: list[str] | None = None,
    venue_getters: dict[str, Callable] | None = None
) -> None:
    """Start the module-level BRTITracker."""
    get_brti_tracker(symbols, venue_getters).start()


def stop_brti_tracker() -> None:
    """Stop the module-level BRTITracker."""
    global _brti_tracker
    if _brti_tracker is not None:
        _brti_tracker.stop()
        _brti_tracker = None


# ── Compatibility / convenience ────────────────────────────────────────────

def get_brti_proxy(symbol: str) -> Optional[float]:
    """Get current BRTI proxy (60s running average) for a symbol."""
    tracker = get_brti_tracker()
    return tracker.get_index_ref(symbol)


def get_brti_spot(symbol: str) -> Optional[float]:
    """Get current median-of-mids spot for a symbol."""
    tracker = get_brti_tracker()
    return tracker.get_spot(symbol)