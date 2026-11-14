"""
binance_feed.py — spot price and volatility for the crypto markets we price.

`api.binance.com` and `fapi.binance.com` return HTTP 451 from this host (geo
restriction). Binance's market-data domains are not blocked, and they serve the
same BTCUSDT series that Polymarket resolves on:

    REST   https://data-api.binance.vision/api/v3
    stream wss://data-stream.binance.vision/ws

That distinction matters beyond access: the spot series is the settlement
reference for Polymarket threshold markets, so using it keeps our fair value
and their resolution on the same price.

Falls back to OKX, then Coinbase, then Kraken if the Binance data domain is
unreachable. Those quote BTC-USD / BTC-USDT, which differ by a few dollars of
USDT basis — acceptable for fair value, not for reconciliation.

`sigma_per_minute` has no fallback — it depends on Binance klines only.
`composite_sigma` adds OKX kline fallback for volatility.

WebSocket streams (Binance `!miniTicker@arr`, Coinbase Advanced Trade `ticker`,
RedStone `price` feed) maintain a live price cache for subsecond spot input.
`spot_price()` prefers the freshest WS price and falls back to REST composite.

Read-only. No keys, no orders.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from typing import AsyncIterator, Dict, List, Optional

import requests
import config

log = logging.getLogger("binance_feed")

_session = requests.Session()

BINANCE_REST = os.environ.get("BINANCE_REST_URL", "https://api.binance.us/api/v3")
BINANCE_WS = os.environ.get("BINANCE_WS_URL", "wss://stream.binance.us:9443/stream")

_KRAKEN_PAIR = {
    "BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD",
    "XRP": "XRPUSD", "DOGE": "XDGUSD", "BNB": "BNBUSD",
    "HYPE": "HYPEUSD", "ZEC": "ZECUSD", "NEAR": "NEARUSD",
}

_KNOWN_BASES = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ZEC", "NEAR"}

# RedStone oracle cache (a third, non-exchange reference price). The legacy
# keyless host api.redstone.finance is DECOMMISSIONED: it CNAMEs to a dead
# CloudFront distribution (d1zxl4a1bpt7kn.cloudfront.net has no A record), and
# modern RedStone serves prices through an authenticated gateway/WebSocket.
# Point REDSTONE_CACHE_URL at a working gateway (e.g. an authenticated
# oracle-gateway URL) to enable it; when unreachable the source is skipped and
# the composite falls back to Binance + Coinbase + OKX + Kraken.
REDSTONE_CACHE_URL = os.environ.get(
    "REDSTONE_CACHE_URL",
    "https://api.redstone.finance/prices/?symbol={base}&provider=redstone&limit=1",
)

# ════════════════════════════════════════════════════════════════════════════
# LIVE WEBSOCKET PRICE CACHE — subsecond spot for momentum gauging
# ════════════════════════════════════════════════════════════════════════════

_WS_STALE_SECONDS = float(os.environ.get("WS_SPOT_STALE_SECONDS", "1.0"))

# {symbol: (price, ts)} — updated by background WS threads; read by spot_price().
_ws_prices: dict[str, tuple[float, float]] = {}
_ws_lock = threading.Lock()
_ws_started = False


def _ws_price(symbol: str) -> Optional[float]:
    """Return the freshest WS price for `symbol`, or None if stale/absent."""
    with _ws_lock:
        entry = _ws_prices.get(symbol.upper())
    if not entry:
        return None
    price, ts = entry
    if time.monotonic() - ts > _WS_STALE_SECONDS:
        return None
    return price


def _ws_set(symbol: str, price: float) -> None:
    now = time.monotonic()
    with _ws_lock:
        _ws_prices[symbol.upper()] = (price, now)
        base = _base_asset(symbol)
        if base and base != symbol.upper():
            _ws_prices[base] = (price, now)


def _binance_ws_loop(symbols: list[str]) -> None:
    """Background thread: Binance.US combined @ticker stream (all symbols, subsecond cadence)."""
    import websockets  # lazy import — optional dep
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Format stream names for Binance.US: btcusd@ticker, ethusd@ticker, etc.
    sym_streams = [
        s.lower() if (s.lower().endswith("usd") or s.lower().endswith("usdt")) else f"{s.lower()}usd"
        for s in symbols
    ]
    streams = "/".join(f"{s}@ticker" for s in sym_streams)
    url = f"{BINANCE_WS}?streams={streams}"
    backoff = 2.0
    while True:
        try:
            loop.run_until_complete(_binance_ws_async(url))
            backoff = 2.0
        except Exception as e:
            sleep_sec = min(backoff * (0.8 + 0.4 * (time.time() % 1.0)), 60.0)
            log.warning("binance WS disconnected: %s — reconnecting in %.1fs", e, sleep_sec)
            time.sleep(sleep_sec)
            backoff = min(backoff * 2.0, 60.0)


async def _binance_ws_async(url: str) -> None:
    import websockets
    async with websockets.connect(url, open_timeout=10, close_timeout=10, ping_interval=20) as ws:
        async for raw in ws:
            try:
                msg = json.loads(raw)
                data = msg.get("data", {})
                sym = data.get("s")
                px = data.get("c")  # close = latest price
                if sym and px is not None:
                    _ws_set(sym, float(px))
            except Exception:
                continue


def _coinbase_ws_loop(symbols: list[str]) -> None:
    """Background thread: Coinbase Advanced Trade ticker channel."""
    import websockets  # lazy import — optional dep
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    url = "wss://advanced-trade-ws.coinbase.com"
    products = [f"{s}-USD" for s in symbols]
    subscribe = {
        "type": "subscribe",
        "channel": "ticker",
        "product_ids": products,
    }
    backoff = 2.0
    while True:
        try:
            loop.run_until_complete(_coinbase_ws_async(url, subscribe))
            backoff = 2.0
        except Exception as e:
            sleep_sec = min(backoff * (0.8 + 0.4 * (time.time() % 1.0)), 60.0)
            log.warning("coinbase WS disconnected: %s — reconnecting in %.1fs", e, sleep_sec)
            time.sleep(sleep_sec)
            backoff = min(backoff * 2.0, 60.0)


async def _coinbase_ws_async(url: str, subscribe: dict) -> None:
    import websockets
    async with websockets.connect(url, open_timeout=10, close_timeout=10, ping_interval=20) as ws:
        await ws.send(json.dumps(subscribe))
        async for raw in ws:
            try:
                msg = json.loads(raw)
                if msg.get("channel") == "ticker":
                    for ev in msg.get("events", []):
                        for t in ev.get("tickers", []):
                            sym = t.get("product_id", "").replace("-USD", "")
                            px = t.get("price")
                            if sym and px is not None:
                                _ws_set(sym, float(px))
            except Exception:
                continue


def _redstone_ws_loop(symbols: list[str]) -> None:
    """Background thread: RedStone price feed WebSocket (50 topic limit/conn).

    Skipped entirely if the gateway is unreachable — REST composite_price
    covers spot input. Non-fatal.
    """
    import websockets  # lazy import — optional dep

    url = os.environ.get("REDSTONE_WS_URL", "wss://gateway.redstone.finance/ws")
    api_key = os.environ.get("REDSTONE_API_KEY", "")
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    # RedStone feedIds are asset symbols like BTC, ETH — use those directly.
    items = [{"feedId": s, "type": "price", "dataServiceId": "redstone-primary-prod"}
             for s in symbols[:50]]
    subscribe = {"op": "subscribe", "items": items}
    tried_once = False
    while True:
        try:
            asyncio.run(_redstone_ws_async(url, headers, subscribe))
            tried_once = True
        except Exception as e:
            # Persistent DNS/connection failure means the gateway is wrong for
            # this host — stop retrying and fall back to REST (non-fatal).
            if tried_once and ("404" in str(e) or "Name or service not known"
                               in str(e) or "Failed to resolve" in str(e)):
                log.warning(
                    "redstone WS gateway unreachable (%s) — falling back to "
                    "REST composite_price for spot input", e)
                return
            log.warning("redstone WS disconnected: %s — reconnecting in 2s", e)
            time.sleep(2)
            tried_once = True


async def _redstone_ws_async(url: str, headers: dict, subscribe: dict) -> None:
    import websockets
    connect_kwargs = dict(open_timeout=10, close_timeout=10, ping_interval=20)
    # websockets >= 13 uses additional_headers; older versions use extra_headers.
    if headers:
        connect_kwargs["additional_headers"] = headers
    async with websockets.connect(url, **connect_kwargs) as ws:
        await ws.send(json.dumps(subscribe))
        async for raw in ws:
            try:
                msg = json.loads(raw)
                if msg.get("type") != "price":
                    continue
                sym = msg.get("dataPackageId")
                px = msg.get("value")
                if sym and px is not None:
                    _ws_set(sym, float(px))
            except Exception:
                continue


def start_ws_streams(symbols: list[str] | None = None) -> None:
    """Start background WebSocket price streams for the given symbols.

    Safe to call multiple times — streams start only once. If `websockets` is
    not installed the streams are silently skipped (REST fallback still works).
    """
    global _ws_started
    if _ws_started:
        return
    symbols = symbols or list(_KNOWN_BASES)
    try:
        import websockets  # noqa: F401  — availability check
    except ImportError:
        log.warning("websockets not installed — WS price streams disabled; "
                    "using REST composite_price only")
        _ws_started = True
        return
    _ws_started = True
    feeds = [("binance", _binance_ws_loop),
             ("coinbase", _coinbase_ws_loop)]
    if getattr(config, "REDSTONE_ENABLED", False):
        feeds.append(("redstone", _redstone_ws_loop))
    for name, fn in feeds:
        t = threading.Thread(target=fn, args=(symbols,),
                             daemon=True, name=f"ws-{name}")
        t.start()
        log.info("WS price stream started: %s", name)


def spot_price(symbol: str = "BTCUSDT") -> Optional[float]:
    """Spot price for `symbol`: freshest WS price if available, else REST composite.

    WS prices are subsecond; REST composite is ~1-2s. For a 15s trading cycle
    either is fine, but WS gives us the tick-level momentum the gate needs.
    Falls back through: WS → REST composite → None.
    """
    base = _base_asset(symbol)
    if base:
        px = _ws_price(base)
        if px is not None:
            return px
    return composite_price(symbol)


def _base_asset(symbol: str) -> Optional[str]:
    """Strip USDT/USDC/USD/-USD suffix and validate against known bases."""
    base = symbol.upper()
    for suffix in ("USDT", "USDC", "-USD", "USD"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    return base if base in _KNOWN_BASES else None


def _fallback_sources(base: str):
    """Fallback quote URLs for one asset, e.g. base='ETH'.

    Kraken is skipped for assets it does not quote (only BTC/ETH).
    """
    pair = _KRAKEN_PAIR.get(base)
    kraken = ()
    if pair:
        kraken = (
            ("kraken", f"https://api.kraken.com/0/public/Ticker?pair={pair}",
             lambda d: float(next(iter(d["result"].values()))["c"][0])),
        )
    return (
        ("okx", f"https://www.okx.com/api/v5/market/ticker?instId={base}-USDT",
         lambda d: float(d["data"][0]["last"])),
        ("gateio", f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={base}_USDT",
         lambda d: float((d[0] if isinstance(d, list) else d)["last"])),
        ("coinbase", f"https://api.exchange.coinbase.com/products/{base}-USD/ticker",
         lambda d: float(d["price"])),
        # RedStone oracle cache — non-exchange reference. Skipped when unreachable.
        ("redstone", REDSTONE_CACHE_URL.format(base=base),
         lambda d: float((d[0] if isinstance(d, list) else d).get("value", (d[0] if isinstance(d, list) else d).get("price", 0)))),
        *kraken,
    )


def _remaining(timeout: int, deadline: float) -> int:
    """Seconds left before deadline, floored to 1 (never 0 or negative)."""
    return max(1, int(deadline - time.monotonic()))


def get_price(symbol: str = "BTCUSDT", timeout: int = 10) -> Optional[float]:
    """Latest Binance spot price, or None if unavailable. Use composite_price()
    when you need to be robust to a single exchange's basis."""
    deadline = time.monotonic() + timeout
    try:
        resp = _session.get(f"{BINANCE_REST}/ticker/price",
                            params={"symbol": symbol},
                            timeout=_remaining(timeout, deadline))
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as e:
        log.warning("binance ticker unavailable (%s), trying fallbacks", e)

    base = _base_asset(symbol)
    if not base:
        log.error("unknown base asset in symbol %s", symbol)
        return None
    for name, url, pick in _fallback_sources(base):
        try:
            resp = _session.get(url, timeout=_remaining(timeout, deadline))
            resp.raise_for_status()
            px = pick(resp.json())
            log.warning("using %s fallback price %.2f", name, px)
            return px
        except Exception:
            continue
    log.error("no spot price available from any source")
    return None


