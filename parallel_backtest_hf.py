"""
parallel_backtest_hf.py — Parameter sweep backtest on DegenHedge/kalshi-btc-15m dataset.

Loads signals.parquet + windows.parquet from Hugging Face, sweeps entry-threshold
and model_probability parameters, computes net EV per trade using the exact
Kalshi quadratic taker fee (fees.py), and reports the highest-EV parameter set.

Usage:
    python parallel_backtest_hf.py          # full sweep
    python parallel_backtest_hf.py --quick  # smaller grid for speed

Output:
    hf_backtest_results.json  — top 50 results
    Prints ranked table + best parameters to stdout.
"""

import argparse
import itertools
import json
import logging
import math
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fees import taker_fee_dollars

LOG = logging.getLogger("parallel_backtest_hf")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")

FEE_RATE = float(os.environ.get("KALSHI_FEE_RATE", "0.07"))
CONTRACTS = 1  # per-trade contract count for EV calc


def roundtrip_pnl_dollars(entry_cents: int, won: bool, contracts: int = 1) -> float:
    """Net P&L in dollars for one round-trip trade using Kalshi taker fees.

    Buy at entry_cents/100, sell at 1.00 (win) or 0.00 (loss).
    Fee charged on both legs via taker_fee_dollars (6dp model).
    """
    entry_price = entry_cents / 100.0
    exit_price = 1.0 if won else 0.0
    entry_value = contracts * entry_price
    exit_value = contracts * exit_price
    fee = taker_fee_dollars(contracts, entry_price) + taker_fee_dollars(
        contracts, exit_price
    )
    return exit_value - entry_value - fee


def evaluate(df: pd.DataFrame, params: dict) -> dict:
    """Filter signals by params and compute EV metrics.

    Params:
        entry_ask_min, entry_ask_max: inclusive entry_ask_cents range
        model_prob_min: minimum model_prob to trade
        side: 'UP', 'DOWN', or 'both'
    """
    d = df.copy()

    entry_min = params.get("entry_ask_min", 1)
    entry_max = params.get("entry_ask_max", 99)
    d = d[(d["entry_ask_cents"] >= entry_min) & (d["entry_ask_cents"] <= entry_max)]

    mp_min = params.get("model_prob_min", 0.0)
    d = d[d["model_prob"] >= mp_min]

    side = params.get("side", "both")
    if side == "UP":
        d = d[d["side"] == "UP"]
    elif side == "DOWN":
        d = d[d["side"] == "DOWN"]

    n = len(d)
    if n == 0:
        return {
            "params": params,
            "n_trades": 0,
            "win_rate": 0.0,
            "avg_entry_cents": 0.0,
            "ev_per_trade": 0.0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "max_drawdown": 0.0,
        }

    won = d["won"].astype(bool)
    win_rate = won.mean()
    avg_entry = d["entry_ask_cents"].mean()

    # P&L per trade
    pnl = [
        roundtrip_pnl_dollars(int(c), bool(w), CONTRACTS)
        for c, w in zip(d["entry_ask_cents"], won)
    ]
    pnl_series = pd.Series(pnl)
    total_pnl = pnl_series.sum()
    ev_per_trade = pnl_series.mean()
    avg_pnl = ev_per_trade

    # Drawdown
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnl:
        running += p
        if running > peak:
            peak = running
        dd = (peak - running) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return {
        "params": params,
        "n_trades": int(n),
        "win_rate": float(win_rate),
        "avg_entry_cents": float(avg_entry),
        "ev_per_trade": float(ev_per_trade),
        "total_pnl": float(total_pnl),
        "avg_pnl": float(avg_pnl),
        "max_drawdown": float(max_dd),
    }


def parameter_grid(quick: bool = False):
    if quick:
        entry_mins = [1, 35, 50, 65]
        entry_maxs = [65, 80, 95, 99]
        model_probs = [0.60, 0.80, 0.90, 0.95]
        sides = ["both"]
    else:
        entry_mins = [1, 20, 35, 50, 60, 65, 70, 75, 80, 85]
        entry_maxs = [65, 70, 75, 80, 85, 90, 95, 99]
        model_probs = [0.60, 0.70, 0.80, 0.85, 0.90, 0.92, 0.94, 0.96, 0.98]
        sides = ["both", "UP", "DOWN"]

    for em in entry_mins:
        for ex in entry_maxs:
            if ex < em:
                continue
            for mp in model_probs:
                for sd in sides:
                    yield {
                        "entry_ask_min": em,
                        "entry_ask_max": ex,
                        "model_prob_min": mp,
                        "side": sd,
                    }


def worker(args):
    df, params = args
    return evaluate(df, params)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="Smaller parameter grid")
    parser.add_argument("--top", type=int, default=20, help="Show top N results")
    parser.add_argument(
        "--jobs", type=int, default=4, help="Parallel workers"
    )
    args = parser.parse_args()

    r = "DegenHedge/kalshi-btc-15m"
    from huggingface_hub import hf_hub_download as g

    LOG.info("Loading signals.parquet ...")
    df = pd.read_parquet(g(r, "signals.parquet", repo_type="dataset"))
    LOG.info("Loaded %d signals", len(df))

    combos = list(parameter_grid(args.quick))
    LOG.info("Testing %d parameter combinations with %d workers...", len(combos), args.jobs)

    work_items = [(df, p) for p in combos]

    results = []
    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            futures = {ex.submit(worker, item): i for i, item in enumerate(work_items)}
            for fut in as_completed(futures):
                results.append(fut.result())
    else:
        for item in work_items:
            results.append(evaluate(*item))

    results.sort(key=lambda r: r["ev_per_trade"], reverse=True)

    print("\n" + "=" * 100)
    print(f"BACKTEST RESULTS — {len(df)} signals, {len(combos)} parameter sets")
    print("=" * 100)
    print(
        f"\n{'Rank':<5} {'EV/trade':<10} {'Total P&L':<10} {'Trades':<7} "
        f"{'Win%':<7} {'AvgEntry':<9} {'MaxDD':<7} {'Params'}"
    )
    print("-" * 100)
    for i, r in enumerate(results[: args.top]):
        p = r["params"]
        print(
            f"{i+1:<5} {r['ev_per_trade']:<10.4f} {r['total_pnl']:<10.2f} "
            f"{r['n_trades']:<7} {r['win_rate']*100:<6.1f}% "
            f"{r['avg_entry_cents']:<9.1f} {r['max_drawdown']*100:<6.1f}% "
            f"emin={p['entry_ask_min']} exmax={p['entry_ask_max']} "
            f"mp>={p['model_prob_min']} side={p['side']}"
        )

    # Save top 50
    out = {
        "n_signals": len(df),
        "n_combos": len(combos),
        "results": [
            {**{"params": r["params"]}, **{k: v for k, v in r.items() if k != "params"}}
            for r in results[:50]
        ],
    }
    with open("hf_backtest_results.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    LOG.info("Results saved to hf_backtest_results.json")

    # Print best
    best = results[0]
    print("\n" + "=" * 80)
    print("BEST PARAMETERS (highest EV):")
    print("=" * 80)
    for k, v in best["params"].items():
        print(f"  {k}: {v}")
    print(f"  EV/trade: ${best['ev_per_trade']:.4f}")
    print(f"  Total P&L: ${best['total_pnl']:.2f}")
    print(f"  Trades: {best['n_trades']}")
    print(f"  Win rate: {best['win_rate']*100:.1f}%")
    print(f"  Avg entry: {best['avg_entry_cents']:.1f}c")
    print(f"  Max drawdown: {best['max_drawdown']*100:.1f}%")


if __name__ == "__main__":
    main()
