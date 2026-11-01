#!/usr/bin/env python3
"""
backtest.py — Parameter sweep backtester using the authenticated KalshiClient.

Fetches closed (settled) markets via KalshiClient.get_markets (requires auth),
then simulates the favorite-maker signal engine against each market using the
shared backtest_engine. Reports EV per parameter set.

USAGE:
    python backtest.py          # full sweep
    python backtest.py --quick  # smaller grid for speed
    python backtest.py --limit N  # max markets per series
    python backtest.py --jobs N   # parallel workers (default: 4)

WARNING: Last price is a rough proxy for entry price. Real backtesting
requires historical orderbook snapshots (see kalshieye_downloader.py).
Fee model uses fees.py for exact Kalshi taker fee calculation.
"""
import argparse
import logging
import sys
import time

import config
from backtest_engine import run_sweep
from kalshi_client import KalshiClient

log = logging.getLogger("backtest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")


def settlement_yes_won(market):
    """YES wins if expiration_value rounds to 100 (preserves backtest.py behavior)."""
    exp_val = market.get("expiration_value")
    if exp_val is None or exp_val == "":
        return None
    try:
        return int(round(float(exp_val) * 100)) >= 100
    except (ValueError, TypeError):
        return None


def fetch_closed_markets(client: KalshiClient, series_list, limit_per_series=100):
    """Fetch closed (settled) markets for each series using the historical tier."""
    all_markets = []
    for series in series_list:
        series_markets = None
        try:
            series_markets = client.fetch_all_historical_markets(
                series_ticker=series, limit=limit_per_series
            )
            if series_markets:
                log.info("%s: %d historical markets fetched", series, len(series_markets))
        except Exception as e:
            log.warning("%s historical fetch failed: %s", series, e)

        if series_markets is None:
            try:
                series_markets = client.get_markets(
                    series_ticker=series, status="closed", limit=limit_per_series
                )
                log.info("%s: %d public markets fetched", series, len(series_markets))
            except Exception as e:
                log.warning("%s fetch failed: %s", series, e)
                series_markets = []

        for m in series_markets:
            m["_series"] = series
        all_markets.extend(series_markets)
        time.sleep(0.15)
    return all_markets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="Smaller parameter grid")
    parser.add_argument("--limit", type=int, default=100, help="Max markets per series")
    parser.add_argument("--top", type=int, default=10, help="Show top N results")
    parser.add_argument("--jobs", type=int, default=4, help="Parallel workers")
    args = parser.parse_args()

    logging.getLogger().setLevel(logging.WARNING)

    client = KalshiClient()
    series = config.TARGET_SERIES

    log.info("Fetching closed markets for %s...", series)
    markets = fetch_closed_markets(client, series, args.limit)
    log.info("Total markets: %d", len(markets))

    if not markets:
        log.error("No markets fetched — cannot backtest")
        sys.exit(1)

    run_sweep(
        markets,
        settlement_yes_won=settlement_yes_won,
        quick=args.quick,
        limit=None,
        top=args.top,
        jobs=args.jobs,
        out_path="backtest_results.json",
        starting_balance=10.0,
    )


if __name__ == "__main__":
    main()