def price_sources(symbol: str = "BTCUSDT", timeout: int = 10) -> Dict[str, float]:
    """Spot price from every reachable venue, keyed by venue name.

    Exchanges disagree by real money: on 2026-11-05 Binance quoted BTC at
    76,239 while Coinbase quoted 76,040. That basis is larger than the
    cross-venue edge we are trying to measure, so a single source is not good
    enough to price against.
    """
    out: Dict[str, float] = {}
    deadline = time.monotonic() + timeout
    try:
        bsym = symbol.upper()
        if not (bsym.endswith("USD") or bsym.endswith("USDT")):
            bsym = f"{bsym}USD"
        resp = _session.get(f"{BINANCE_REST}/ticker/price",
                            params={"symbol": bsym}, timeout=_remaining(timeout, deadline))
        resp.raise_for_status()
        out["binance"] = float(resp.json()["price"])
    except Exception as e:
        log.warning("binance ticker unavailable: %s", e)
    base = _base_asset(symbol)
    if base:
        for name, url, pick in _fallback_sources(base):
            try:
                resp = _session.get(url, timeout=_remaining(timeout, deadline))
                resp.raise_for_status()
                out[name] = pick(resp.json())
            except Exception:
                continue
    return out


def composite_price(symbol: str = "BTCUSDT", timeout: int = 10) -> Optional[float]:
    """Median across venues — a proxy for a composite index like CF BRTI.

    A median is chosen over a mean so one stale or dislocated venue cannot drag
    the reference. This is a proxy: BRTI is a volume-weighted composite, and we
    cannot see its weights, so a persistent basis against BRTI remains.
    """
    sources = price_sources(symbol, timeout)
    prices = sorted(p for p in sources.values() if math.isfinite(p))
    if not prices:
        log.warning("composite_price: no valid prices from any source")
        return None
    if len(prices) == 1:
        log.warning("composite_price: only one venue available (%s)", list(sources))
    mid = len(prices) // 2
    if len(prices) % 2:
        return prices[mid]
    return (prices[mid - 1] + prices[mid]) / 2.0


