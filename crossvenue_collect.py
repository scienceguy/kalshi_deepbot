"""
crossvenue_collect.py — snapshot Kalshi/Polymarket ladders against spot over time.

One snapshot cannot tell you which venue is stale, because "stale" is a claim
about time: a price is stale if spot has moved and the venue has not. So this
appends a row per rung per snapshot and the analysis question becomes whether
deviations persist and which venue leads.

Writes JSONL under data/crossvenue/YYYYMMDD.jsonl. No orders, no writes to any
venue.

    python crossvenue_collect.py --once
    python crossvenue_collect.py --interval 60
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import binance_feed
import crossvenue as cv

log = logging.getLogger("crossvenue_collect")

OUT_DIR = Path(__file__).resolve().parent / "data" / "crossvenue"


def snapshot() -> list[dict]:
    """One row per (pair, strike) with both venues' prices and spot fair value."""
    spots = {}
    for asset in ("BTC", "ETH"):
        symbol = f"{asset}USDT"
        px = binance_feed.composite_price(symbol)
        sigma = binance_feed.sigma_per_minute(symbol)
        if px is None or sigma is None:
            log.warning("no spot/vol for %s, skipping", symbol)
            continue
        spots[asset] = (px, sigma, binance_feed.price_sources(symbol))

    now = datetime.now(timezone.utc)
    rows = []
    for event in cv.discover_kalshi_events():
        parsed = cv.parse_kalshi_event(event)
        if not parsed:
            continue
        asset, expiry = parsed
        if asset not in spots:
            continue
        spot, sigma, sources = spots[asset]
        kalshi = cv.fetch_kalshi_ladder(event)
        if not kalshi or not kalshi.ok:
            continue
        for slug in cv.candidate_pm_slugs(asset, expiry):
            poly = cv.fetch_pm_ladder(slug)
            if not poly or not poly.ok:
                continue
            for r in cv.three_way(kalshi, poly, spot, sigma, now=now):
                rows.append({
                    "ts": now.isoformat(),
                    "asset": asset,
                    "event": event,
                    "slug": slug,
                    "expiry": kalshi.expiry_et.isoformat(),
                    "minutes_left": round(r["minutes_left"], 2),
                    "strike": r["strike"],
                    "spot": round(spot, 2),
                    "sigma_min": round(sigma, 8),
                    "venue_dispersion": round(max(sources.values()) - min(sources.values()), 2),
                    "fair": round(r["fair"], 4),
                    "kalshi": round(r["kalshi"], 4),
                    "poly": round(r["poly"], 4),
                    "kalshi_edge_c": round(r["kalshi_edge_c"], 2),
                    "poly_edge_c": round(r["poly_edge_c"], 2),
                })
    return rows


def append(rows: list[dict]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    with path.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="take one snapshot and exit")
    ap.add_argument("--interval", type=int, default=60, help="seconds between snapshots")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while not stop["flag"]:
        try:
            rows = snapshot()
            if rows:
                path = append(rows)
                log.info("wrote %d rows to %s", len(rows), path.name)
            else:
                log.warning("no paired ladders this cycle")
        except Exception as e:
            log.error("snapshot failed: %s", e)
        if args.once:
            break
        for _ in range(args.interval):
            if stop["flag"]:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
