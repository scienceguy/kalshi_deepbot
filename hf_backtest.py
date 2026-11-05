"""
hf_backtest.py — Sweep the live strategy on real 15m quote history from Hugging Face.

Dataset: papylove/kalshi-15m-data (quote_history/YYYY-MM-DD.parquet)
One row per minute per window, carrying the live yes_bid/yes_ask and the
official result. That gives the sweep a real fill price at the entry minute and
a real settlement, instead of the synthetic 45c entry the public API forces.

The KalshiEye snapshot endpoint would be the other option, but it needs the
$39.50/mo Research plan; the free Explorer tier returns 402.

Usage:
    python hf_backtest.py --days 3 --quick
    python hf_backtest.py --days 14 --limit 2000 --jobs 2
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

import pandas as pd
from huggingface_hub import HfApi, hf_hub_download

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import run_sweep  # noqa: E402

REPO = "papylove/kalshi-15m-data"
log = logging.getLogger("hf_backtest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")


def _iso(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def quote_days(days: int) -> list:
    """The newest `days` quote_history files in the dataset."""
    files = [
        f.path for f in HfApi().list_repo_tree(REPO, repo_type="dataset", recursive=True)
        if getattr(f, "path", "").startswith("quote_history/") and f.path.endswith(".parquet")
    ]
    return sorted(files)[-days:]


def load_markets(days: int = 3, series: str = "") -> list:
    markets = []
    for path in quote_days(days):
        df = pd.read_parquet(hf_hub_download(REPO, path, repo_type="dataset"))
        if series:
            df = df[df["series"] == series]
        for ticker, g in df.groupby("ticker", sort=False):
            g = g.sort_values("minute")
            first = g.iloc[0]
            quotes = {
                int(r.minute): (int(round(r.yes_bid * 100)), int(round(r.yes_ask * 100)))
                for r in g.itertuples()
                if pd.notna(r.yes_bid) and pd.notna(r.yes_ask) and r.yes_bid and r.yes_ask
            }
            if not quotes:
                continue
            markets.append({
                "ticker": ticker,
                "open_time": _iso(first.open_ts),
                "close_time": _iso(first.close_ts),
                "floor_strike": first.floor_strike,
                "expiration_value": first.expiration_value,
                "result": first.result,
                "minute_quotes": quotes,
            })
        log.info("%s: %d windows so far", path, len(markets))
    return markets


def settlement_yes_won(market):
    result = str(market.get("result") or "").strip().lower()
    if result == "yes":
        return True
    if result == "no":
        return False
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="Newest N days of quote history")
    ap.add_argument("--series", default="", help="Restrict to one series, e.g. KXBTC15M")
    ap.add_argument("--quick", action="store_true", help="Smaller parameter grid")
    ap.add_argument("--limit", type=int, default=None, help="Cap markets used")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--out", default="hf_sweep_results.json")
    ap.add_argument("--balance", type=float, default=10.0,
                    help="Starting bankroll per run (default 10, the live bankroll scale)")
    args = ap.parse_args()

    markets = load_markets(args.days, args.series)
    log.info("Loaded %d windows with real quotes", len(markets))
    if not markets:
        log.error("No markets loaded — dataset layout changed?")
        return 1

    run_sweep(
        markets,
        settlement_yes_won=settlement_yes_won,
        quick=args.quick,
        limit=args.limit,
        top=args.top,
        jobs=args.jobs,
        out_path=args.out,
        starting_balance=args.balance,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