def composite_sigma(symbol: str = "BTCUSDT", lookback: int = 60,
                    timeout: int = 10) -> Optional[float]:
    """Per-minute volatility with fallback venue.

    Tries Binance first (most liquid), then OKX klines.
    This provides redundancy if Binance is unreachable.
    """
    # Try Binance first
    sigma = sigma_per_minute(symbol, lookback)
    if sigma is not None:
        return sigma
    # Fallback: OKX klines
    base = _base_asset(symbol)
    if base:
        okx_symbol = f"{base}-USDT"
        try:
            resp = _session.get("https://www.okx.com/api/v5/market/history-candles",
                                params={"instId": okx_symbol, "bar": "1m", "limit": lookback + 1},
                                timeout=timeout)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if data:
                # OKX returns [ts, o, h, l, c, ...]; sort ascending by timestamp and pick close (index 4)
                sorted_rows = sorted(data, key=lambda r: float(r[0]))
                closes = [float(row[4]) for row in sorted_rows]
                rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
                if len(rets) >= 5:
                    mean = sum(rets) / len(rets)
                    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
                    log.warning("using OKX fallback sigma: %.6f", math.sqrt(var))
                    return math.sqrt(var)
        except Exception as e:
            log.warning("OKX sigma fallback failed: %s", e)
    log.warning("composite_sigma: no valid sigma from any source")
    return None


