"""
tick_bus.py — T15-10: Event-driven spot snapshot (Binance WS, Coinbase WS).

A single in-process tick bus (asyncio + uvloop, bounded queues, drop-oldest on
slow consumers) feeding a lock-free latest-snapshot per symbol. Binance and
Coinbase WS feeds with sequence/gap detection, jittered reconnect, staleness
flag. Maintains ATR and sigma incrementally from ticks in memory.

Binance WS docs: https://binance-docs.github.io/apidocs/spot/en/#websocket-market-streams (2026-11-20)
Coinbase WS docs: https://docs.cdp.coinbase.com/advanced-trade/docs/ws (2026-11-20)

Read current Coinbase and Binance docs for channels and auth and record
the doc URL and date in each module header.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import uvloop

# ── Configuration ──────────────────────────────────────────────────────────
# Maximum queue size per consumer before dropping oldest
MAX_QUEUE_SIZE = int(os.environ.get("TICK_BUS_MAX_QUEUE", "10000"))
# Staleness threshold for entry decisions (seconds)
WS_SPOT_STALE_SECONDS = float(os.environ.get("WS_SPOT_STALE_SECONDS", "1.0"))
# Binance WS endpoint
BINANCE_WS_URL = os.environ.get(
    "BINANCE_WS_URL", "wss://data-stream.binance.vision/ws"
)
# Coinbase WS endpoint
COINBASE_WS_URL = os.environ.get(
    "COINBASE_WS_URL", "wss://advanced-trade-ws.coinbase.com"
)
# Known bases we track
KNOWN_BASES = {
    "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ZEC", "NEAR"
}
# Max reconnection retries (None = infinite)
BINANCE_WS_MAX_RETRIES = int(os.environ.get("BINANCE_WS_MAX_RETRIES", "100"))
COINBASE_WS_MAX_RETRIES = int(os.environ.get("COINBASE_WS_MAX_RETRIES", "100"))
# ATR window size (number of ticks)
ATR_WINDOW = int(os.environ.get("TICK_BUS_ATR_WINDOW", "60"))
# Sigma window size (number of 1-minute bars)
SIGMA_WINDOW = int(os.environ.get("TICK_BUS_SIGMA_WINDOW", "60"))
# Use CF Benchmarks settlement index (Kalshi CF Benchmarks WS channels)
USE_CFB_INDEX = os.environ.get("USE_CFB_INDEX", "").lower() in ("1", "true", "yes", "on")
# Staleness threshold for CF Benchmarks index values (seconds)
WS_INDEX_STALE_SECONDS = float(os.environ.get("WS_INDEX_STALE_SECONDS", "1.0"))
# Bounded trade tape size per symbol (newest last)
TRADE_TAPE_SIZE = int(os.environ.get("TRADE_TAPE_SIZE", "500"))

log = logging.getLogger("tick_bus")

# ── Data structures ────────────────────────────────────────────────────────

@dataclass
class Tick:
    """Single market tick from a venue."""
    symbol: str           # Base asset, e.g., "BTC"
    venue: str            # "binance" or "coinbase"
    price: float          # Mid price (or trade price)
    bid: Optional[float] = None
    ask: Optional[float] = None
    timestamp: float = field(default_factory=time.monotonic)  # Local receive time (monotonic)
    venue_timestamp: Optional[float] = None  # Venue-provided timestamp if available
    sequence: Optional[int] = None  # Sequence number for gap detection


@dataclass
class Snapshot:
    """Latest consolidated snapshot for a symbol."""
    symbol: str
    price: float              # Median mid across venues
    bid: Optional[float] = None
    ask: Optional[float] = None
    venues: dict = field(default_factory=dict)  # venue -> Tick
    timestamp: float = field(default_factory=time.monotonic)
    atr: Optional[float] = None           # Average True Range (dollars)
    sigma_per_min: Optional[float] = None  # Per-minute log-return sigma
    staleness_flag: bool = False           # True if any venue price is stale


@dataclass
class VenueState:
    """Per-venue connection state."""
    name: str
    connected: bool = False
    last_sequence: Optional[int] = None
    gaps_detected: int = 0
    last_message_at: float = 0.0
    reconnect_count: int = 0


# ── Tick Bus (core) ────────────────────────────────────────────────────────

class TickBus:
    """
    In-process tick bus with bounded queues and lock-free snapshots.

    Architecture:
    - Single asyncio event loop (uvloop) in a background thread
    - Multiple venue feed tasks publishing to internal queues
    - Snapshot builder task consolidating venue ticks into per-symbol snapshots
    - ATR/Sigma calculator updating incrementally
    - Thread-safe read access via snapshot copies
    """

    def __init__(self, symbols: list[str] | None = None):
        self.symbols = [s.upper() for s in (symbols or list(KNOWN_BASES))]
        self._running = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Per-symbol latest snapshot (lock-free reads via copy-on-write)
        self._snapshots: dict[str, Snapshot] = {}
        self._snapshot_lock = threading.RLock()

        # Venue states
        self._venue_states: dict[str, VenueState] = {
            "binance": VenueState(name="binance"),
            "coinbase": VenueState(name="coinbase"),
        }

        # Per-venue, per-symbol latest tick cache (updated by feed tasks)
        self._latest_ticks: dict[str, dict[str, Tick]] = {
            venue: {s: None for s in self.symbols}
            for venue in ("binance", "coinbase")
        }
        self._latest_ticks_lock = threading.RLock()

        # ATR/Sigma state per symbol
        self._atr_state: dict[str, deque] = {
            s: deque(maxlen=ATR_WINDOW) for s in self.symbols
        }
        self._returns_state: dict[str, deque] = {
            s: deque(maxlen=SIGMA_WINDOW) for s in self.symbols
        }
        self._last_price: dict[str, float] = {}

        # Trade tape: bounded per-symbol deque of trades (newest last)
        self._trade_tape: dict[str, deque] = {
            s: deque(maxlen=TRADE_TAPE_SIZE) for s in self.symbols
        }
        self._trade_tape_lock = threading.RLock()

        # CF Benchmarks index cache: index_id -> (value, monotonic receive time)
        self._index_cache: dict[str, tuple[float, float]] = {}
        self._index_lock = threading.RLock()

        # Consumer queues (for any downstream tasks that want raw ticks)
        self._consumer_queues: list[asyncio.Queue] = []

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background tick bus thread. Idempotent."""
        if self._running:
            return
        self._running = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="tick-bus"
        )
        self._thread.start()
        # Wait briefly for connection to establish
        time.sleep(0.5)

    def stop(self) -> None:
        """Stop the background thread and close websockets."""
        self._running = False
        self._stop.set()
        if self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=5)
            except Exception as e:
                log.debug("TickBus shutdown error: %s", e)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _run_loop(self) -> None:
        """Background thread entry: runs uvloop event loop."""
        uvloop.install()
        self._loop = uvloop.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main_async())
        finally:
            self._loop.close()
            self._loop = None

    async def _main_async(self) -> None:
        """Main async entry: spawns feed tasks and snapshot builder."""
        tasks = [
            asyncio.create_task(self._binance_feed_task()),
            asyncio.create_task(self._coinbase_feed_task()),
            asyncio.create_task(self._snapshot_builder_task()),
            asyncio.create_task(self._atr_sigma_task()),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _shutdown(self) -> None:
        """Cancel all running tasks."""
        for task in asyncio.all_tasks(self._loop):
            task.cancel()
        await asyncio.gather(*asyncio.all_tasks(self._loop), return_exceptions=True)

    # ── Venue Feed Tasks ──────────────────────────────────────────────────

    async def _binance_feed_task(self) -> None:
        """Binance combined bookTicker + aggTrade streams with gap detection."""
        if is_test_mode():
            # In test mode, just keep the task alive without connecting
            venue_state = self._venue_states["binance"]
            while self._running and not self._stop.is_set():
                await asyncio.sleep(1.0)
            return

        sym_streams = [
            s.lower() if s.lower().endswith(("usd", "usdt")) else f"{s.lower()}usd"
            for s in self.symbols
        ]
        streams = "/".join(f"{s}@bookTicker/{s}@aggTrade" for s in sym_streams)
        url = f"{BINANCE_WS_URL}?streams={streams}"

        venue_state = self._venue_states["binance"]
        backoff = 2.0
        retries = 0
        max_retries = BINANCE_WS_MAX_RETRIES

        while self._running and not self._stop.is_set():
            try:
                await self._binance_ws_async(url, venue_state)
                backoff = 2.0
                retries = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                retries += 1
                venue_state.connected = False
                if max_retries is not None and retries >= max_retries:
                    log.error("Binance WS max retries (%d) exceeded — giving up", max_retries)
                    return
                # Jittered backoff
                sleep_sec = min(backoff * (0.8 + 0.4 * (time.time() % 1.0)), 60.0)
                log.warning(
                    "Binance WS disconnected: %s — reconnecting in %.1fs (retry %d/%s)",
                    e, sleep_sec, retries, max_retries if max_retries else "inf"
                )
                await asyncio.sleep(sleep_sec)
                backoff = min(backoff * 2.0, 60.0)
                venue_state.reconnect_count += 1

    async def _binance_ws_async(self, url: str, venue_state: VenueState) -> None:
        import websockets
        async with websockets.connect(
            url, open_timeout=10, close_timeout=10, ping_interval=20
        ) as ws:
            venue_state.connected = True
            log.info("Binance WS connected")
            async for raw in ws:
                if not self._running or self._stop.is_set():
                    break
                try:
                    msg = json.loads(raw)
                    stream = msg.get("stream", "")
                    data = msg.get("data", {})

                    if "@bookTicker" in stream:
                        # bookTicker: {'u': 12345, 's': 'BTCUSDT', 'b': '50000.00', 'B': '1.5', 'a': '50001.00', 'A': '1.2'}
                        sym = data.get("s", "").replace("USDT", "").replace("USD", "").upper()
                        bid = data.get("b")
                        ask = data.get("a")
                        seq = data.get("u")  # Update ID as sequence
                        if sym and bid is not None and ask is not None:
                            bid_f = float(bid)
                            ask_f = float(ask)
                            mid = (bid_f + ask_f) / 2.0
                            tick = Tick(
                                symbol=sym,
                                venue="binance",
                                price=mid,
                                bid=bid_f,
                                ask=ask_f,
                                sequence=seq if isinstance(seq, int) else None,
                            )
                            await self._publish_tick(tick)
                            self._check_gap(venue_state, seq)

                    elif "@aggTrade" in stream:
                        # aggTrade: {'e': 'aggTrade', 'E': 123456789, 'a': 12345, 's': 'BTCUSDT', 'p': '50000.00', 'q': '1.5', 'f': 123, 'l': 456, 'T': 123456789, 'm': True, 'M': True}
                        sym = data.get("s", "").replace("USDT", "").replace("USD", "").upper()
                        px = data.get("p")
                        if sym and px is not None:
                            price = float(px)
                            tick = Tick(
                                symbol=sym,
                                venue="binance",
                                price=price,
                                sequence=data.get("a") if isinstance(data.get("a"), int) else None,
                            )
                            await self._publish_tick(tick)

                except Exception:
                    continue
            venue_state.connected = False

    async def _coinbase_feed_task(self) -> None:
        """Coinbase Advanced Trade ticker channel with gap detection."""
        if is_test_mode():
            # In test mode, just keep the task alive without connecting
            venue_state = self._venue_states["coinbase"]
            while self._running and not self._stop.is_set():
                await asyncio.sleep(1.0)
            return

        products = [f"{s}-USD" for s in self.symbols]
        subscribe = {
            "type": "subscribe",
            "channel": "ticker",
            "product_ids": products,
        }

        venue_state = self._venue_states["coinbase"]
        backoff = 2.0
        retries = 0
        max_retries = COINBASE_WS_MAX_RETRIES

        while self._running and not self._stop.is_set():
            try:
                await self._coinbase_ws_async(COINBASE_WS_URL, subscribe, venue_state)
                backoff = 2.0
                retries = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                retries += 1
                venue_state.connected = False
                if max_retries is not None and retries >= max_retries:
                    log.error("Coinbase WS max retries (%d) exceeded — giving up", max_retries)
                    return
                sleep_sec = min(backoff * (0.8 + 0.4 * (time.time() % 1.0)), 60.0)
                log.warning(
                    "Coinbase WS disconnected: %s — reconnecting in %.1fs (retry %d/%s)",
                    e, sleep_sec, retries, max_retries if max_retries else "inf"
                )
                await asyncio.sleep(sleep_sec)
                backoff = min(backoff * 2.0, 60.0)
                venue_state.reconnect_count += 1

    async def _coinbase_ws_async(self, url: str, subscribe: dict, venue_state: VenueState) -> None:
        import websockets
        async with websockets.connect(
            url, open_timeout=10, close_timeout=10, ping_interval=20
        ) as ws:
            await ws.send(json.dumps(subscribe))
            venue_state.connected = True
            log.info("Coinbase WS connected")
            async for raw in ws:
                if not self._running or self._stop.is_set():
                    break
                try:
                    msg = json.loads(raw)
                    if msg.get("channel") == "ticker":
                        for ev in msg.get("events", []):
                            for t in ev.get("tickers", []):
                                sym = t.get("product_id", "").replace("-USD", "").upper()
                                px = t.get("price")
                                bid = t.get("best_bid")
                                ask = t.get("best_ask")
                                seq = t.get("sequence")  # Coinbase provides sequence
                                if sym and px is not None:
                                    price = float(px)
                                    bid_f = float(bid) if bid is not None else None
                                    ask_f = float(ask) if ask is not None else None
                                    tick = Tick(
                                        symbol=sym,
                                        venue="coinbase",
                                        price=price,
                                        bid=bid_f,
                                        ask=ask_f,
                                        sequence=seq if isinstance(seq, int) else None,
                                    )
                                    await self._publish_tick(tick)
                                    self._check_gap(venue_state, seq)
                except Exception:
                    continue
            venue_state.connected = False

    # ── Gap Detection ──────────────────────────────────────────────────────

    def _check_gap(self, venue_state: VenueState, sequence: Optional[int]) -> None:
        """Detect sequence gaps in venue feed."""
        if sequence is None or venue_state.last_sequence is None:
            venue_state.last_sequence = sequence
            return
        if sequence != venue_state.last_sequence + 1:
            gap = sequence - venue_state.last_sequence - 1
            venue_state.gaps_detected += gap
            log.warning(
                "%s WS gap detected: expected %d, got %d (gap=%d, total gaps=%d)",
                venue_state.name, venue_state.last_sequence + 1, sequence, gap, venue_state.gaps_detected
            )
        venue_state.last_sequence = sequence

    # ── Tick Publishing ────────────────────────────────────────────────────

    async def _publish_tick(self, tick: Tick) -> None:
        """Publish tick to all consumer queues (drop-oldest on full) and update cache."""
        venue_state = self._venue_states.get(tick.venue)
        if venue_state:
            venue_state.last_message_at = time.monotonic()

        # Update per-venue latest tick cache
        with self._latest_ticks_lock:
            if tick.symbol in self._latest_ticks[tick.venue]:
                self._latest_ticks[tick.venue][tick.symbol] = tick

        # Push to consumer queues (non-blocking, drop oldest if full)
        for queue in self._consumer_queues:
            try:
                queue.put_nowait(tick)
            except asyncio.QueueFull:
                # Drop oldest to make room for newest
                try:
                    queue.get_nowait()
                    queue.put_nowait(tick)
                except Exception:
                    pass

    def subscribe(self) -> asyncio.Queue:
        """Create a new consumer queue for raw ticks. Call from async context."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._consumer_queues.append(queue)
        return queue

    # ── Thread-safe ingestion API (callable from any thread, no loop required) ──

    def ingest_tick(
        self,
        symbol: str,
        venue: str,
        price: float,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        venue_timestamp: Optional[float] = None,
        sequence: Optional[int] = None,
    ) -> Tick:
        """Ingest a venue tick into the bus.

        Thread-safe and works with no background event loop running. Updates the
        per-venue latest-tick cache so get_snapshot/get_snapshot_with_age reflect
        it on the next snapshot build. Accepts any venue string (dynamic venue
        support for cryptofeed-normalized venues).
        """
        symbol = symbol.upper()
        tick = Tick(
            symbol=symbol,
            venue=venue,
            price=float(price),
            bid=None if bid is None else float(bid),
            ask=None if ask is None else float(ask),
            timestamp=time.monotonic(),
            venue_timestamp=venue_timestamp,
            sequence=sequence,
        )
        with self._latest_ticks_lock:
            venue_ticks = self._latest_ticks.setdefault(venue, {})
            venue_ticks[symbol] = tick
        with self._snapshot_lock:
            if venue not in self._venue_states:
                self._venue_states[venue] = VenueState(name=venue)
            self._venue_states[venue].last_message_at = time.monotonic()
        return tick

    def ingest_trade(
        self, symbol: str, venue: str, price: float, size: float, side: str
    ) -> dict:
        """Ingest a trade into the bounded per-symbol trade tape (newest last)."""
        symbol = symbol.upper()
        trade = {
            "symbol": symbol,
            "venue": venue,
            "price": float(price),
            "size": float(size),
            "side": side,
            "timestamp": time.monotonic(),
        }
        with self._trade_tape_lock:
            tape = self._trade_tape.setdefault(symbol, deque(maxlen=TRADE_TAPE_SIZE))
            tape.append(trade)
        return trade

    def get_trade_tape(self, symbol: str, limit: Optional[int] = None) -> list:
        """Return the trade tape for a symbol (newest last), optionally truncated to limit."""
        symbol = symbol.upper()
        with self._trade_tape_lock:
            tape = self._trade_tape.get(symbol)
            if not tape:
                return []
            items = list(tape)
        if limit is not None:
            items = items[-limit:]
        return items

    def ingest_index(self, index_id: str, value: float,
                     source_timestamp: Optional[float] = None) -> None:
        """Ingest a CF Benchmarks index value (e.g. BRTI)."""
        receive_ts = time.monotonic()
        with self._index_lock:
            self._index_cache[index_id] = (float(value), receive_ts)

    def get_index_ref(self, index_id: str) -> Optional[float]:
        """Return the latest CF Benchmarks index value, or None if never ingested."""
        with self._index_lock:
            entry = self._index_cache.get(index_id)
        if entry is None:
            return None
        return entry[0]

    def get_index_with_age(self, index_id: str) -> tuple[Optional[float], Optional[float]]:
        """Return (value, age_seconds) for a CF Benchmarks index, or (None, None)."""
        with self._index_lock:
            entry = self._index_cache.get(index_id)
        if entry is None:
            return None, None
        value, receive_ts = entry
        age = max(0.0, time.monotonic() - receive_ts)
        return value, age

    def index_is_stale(self, index_id: str,
                       stale_seconds: Optional[float] = None) -> bool:
        """Return True if a CF Benchmarks index has no value or is older than the threshold."""
        if stale_seconds is None:
            stale_seconds = WS_INDEX_STALE_SECONDS
        value, age = self.get_index_with_age(index_id)
        if value is None:
            return True
        return age > stale_seconds

    # ── Snapshot Builder ──────────────────────────────────────────────────

    async def _snapshot_builder_task(self) -> None:
        """Consolidate venue ticks into per-symbol snapshots."""
        while self._running and not self._stop.is_set():
            await asyncio.sleep(0.01)  # 10ms tick
            self._build_snapshots()

    def _build_snapshots(self) -> None:
        """Build latest snapshot for each symbol from venue ticks."""
        now = time.monotonic()
        with self._latest_ticks_lock:
            with self._snapshot_lock:
                for symbol in self.symbols:
                    venue_ticks_result: dict = {}
                    prices = []
                    bids = []
                    asks = []

                    # Collect latest tick from each venue (any venue, not just binance/coinbase)
                    for venue_name, vt in self._latest_ticks.items():
                        tick = vt.get(symbol)
                        if tick is not None:
                            venue_ticks_result[venue_name] = tick
                            prices.append(tick.price)
                            if tick.bid is not None:
                                bids.append(tick.bid)
                            if tick.ask is not None:
                                asks.append(tick.ask)

                    if not prices:
                        continue

                    # Median of mids across venues
                    prices_sorted = sorted(prices)
                    n = len(prices_sorted)
                    if n % 2 == 1:
                        median_price = prices_sorted[n // 2]
                    else:
                        median_price = (prices_sorted[n // 2 - 1] + prices_sorted[n // 2]) / 2.0

                    # Best bid/ask across venues
                    best_bid = max(bids) if bids else None
                    best_ask = min(asks) if asks else None

                    snap = self._snapshots.get(symbol)
                    if snap is None:
                        snap = Snapshot(
                            symbol=symbol,
                            price=median_price,
                            bid=best_bid,
                            ask=best_ask,
                            venues=venue_ticks_result,
                            timestamp=now,
                        )
                        self._snapshots[symbol] = snap
                    else:
                        snap.price = median_price
                        snap.bid = best_bid
                        snap.ask = best_ask
                        snap.venues = venue_ticks_result
                        snap.timestamp = now

    # ── ATR / Sigma Calculator ────────────────────────────────────────────

    async def _atr_sigma_task(self) -> None:
        """Incrementally compute ATR and sigma from snapshot prices."""
        last_minute = int(time.time() / 60)
        # Collect prices per minute for OHLC calculation
        minute_prices: dict[str, list[float]] = {s: [] for s in self.symbols}

        while self._running and not self._stop.is_set():
            await asyncio.sleep(0.1)  # 100ms
            now = time.time()
            current_minute = int(now / 60)

            # Collect current snapshot prices into minute buckets
            with self._snapshot_lock:
                for symbol in self.symbols:
                    snap = self._snapshots.get(symbol)
                    if snap and snap.price is not None:
                        minute_prices[symbol].append(snap.price)

            # Roll minute bars
            if current_minute != last_minute:
                for symbol in self.symbols:
                    if minute_prices[symbol]:
                        close = minute_prices[symbol][-1]
                        if len(minute_prices[symbol]) >= 2:
                            open_px = minute_prices[symbol][0]
                            high_px = max(minute_prices[symbol])
                            low_px = min(minute_prices[symbol])
                            # True Range = max(high-low, |high-prev_close|, |low-prev_close|)
                            prev_close = self._last_price.get(symbol, close)
                            tr = max(
                                high_px - low_px,
                                abs(high_px - prev_close),
                                abs(low_px - prev_close)
                            )
                            self._atr_state[symbol].append(tr)
                            # Log return for sigma
                            if prev_close > 0:
                                log_ret = math.log(close / prev_close)
                                self._returns_state[symbol].append(log_ret)
                        self._last_price[symbol] = close
                        minute_prices[symbol] = []
                last_minute = current_minute

            # Update snapshots with latest ATR/sigma
            self._update_snapshots()

    def _update_snapshots(self) -> None:
        """Update snapshot ATR/sigma from incremental state."""
        now = time.monotonic()
        with self._snapshot_lock:
            for symbol in self.symbols:
                snap = self._snapshots.get(symbol)
                if snap is None:
                    # Create new snapshot with current price if available
                    continue

                # ATR: mean of true ranges
                if self._atr_state[symbol]:
                    snap.atr = sum(self._atr_state[symbol]) / len(self._atr_state[symbol])

                # Sigma: std of log returns (per-minute)
                if len(self._returns_state[symbol]) >= 2:
                    rets = list(self._returns_state[symbol])
                    mean_ret = sum(rets) / len(rets)
                    var = sum((r - mean_ret) ** 2 for r in rets) / (len(rets) - 1)
                    snap.sigma_per_min = math.sqrt(var) if var > 0 else None

                # Staleness flag
                snap.staleness_flag = any(
                    now - tick.timestamp > WS_SPOT_STALE_SECONDS
                    for tick in snap.venues.values()
                )
                snap.timestamp = now

    # ── Thread-safe Readers ───────────────────────────────────────────────

    def get_snapshot(self, symbol: str) -> Optional[Snapshot]:
        """Return a copy of the latest snapshot for a symbol."""
        symbol = symbol.upper()
        with self._snapshot_lock:
            snap = self._snapshots.get(symbol)
            if snap is None:
                return None
            # Return a copy to avoid external mutation
            return Snapshot(
                symbol=snap.symbol,
                price=snap.price,
                bid=snap.bid,
                ask=snap.ask,
                venues=dict(snap.venues),
                timestamp=snap.timestamp,
                atr=snap.atr,
                sigma_per_min=snap.sigma_per_min,
                staleness_flag=snap.staleness_flag,
            )

    def get_snapshot_with_age(self, symbol: str) -> tuple[Optional[float], Optional[float]]:
        """
        H6-compatible interface: latest price and its age in seconds.
        Returns (price, age) or (None, None).
        """
        snap = self.get_snapshot(symbol)
        if snap is None or snap.price is None:
            return None, None
        age = max(0.0, time.monotonic() - snap.timestamp)
        return snap.price, age

    def get_atr(self, symbol: str) -> Optional[float]:
        """Return current ATR for a symbol."""
        snap = self.get_snapshot(symbol)
        return snap.atr if snap else None

    def get_sigma(self, symbol: str) -> Optional[float]:
        """Return current per-minute sigma for a symbol."""
        snap = self.get_snapshot(symbol)
        return snap.sigma_per_min if snap else None

    def get_venue_state(self, venue: str) -> Optional[VenueState]:
        """Return venue connection state."""
        return self._venue_states.get(venue)

    def is_healthy(self) -> bool:
        """Return True if at least one venue is connected and recent."""
        now = time.monotonic()
        for state in self._venue_states.values():
            if state.connected and now - state.last_message_at < WS_SPOT_STALE_SECONDS * 2:
                return True
        return False


# ── Module-level singleton ─────────────────────────────────────────────────

_tick_bus: Optional[TickBus] = None


def get_tick_bus(symbols: list[str] | None = None) -> TickBus:
    """Get or create the module-level TickBus."""
    global _tick_bus
    if _tick_bus is None:
        _tick_bus = TickBus(symbols)
    return _tick_bus


def start_tick_bus(symbols: list[str] | None = None) -> None:
    """Start the module-level TickBus."""
    get_tick_bus(symbols).start()


def stop_tick_bus() -> None:
    """Stop the module-level TickBus."""
    global _tick_bus
    if _tick_bus is not None:
        _tick_bus.stop()
        _tick_bus = None


# Test mode: when enabled, WebSocket feeds don't connect
_test_mode = False


def set_test_mode(enabled: bool) -> None:
    """Enable/disable test mode (skips real WebSocket connections)."""
    global _test_mode
    _test_mode = enabled


def is_test_mode() -> bool:
    """Check if test mode is enabled."""
    return _test_mode


# ── Compatibility shims for existing code ──────────────────────────────────

def spot_price(symbol: str = "BTC") -> Optional[float]:
    """Compatibility: return latest spot price from tick bus, or None."""
    bus = get_tick_bus()
    snap = bus.get_snapshot(symbol.upper())
    return snap.price if snap else None


def spot_with_age(symbol: str) -> tuple[Optional[float], Optional[float]]:
    """Compatibility: H6 interface - latest WS tick and its age."""
    bus = get_tick_bus()
    return bus.get_snapshot_with_age(symbol.upper())