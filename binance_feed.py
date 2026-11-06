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

Read-only. No keys, no orders.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import AsyncIterator, List, Optional

import requests

log = logging.getLogger("binance_feed")

BINANCE_REST = "https://data-api.binance.vision/api/v3"
BINANCE_WS = "wss://data-stream.binance.vision/ws"

_KRAKEN_PAIR = {"BTC": "XBTUSD", "ETH": "ETHUSD"}


def _fallback_sources(base: str):
    """Fallback quote URLs for one asset, e.g. base='ETH'."""
    return (
        ("okx", f"https://www.okx.com/api/v5/market/ticker?instId={base}-USDT",
         lambda d: float(d["data"][0]["last"])),
        ("coinbase", f"https://api.exchange.coinbase.com/products/{base}-USD/ticker",
         lambda d: float(d["price"])),
        ("kraken", f"https://api.kraken.com/0/public/Ticker?pair={_KRAKEN_PAIR[base]}",
         lambda d: float(next(iter(d["result"].values()))["c"][0])),
    )


def get_price(symbol: str = "BTCUSDT", timeout: int = 10) -> Optional[float]:
    """Latest Binance spot price, or None if unavailable. Use composite_price()
    when you need to be robust to a single exchange's basis."""
    try:
        resp = requests.get(f"{BINANCE_REST}/ticker/price",
                            params={"symbol": symbol}, timeout=timeout)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as e:
        log.warning("binance ticker unavailable (%s), trying fallbacks", e)

    for name, url, pick in _fallback_sources(symbol.replace("USDT", "")):
        try:
            resp = requests.get(url, timeout=timeout)
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
    try:
        resp = requests.get(f"{BINANCE_REST}/ticker/price",
                            params={"symbol": symbol}, timeout=timeout)
        resp.raise_for_status()
        out["binance"] = float(resp.json()["price"])
    except Exception as e:
        log.warning("binance ticker unavailable: %s", e)
    for name, url, pick in _fallback_sources(symbol.replace("USDT", "")):
        try:
            resp = requests.get(url, timeout=timeout)
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
    prices = sorted(price_sources(symbol, timeout).values())
    if not prices:
        return None
    mid = len(prices) // 2
    if len(prices) % 2:
        return prices[mid]
    return (prices[mid - 1] + prices[mid]) / 2.0


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


def get_klines_range(symbol: str, start_ms: int, end_ms: int,
                     interval: str = "1m", timeout: int = 20) -> Optional[List[List]]:
    """Klines for a historical window, for backtests. Binance caps at 1000/response."""
    try:
        resp = requests.get(f"{BINANCE_REST}/klines",
                            params={"symbol": symbol, "interval": interval,
                                    "startTime": start_ms, "endTime": end_ms, "limit": 1000},
                            timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning("binance klines range failed: %s", e)
        return None


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
    """
    import websockets

    url = f"{BINANCE_WS}/{symbol.lower()}@trade"
    async with websockets.connect(url, open_timeout=10, ping_interval=20) as ws:
        async for raw in ws:
            try:
                import json
                yield float(json.loads(raw)["p"])
            except (ValueError, KeyError, TypeError):
                continue


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