def get_klines(symbol: str = "BTCUSDT", interval: str = "1m",
               limit: int = 60, timeout: int = 15) -> Optional[List[List]]:
    """Raw Binance klines, oldest first. Each row: [open_time, o, h, l, c, v, ...]."""
    try:
        resp = requests.get(f"{BINANCE_REST}/klines",
                            params={"symbol": symbol, "interval": interval, "limit": limit},
                            timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning("binance klines failed: %s", e)
        return None


def _fetch_klines_paginated(
    url: str,
    params_builder,
    start_ms: int,
    end_ms: int,
    timeout: int,
    limit_per_req: int = 1000,
    reverse_results: bool = False,
    forward: bool = False,
) -> Optional[List[List]]:
    """Generic pagination helper for fetching klines from any venue.
    
    Args:
        url: API endpoint URL
        params_builder: function(cursor, start_ms, end_ms) -> dict of params
        start_ms: window start time (ms)
        end_ms: window end time (ms)
        timeout: request timeout
        limit_per_req: max results per request
        reverse_results: if True, each batch is newest-first and will be reversed
        forward: walk from start_ms towards end_ms. Binance needs this: it
            treats the cursor as ``startTime`` grams, so starting at end_ms
            returns only the candle at the window's far edge.
    """
    all_klines = []
    cursor = start_ms if forward else end_ms
    try:
        while (cursor < end_ms) if forward else (cursor > start_ms):
            previous = cursor
            params = params_builder(cursor, start_ms, end_ms)
            resp = _session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            if reverse_results:
                data.reverse()
                all_klines = data + all_klines  # prepend
            else:
                all_klines.extend(data)  # append
            # Advance cursor based on pagination direction
            if reverse_results:
                # OKX returns newest first; we reversed to oldest-first.
                # data[0] is the OLDEST in this batch. Next page should fetch
                # klines BEFORE this oldest timestamp (OKX uses 'before' param).
                cursor = int(data[0][0]) - 1  # OKX: before oldest
            else:
                # Binance uses 'startTime' (after cursor), data[-1] is newest
                cursor = int(data[-1][0]) + 1  # Binance: after newest
            if len(data) < limit_per_req:
                break
            if cursor <= previous:
                # A page that does not advance can only spin forever.
                log.warning("klines pagination stalled at %s", cursor)
                break
        return all_klines
    except Exception as e:
        log.warning("klines paginated fetch failed: %s", e)
        return None


def get_klines_range(symbol: str, start_ms: int, end_ms: int,
                     interval: str = "1m", timeout: int = 20) -> Optional[List[List]]:
    """Klines for a historical window, for backtests. Binance caps at 1000/response.
    Uses pagination to fetch all klines in the window."""
    def _params(cursor, start_ms, end_ms):
        return {"symbol": symbol, "interval": interval,
                "startTime": cursor, "endTime": end_ms, "limit": 1000}
    return _fetch_klines_paginated(
        f"{BINANCE_REST}/klines", _params, start_ms, end_ms, timeout, 1000, False,
        forward=True,
    )


def _get_okx_klines_range(symbol: str, start_ms: int, end_ms: int,
                          interval: str = "1m", timeout: int = 20) -> Optional[List[List]]:
    """Fetch klines from OKX as fallback.
    
    OKX API: GET /api/v5/market/history-candles
    Params: instId, bar, before, after, limit
    Returns: [[ts, o, h, l, c, vol, ...], ...] oldest first
    """
    base = _base_asset(symbol)
    if not base:
        return None
    okx_symbol = f"{base}-USDT"
    
    # Convert interval to OKX bar format
    bar_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1H": "1H", "4H": "4H", "1D": "1D"}
    okx_bar = bar_map.get(interval, "1m")
    
    def _params(cursor, start_ms, end_ms):
        return {
            "instId": okx_symbol,
            "bar": okx_bar,
            "before": str(cursor),
            "limit": 100
        }
    
    klines = _fetch_klines_paginated(
        "https://www.okx.com/api/v5/market/history-candles",
        _params, start_ms, end_ms, timeout, 100, True
    )
    if klines is None:
        return None
    # Convert OKX format [ts, o, h, l, c, vol, ...] to Binance format [open_time, o, h, l, c, v, ...]
    converted = []
    for k in klines:
        converted.append([
            int(k[0]),      # open_time (ms)
            float(k[1]),    # open
            float(k[2]),    # high
            float(k[3]),    # low
            float(k[4]),    # close
            float(k[5]),    # volume
            # Add remaining fields as 0/empty to match Binance format
            0, 0, 0, 0, 0, 0
        ])
    return converted


def composite_klines_range(symbol: str, start_ms: int, end_ms: int,
                           interval: str = "1m", timeout: int = 20) -> Optional[List[List]]:
    """Klines with fallback: tries Binance first, then OKX.
    
    Returns klines in Binance format (oldest first).
    """
    # Try Binance first
    klines = get_klines_range(symbol, start_ms, end_ms, interval, timeout)
    if klines is not None:
        return klines
    # Fallback: OKX klines
    log.warning("Binance klines failed, trying OKX fallback for %s", symbol)
    return _get_okx_klines_range(symbol, start_ms, end_ms, interval, timeout)


def sigma_per_minute(symbol: str = "BTCUSDT", lookback: int = 60) -> Optional[float]:
    """Std dev of 1-minute log returns over the lookback window.

    Returned per minute so a caller can scale by sqrt(minutes remaining). Uses
    close-to-close returns; that understates intra-minute moves, which is the
    conservative direction for a threshold market near expiry.
    """
    klines = get_klines(symbol, "1m", lookback + 1)
    if not klines or len(klines) < 10:
        return None
    closes = [float(k[4]) for k in klines]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def prob_above(spot: float, strike: float, sigma_min: float, minutes_left: float) -> float:
    """P(price > strike) for a zero-drift lognormal walk.

    This is the spot-implied fair value we compare both venues against. Near
    expiry the lognormal is a poor description of a single print, so treat the
    result as a reference, not a truth.
    """
    if spot <= 0 or strike <= 0 or sigma_min <= 0 or minutes_left <= 0:
        return float("nan")
    denom = sigma_min * math.sqrt(minutes_left)
    if denom <= 0:
        return 1.0 if spot > strike else 0.0
    d2 = (math.log(spot / strike) - 0.5 * denom * denom) / denom
    return 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))


async def stream_trades(symbol: str = "btcusdt") -> AsyncIterator[float]:
    """Live trade prices from the Binance market-data stream.

    Provided for streaming callers. The trading loop decides once per cycle, so
    `get_price` is the correct path there — a websocket would add a reconnect
    surface for a number that changes nothing within a 60-second cycle.

    This function reconnects automatically on disconnect with exponential backoff.
    """
    import websockets

    url = f"{BINANCE_WS}/{symbol.lower()}@trade"
    backoff = 1
    max_backoff = 60
    while True:
        try:
            async with websockets.connect(url, open_timeout=10, close_timeout=10, ping_interval=20) as ws:
                backoff = 1  # Reset on successful connection
                async for raw in ws:
                    try:
                        yield float(json.loads(raw)["p"])
                    except (ValueError, KeyError, TypeError):
                        continue
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("stream_trades disconnected: %s — reconnecting in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    px = get_price()
    sig = sigma_per_minute()
    print(f"BTCUSDT spot: {px}")
    print(f"sigma per minute: {sig}")
    if px and sig:
        for mins in (60, 15):
            print(f"  60m threshold at spot+0.2% -> P(above) = "
                  f"{prob_above(px, px * 1.002, sig, mins):.3f}")
