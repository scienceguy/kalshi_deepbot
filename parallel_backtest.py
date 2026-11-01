#!/usr/bin/env python3
"""
parallel_backtest.py — Parallel parameter sweep using the public Kalshi API.

Fetches settled (finalized) markets from the public Kalshi markets endpoint
(does not require authentication), then simulates the signal engine against
each market across a parameter grid using the shared backtest_engine.

Settlement outcome for Kalshi 15-min binary markets:
  YES wins if expiration_value >= floor_strike (strike)
  NO wins otherwise

USAGE:
    python parallel_backtest.py          # full sweep
    python parallel_backtest.py --quick  # smaller grid for speed
    python parallel_backtest.py --limit N  # max markets per series
    python parallel_backtest.py --jobs N   # parallel workers (default: 4)
"""
import argparse
import logging
import sys
import time

import requests
import config
from backtest_engine import run_sweep

log = logging.getLogger("parallel_backtest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


def settlement_yes_won(market):
    """YES wins if expiration_value >= floor_strike."""
    exp_val = market.get("expiration_value")
    floor_strike = market.get("floor_strike")
    if exp_val is None or exp_val == "" or floor_strike is None or floor_strike == "":
        return None
    try:
        return float(exp_val) >= float(floor_strike)
    except (ValueError, TypeError):
        return None


def fetch_settled_markets_public(series_list, limit_per_series=1000):
    """Fetch settled markets from public Kalshi endpoint (no auth required).

    Follows the cursor returned by the API to page through all results,
    so markets beyond the first page are not silently excluded.
    """
    all_markets = []
    for series in series_list:
        url = f"{BASE_URL}/markets"
        cursor = ""
        fetched = 0
        while fetched < limit_per_series:
            params = {
                "series_ticker": series,
                "limit": min(100, limit_per_series - fetched),
            }
            if cursor:
                params["cursor"] = cursor
            try:
                r = requests.get(url, params=params, timeout=60)
                r.raise_for_status()
                data = r.json()
                markets = data.get("markets", [])
                if not markets:
                    break
                settled = []
                for m in markets:
                    if m.get("status") == "finalized" and m.get("expiration_value"):
                        m["_series"] = series
                        settled.append(m)
                all_markets.extend(settled)
                fetched += len(markets)
                cursor = data.get("cursor", "")
                if not cursor:
                    break
                log.info("%s: %d fetched so far...", series, fetched)
            except Exception as e:
                log.warning("%s fetch failed: %s", series, e)
                break
        settled_count = sum(
            1 for m in all_markets if m.get("_series") == series
        )
        log.info("%s: %d settled markets (out of %d fetched)",
                 series, settled_count, fetched)
        time.sleep(0.2)
    return all_markets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="Smaller parameter grid")
    parser.add_argument("--limit", type=int, default=1000, help="Max markets per series")
    parser.add_argument("--top", type=int, default=10, help="Show top N results")
    parser.add_argument("--jobs", type=int, default=4, help="Parallel workers")
    args = parser.parse_args()

    logging.getLogger().setLevel(logging.WARNING)

    series = config.TARGET_SERIES
    log.info("Fetching settled markets for %s...", series)
    markets = fetch_settled_markets_public(series, args.limit)
    log.info("Total settled markets: %d", len(markets))

    if not markets:
        log.error("No settled markets fetched — cannot backtest")
        sys.exit(1)

    run_sweep(
        markets,
        settlement_yes_won=settlement_yes_won,
        quick=args.quick,
        limit=None,
        top=args.top,
        jobs=args.jobs,
        out_path="parallel_backtest_results.json",
        starting_balance=10.0,
    )


if __name__ == "__main__":
    main()
