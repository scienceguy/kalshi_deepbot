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
from backtest_engine import _default_settlement_yes_won as settlement_yes_won, run_sweep
from kalshi_client import KalshiClient

log = logging.getLogger("backtest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")


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
    parser.add_argument("--replay", action="store_true", help="Paper replay mode: run current config params over fetched markets, printing one-line per market")
    args = parser.parse_args()

    logging.getLogger().setLevel(logging.WARNING)

    # Validate credentials before constructing KalshiClient
    config.validate_credentials()

    client = KalshiClient()
    series = config.TARGET_SERIES

    log.info("Fetching closed markets for %s...", series)
    markets = fetch_closed_markets(client, series, args.limit)
    log.info("Total markets: %d", len(markets))

    if not markets:
        log.error("No markets fetched — cannot backtest")
        sys.exit(1)

    if args.replay:
        from backtest_engine import replay_backtest, StratParams
        # Use current config parameters (config already imported at module level)
        params = StratParams(
            STRATEGY_MODE=getattr(config, 'STRATEGY_MODE', 'final_taker'),
            ENTRY_WINDOW_START_MIN=getattr(config, 'ENTRY_WINDOW_START_MIN', 14.0),
            ENTRY_WINDOW_END_MIN=getattr(config, 'ENTRY_WINDOW_END_MIN', 15.0),
            ENTRY_BAND_MIN=getattr(config, 'ENTRY_BAND_MIN', 75),
            ENTRY_BAND_MAX=getattr(config, 'ENTRY_BAND_MAX', 99),
            AGGRESSIVE_FILL=getattr(config, 'AGGRESSIVE_FILL', True),
            EXIT_STOP_LOSS_CENTS=getattr(config, 'EXIT_STOP_LOSS_CENTS', 0),
            PER_TRADE_PCT=getattr(config, 'PER_TRADE_PCT', 0.08),
            MAX_TRADE_DOLLARS=getattr(config, 'MAX_TRADE_DOLLARS', 3.00),
            MAX_OPEN_POSITIONS=getattr(config, 'MAX_OPEN_POSITIONS', 4),
            BALANCE_PCT_PER_TRADE=getattr(config, 'BALANCE_PCT_PER_TRADE', 0.10),
            STARTING_BALANCE=100.0,
        )
        log.info("Running paper replay with %d markets...", len(markets))
        result = replay_backtest(params, markets, settlement_yes_won=settlement_yes_won)
        log.info("Replay complete: %d trades, EV/trade=$%.2f, total P&L=$%.2f, win_rate=%.1f%%",
                 result["n_trades"], result["ev"], result["total_pnl"], result["win_rate"]*100)
        sys.exit(0)

    run_sweep(
        markets,
        settlement_yes_won=settlement_yes_won,
        quick=args.quick,
        limit=None,
        top=args.top,
        jobs=args.jobs,
        out_path="backtest_results.json",
        starting_balance=100.0,
    )


if __name__ == "__main__":
    main()
