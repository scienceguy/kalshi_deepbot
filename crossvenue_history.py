"""
crossvenue_history.py — measure the Kalshi reference basis from settled markets.

The planner refuses to trade until it knows the offset between Kalshi's
settlement reference (CF Benchmarks BRTI, a 60-second average) and the composite
spot we price from. That offset cannot be estimated from one snapshot: a single
reading is indistinguishable from a genuine mispricing. It can be estimated from
history, and Kalshi gives the history away:

    /historical/markets                       settlement result + expiration_value
    /series/{s}/markets/{t}/candlesticks      per-minute yes_bid / yes_ask OHLC
    /historical/trades                        trade tape with taker side

For each settled event we rebuild the ladder at each minute, invert it to the
spot price Kalshi was centred on, and compare that to the exchange composite at
the same minute. The distribution of that difference is the basis.

Read-only. Places nothing.

    python crossvenue_history.py --series KXBTCD --events 6
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

import binance_feed
import ladder_strategy as ls
from crossvenue import KALSHI_BASE

log = logging.getLogger("crossvenue_history")

SERIES_ASSET = {
    "KXBTCD": "BTC", "KXETHD": "ETH",
    "KXBTC": "BTC", "KXETH": "ETH",
    "KXBTC15M": "BTC", "KXETH15M": "ETH",
    "KXBTC15M-D": "BTC", "KXETH15M-D": "ETH",
    "KXSOL15M": "SOL", "KXXRP15M": "XRP", "KXDOGE15M": "DOGE",
    "KXBNB15M": "BNB", "KXHYPE15M": "HYPE", "KXZEC15M": "ZEC",
    "KXNEAR15M": "NEAR",
}
REQUEST_GAP = 0.12  # stay well inside Kalshi's public rate limit

_last = 0.0


def _get(path: str, params: dict, timeout: int = 25) -> Optional[dict]:
    global _last
    gap = time.time() - _last
    if gap < REQUEST_GAP:
        time.sleep(REQUEST_GAP - gap)
    try:
        r = requests.get(f"{KALSHI_BASE}{path}", params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("%s failed: %s", path, e)
        return None
    finally:
        _last = time.time()


def settled_events(series: str, limit: int = 200) -> List[dict]:
    """Settled events for a series, newest first, with their settlement value."""
    events: Dict[str, dict] = {}
    cursor = None
    prev_cursor = None
    pages = 0
    while pages < 12 and len(events) < limit:
        params = {"series_ticker": series, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        payload = _get("/historical/markets", params)
        if not payload:
            break
        for m in payload.get("markets", []):
            ev = m.get("event_ticker")
            if not ev or m.get("expiration_value") in (None, ""):
                continue
            if not m.get("close_time"):
                continue
            try:
                ev_val = float(m["expiration_value"])
            except (TypeError, ValueError):
                continue
            if not (100 < ev_val < 1_000_000):
                continue
            if ev not in events:
                events[ev] = {
                    "event": ev,
                    "close_time": m.get("close_time"),
                    "expiration_value": ev_val,
                    "strikes": {},
                }
            try:
                events[ev]["strikes"][float(m["floor_strike"])] = m["ticker"]
            except (TypeError, ValueError):
                continue
        prev_cursor = cursor
        cursor = payload.get("cursor")
        pages += 1
        # Detect stuck cursor (same cursor returned repeatedly)
        if cursor and cursor == prev_cursor:
            log.warning("Pagination cursor stuck for series %s after %d pages -- breaking", series, pages)
            break
        if not cursor:
            break
    return list(events.values())


def candles(ticker: str, series: str, start: int, end: int) -> List[dict]:
    payload = _get(f"/historical/markets/{ticker}/candlesticks",
                   {"start_ts": start, "end_ts": end, "period_interval": 1})
    return (payload or {}).get("candlesticks", []) if payload else []


def _ohlc(d) -> Optional[float]:
    """Read close from a nested BidAskDistribution dict."""
    if not isinstance(d, dict):
        return None
    v = d.get("close")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _f(d) -> Optional[float]:
    return _ohlc(d)


def spot_closes(symbol: str, start: int, end: int) -> Dict[int, float]:
    rows = binance_feed.get_klines_range(symbol, start * 1000, end * 1000, "1m")
    out = {}
    for r in rows or []:
        close_ms = int(r[0]) + 60000     # align to close time, not open
        ts = close_ms // 1000
        out[ts - ts % 60] = float(r[4])
    return out


def sigma_curve(symbol: str, start_ts: int, end_ts: int,
                window: int = 60) -> Dict[int, Optional[float]]:
    """Precompute per-minute sigma for the whole window (one API call)."""
    rows = binance_feed.get_klines_range(symbol, start_ts * 1000,
                                         end_ts * 1000, "1m")
    out: Dict[int, Optional[float]] = {}
    if not rows:
        return out
    closes = [(int(r[0]) // 1000, float(r[4])) for r in rows]
    for i, (ts, _) in enumerate(closes):
        if i < window:
            out[ts - ts % 60] = None
            continue
        window_closes = [c for _, c in closes[i - window:i + 1]]
        rets = [math.log(b / a) for a, b in zip(window_closes, window_closes[1:])
                if a > 0 and b > 0]
        if len(rets) < 5:
            out[ts - ts % 60] = None
            continue
        mean = sum(rets) / len(rets)
        out[ts - ts % 60] = (
            sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        ) ** 0.5
    return out


def sigma_at(minute_ts: int, symbol: str, window: int = 60) -> Optional[float]:
    # minute_ts is the end of the minute (e.g., 14:30:00).
    # We want klines from (minute_ts - window*60) to minute_ts inclusive.
    # Binance klines use open_time, so we need end_ms = (minute_ts + 60) * 1000
    # to include the kline that closes at minute_ts.
    rows = binance_feed.get_klines_range(symbol, (minute_ts - window * 60) * 1000,
                                         (minute_ts + 60) * 1000, "1m")
    if not rows or len(rows) < 10:
        return None
    closes = [float(r[4]) for r in rows]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    return (sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5


def event_basis(event: dict, series: str, asset: str, minutes: int = 60) -> List[dict]:
    """Invert the ladder at each minute and compare to the exchange composite."""
    if not event.get("close_time"):
        return []
    close = datetime.fromisoformat(event["close_time"].replace("Z", "+00:00"))
    close_ts = int(close.timestamp())
    start_ts = close_ts - minutes * 60
    settle = event["expiration_value"]
    if settle <= 0:
        return []
    symbol = f"{asset}USDT"

    # Only strikes near the settlement value carry information; the wings sit at
    # 0/1 and cannot be inverted.
    near = {s: t for s, t in event["strikes"].items() if abs(s - settle) / settle < 0.02}
    if len(near) < 4:
        return []

    ladders: Dict[int, List[dict]] = {}
    for strike, ticker in sorted(near.items()):
        for c in candles(ticker, series, start_ts, close_ts):
            ts = int(c["end_period_ts"])
            bid, ask = _ohlc(c.get("yes_bid")), _ohlc(c.get("yes_ask"))
            if bid is None or ask is None or ask < bid:
                continue
            if ask - bid > 0.08:
                continue
            mid = (bid + ask) / 2.0
            if not 0.0 < mid < 1.0:
                continue
            ladders.setdefault(ts, []).append({"strike": strike, "kalshi": mid})

    spots = spot_closes(symbol, start_ts, close_ts)
    sigmas = sigma_curve(symbol, start_ts, close_ts)
    out = []
    for ts, rungs in sorted(ladders.items()):
        if len(rungs) < 4:
            continue
        # Use spot/sigma from previous minute to avoid lookahead bias:
        # Kalshi candle at `ts` (end_period_ts) covers [ts-60, ts].
        # The spot at `ts` is not known until after the minute ends.
        spot = spots.get(ts - 60)
        if spot is None:
            continue
        mins_left = (close_ts - ts) / 60.0
        if mins_left <= 0:
            continue
        sigma = sigmas.get(ts - 60)
        if not sigma:
            continue
        implied = ls.implied_spot(rungs, sigma, mins_left)
        if implied is None:
            continue
        sigma_dollars = spot * sigma * (mins_left ** 0.5)
        out.append({
            "ts": ts, "event": event["event"], "strike_count": len(rungs),
            "minutes_left": round(mins_left, 1),
            "spot": round(spot, 2), "implied": round(implied, 2),
            "basis": round(implied - spot, 2),
            "sigma_dollars": round(sigma_dollars, 2),
            "basis_over_sigma": round((implied - spot) / sigma_dollars, 3) if sigma_dollars else None,
            "settle": settle,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXBTCD")
    ap.add_argument("--events", type=int, default=6, help="settled events to sample")
    ap.add_argument("--minutes", type=int, default=60, help="window before close")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    asset = SERIES_ASSET.get(args.series)
    if not asset:
        print(f"unknown series {args.series}")
        return 2

    events = settled_events(args.series, limit=args.events * 4)[: args.events]
    print(f"{args.series}: {len(events)} settled events sampled, asset {asset}")

    rows: List[dict] = []
    for ev in events:
        got = event_basis(ev, args.series, asset, args.minutes)
        if got:
            basis = [r["basis"] for r in got]
            print(f"  {ev['event']}  settle {ev['expiration_value']:,.2f}  "
                  f"{len(got)} obs  median basis {statistics.median(basis):+,.2f}")
            rows.extend(got)
        else:
            print(f"  {ev['event']}  no usable observations")

    if not rows:
        print("no data — nothing measured")
        return 1

    basis = [r["basis"] for r in rows]
    over_sigma = [r["basis_over_sigma"] for r in rows if r["basis_over_sigma"] is not None]
    print(f"\n=== {args.series} basis over {len(rows)} observations ===")
    print(f"median  {statistics.median(basis):+,.2f}")
    print(f"mean    {statistics.fmean(basis):+,.2f}")
    print(f"stdev   {statistics.stdev(basis):,.2f}" if len(basis) > 1 else "")
    print(f"p10/p90 {sorted(basis)[len(basis)//10]:+,.2f} / "
          f"{sorted(basis)[9*len(basis)//10]:+,.2f}")
    if over_sigma:
        print(f"basis as fraction of one sigma: median "
              f"{statistics.median(over_sigma):+.3f}, stdev {statistics.pstdev(over_sigma):.3f}")
    print(f"\nplanner gate:  --basis-dollars {statistics.median(basis):.2f}")

    if args.out:
        with open(args.out, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        print(f"wrote {len(rows)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
