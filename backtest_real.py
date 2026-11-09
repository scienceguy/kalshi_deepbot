#!/usr/bin/env python3
"""
backtest_real.py — Backtest final-minute taker strategy using REAL Kalshi data.

Data sources:
1. Kalshi API /historical/markets — settled markets with expiration_value, floor_strike,
   open_time, close_time (real settlement outcomes)
2. Kalshi API /historical/markets/{ticker}/candlesticks — real 1-minute OHLCV candles
   including yes_ask/yes_bid close prices per minute

Strategy: Final-minute taker — enter at minute 14, pay yes_ask close on favorite,
hold to settlement. No early exit.
"""
import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from backtest_engine import StratParams, run_sweep
from kalshi_client import KalshiClient
from dateutil import parser

log = logging.getLogger("backtest_real")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")


def load_settlement_markets(client, series_list, limit_per_series=200):
    """Fetch historical settled markets for each series."""
    all_markets = []
    for series in series_list:
        try:
            markets = client.fetch_all_historical_markets(
                series_ticker=series, limit=limit_per_series
            )
            log.info("%s: %d historical markets fetched", series, len(markets))
        except Exception as e:
            log.warning("%s historical fetch failed: %s", series, e)
            markets = []
        for m in markets:
            m["_series"] = series
        all_markets.extend(markets)
        time.sleep(0.1)
    return all_markets


def fetch_candlesticks_for_market(client, market):
    """Fetch 1-minute candlesticks for a market's full 15-minute window."""
    ticker = market["ticker"]
    try:
        open_dt = parser.parse(market["open_time"])
        close_dt = parser.parse(market["close_time"])
    except Exception:
        return None

    try:
        candles = client.get_market_candlesticks(
            ticker,
            start_ts=int(open_dt.timestamp()),
            end_ts=int(close_dt.timestamp()),
            period_interval=1,
            limit=100,
        )
    except Exception as e:
        log.debug("Candlestick fetch failed for %s: %s", ticker, e)
        return None

    if not candles or not isinstance(candles, dict):
        return None

    candle_list = candles.get("candlesticks", [])
    if not candle_list:
        return None

    # Sort by end_period_ts to ensure chronological order
    candle_list.sort(key=lambda c: c.get("end_period_ts", 0))

    # Build minute_quotes: {minute: (bid_cents, ask_cents)}
    # Minute 1 = first candle after open, minute 15 = last candle at close
    minute_quotes = {}
    for i, c in enumerate(candle_list):
        minute = i + 1
        if minute > 15:
            break
        yes_ask_close = c.get("yes_ask", {}).get("close")
        yes_bid_close = c.get("yes_bid", {}).get("close")
        if yes_ask_close is None or yes_bid_close is None:
            continue
        try:
            ask_cents = int(round(float(yes_ask_close) * 100))
            bid_cents = int(round(float(yes_bid_close) * 100))
            ask_cents = max(1, min(99, ask_cents))
            bid_cents = max(1, min(99, bid_cents))
            # Ensure ask > bid
            if ask_cents <= bid_cents:
                ask_cents = min(99, bid_cents + 1)
            minute_quotes[minute] = (bid_cents, ask_cents)
        except (ValueError, TypeError):
            continue

    return minute_quotes if minute_quotes else None


def market_to_backtest_format(market, minute_quotes):
    """Convert a real market + candlesticks to backtest_engine format."""
    ticker = market.get("ticker", "")
    close_time = market.get("close_time", "")
    open_time = market.get("open_time", "")
    expiration_value = market.get("expiration_value")
    floor_strike = market.get("floor_strike")

    # YES wins if expiration_value >= floor_strike
    try:
        yes_won = float(expiration_value) >= float(floor_strike)
    except (ValueError, TypeError):
        return None

    if not minute_quotes or 14 not in minute_quotes:
        return None  # Need minute 14 data for final-minute entry

    return {
        "ticker": ticker,
        "close_time": close_time,
        "open_time": open_time,
        "expiration_value": expiration_value,
        "floor_strike": floor_strike,
        "minute_quotes": minute_quotes,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="smaller grid for speed")
    parser.add_argument("--limit", type=int, default=200, help="max markets per series")
    parser.add_argument("--jobs", type=int, default=4, help="parallel workers")
    parser.add_argument("--starting-balance", type=float, default=100.0, help="starting cash")
    args = parser.parse_args()

    series_list = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXXRP15M",
                   "KXBNB15M", "KXHYPE15M", "KXZEC15M", "KXNEAR15M"]

    client = KalshiClient()

    # Fetch historical settled markets
    markets = load_settlement_markets(client, series_list, limit_per_series=args.limit)
    log.info("Total markets fetched: %d", len(markets))

    # Fetch candlesticks and convert to backtest format
    backtest_markets = []
    for i, m in enumerate(markets):
        minute_quotes = fetch_candlesticks_for_market(client, m)
        if minute_quotes is None:
            continue
        bm = market_to_backtest_format(m, minute_quotes)
        if bm:
            backtest_markets.append(bm)

        if (i + 1) % 20 == 0:
            log.info("Processed %d/%d markets, %d with quotes",
                     i + 1, len(markets), len(backtest_markets))

        time.sleep(0.05)  # Rate limit

    log.info("Final: %d markets with candlestick data", len(backtest_markets))

    if not backtest_markets:
        log.error("No markets with usable candlestick data!")
        sys.exit(1)

    # Save markets for reuse
    with open("real_markets.json", "w") as f:
        json.dump(backtest_markets, f, indent=2, default=str)
    log.info("Saved to real_markets.json")

    # Run backtest sweep
    results = run_sweep(
        backtest_markets,
        quick=args.quick,
        jobs=args.jobs,
        out_path="backtest_real_results.json",
        starting_balance=args.starting_balance,
    )

    best = results[0]
    print("\n" + "=" * 80)
    print("OPTIMAL PARAMETERS — REAL KALSHI HISTORICAL DATA")
    print("=" * 80)
    for k, v in best["params"].items():
        print(f"  {k}: {v}")
    print(f"\n  EV/trade: ${best['ev']:.4f}")
    print(f"  Total P&L: ${best['total_pnl']:.2f}")
    print(f"  Trades: {best['n_trades']}")
    print(f"  Win rate: {best['win_rate']*100:.1f}%")
    print(f"  Max drawdown: {best['max_drawdown']*100:.1f}%")
    print(f"  Final balance: ${best['final_balance']:.2f}")
    print(f"  Gross edge per contract: {best['gross_edge_cents']:+.1f}c")
    print(f"  Avg entry: {best['avg_entry_cents']:.1f}c")

    summary = {
        "params": best["params"],
        "ev_per_trade": best["ev"],
        "total_pnl": best["total_pnl"],
        "n_trades": best["n_trades"],
        "win_rate": best["win_rate"],
        "max_drawdown": best["max_drawdown"],
        "final_balance": best["final_balance"],
        "gross_edge_cents": best["gross_edge_cents"],
        "avg_entry_cents": best["avg_entry_cents"],
        "n_markets": len(backtest_markets),
        "data_source": "real_kalshi_candlesticks",
    }
    with open("best_params_real.json", "w") as f:
        json.dump(summary, f, indent=2)
    print("\nBest params saved to best_params_real.json")


if __name__ == "__main__":
    main()
