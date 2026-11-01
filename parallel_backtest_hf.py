#!/usr/bin/env python3
"""
parallel_backtest_hf.py — Parallel parameter sweep using HuggingFace Kalshi BTC 15m features dataset.

Loads all 8 parquet shards from bennett-tan/kalshi-btc-15m-features and runs
a parallel parameter sweep to find the strategy with highest EV per trade.

Strategy: favorite-maker limit orders
- Enter YES when price is in favorite band (82-99c)
- Enter NO when price is in NO favorite band (equivalently YES price in 1-18c)
- Entry price proxy: p_last (last observed price before settlement)
- Settlement from resolved_yes field
- Fee-aware P&L using fees.py
"""
import argparse, itertools, json, logging, sys
from dataclasses import dataclass
from multiprocessing import Pool
from pathlib import Path

import pyarrow.parquet as pq
from fees import buy_cash_out, sell_cash_in

log = logging.getLogger("parallel_backtest_hf")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")

_cached_data = None


def init_worker(data):
    global _cached_data
    _cached_data = data


@dataclass
class StratParams:
    ENTRY_BAND_MIN: int = 82
    ENTRY_BAND_MAX: int = 93
    MAX_NO_ENTRY_CENTS: int = 88
    MIN_SKEW_CENTS: int = 3
    MIN_TREND_CENTS: int = 8
    ENTRY_WINDOW_START_MIN: float = 6.0
    ENTRY_WINDOW_END_MIN: float = 11.0
    AGGRESSIVE_FILL: bool = False
    MAX_OPEN_POSITIONS: int = 4
    PER_TRADE_PCT: float = 0.08
    MAX_TRADE_DOLLARS: float = 3.00
    BALANCE_PCT_PER_TRADE: float = 0.10
    STARTING_BALANCE: float = 10.00


def load_all_data():
    """Load all parquet shards from hf_data/."""
    all_rows = []
    data_dir = Path("hf_data")
    for pq_file in sorted(data_dir.glob("*.parquet")):
        table = pq.read_table(str(pq_file))
        df = table.to_pandas()
        all_rows.append(df)
        log.info("Loaded %s: %d rows", pq_file.name, len(df))
    if not all_rows:
        return []
    import pandas as pd
    combined = pd.concat(all_rows, ignore_index=True)
    return combined.to_dict("records")


def simulate_trade(row, params, balance=10.0):
    """Simulate one market through the favorite-maker signal engine."""
    ticker = row.get("market_ticker", "")
    p_open = row.get("p_open", 0.5)
    p_last = row.get("p_last", 0.5)
    resolved_yes = row.get("resolved_yes", 0)

    # Entry price proxy: last price (price at end of window before settlement)
    # Note: this is a conservative estimate — actual entry would be earlier
    mid_proxy = int(round(p_last * 100))

    # YES won?
    yes_won = bool(resolved_yes == 1)

    # Favorite-maker entry: only enter YES when it is the favorite (in band).
    # We do NOT bet the underdog (NO complement) when YES is above the band —
    # that contradicts the favourite-maker strategy.
    if mid_proxy < params.ENTRY_BAND_MIN or mid_proxy > params.ENTRY_BAND_MAX:
        # Price outside favorite band — no signal
        return None

    ep = mid_proxy
    side_bullish = True

    # Position sizing
    max_dollars = min(params.MAX_TRADE_DOLLARS, balance * params.BALANCE_PCT_PER_TRADE)
    cost = ep / 100.0
    n = int(max_dollars / cost)
    if n < 1 and max_dollars >= cost:
        n = 1
    n = max(0, n)
    if n < 1:
        return None

    dollar_risk = n * ep / 100.0

    # Fee-aware P&L: buy at ep, sell at settlement
    entry_price_dollars = ep / 100.0
    exit_price_dollars = (
        1.0 if yes_won else 0.0
    ) if side_bullish else (
        0.0 if yes_won else 1.0
    )

    buy_cost = buy_cash_out(n, entry_price_dollars)
    sell_revenue = sell_cash_in(n, exit_price_dollars)
    pnl = sell_revenue - buy_cost
    won = sell_revenue > buy_cost

    return {
        "ticker": ticker,
        "direction": "YES" if side_bullish else "NO",
        "entry_price": ep,
        "contracts": n,
        "dollar_risk": dollar_risk,
        "buy_cost": buy_cost,
        "sell_revenue": sell_revenue,
        "settled_yes": 100 if yes_won else 0,
        "won": won,
        "pnl": pnl,
        "p_open": p_open,
        "p_last": p_last,
        "mid": mid_proxy,
    }


def run_backtest(params, data, starting_balance=10.0):
    balance = starting_balance
    trades = []

    for row in data:
        result = simulate_trade(row, params, balance)
        if not result:
            continue
        if result["dollar_risk"] > balance * params.PER_TRADE_PCT:
            continue
        if result["dollar_risk"] > params.MAX_TRADE_DOLLARS:
            continue
        balance -= result["buy_cost"]
        balance += result["sell_revenue"]
        trades.append(result)

    if not trades:
        return {"ev": 0.0, "total_pnl": 0.0, "n_trades": 0, "win_rate": 0.0,
                "avg_pnl": 0.0, "final_balance": balance, "max_drawdown": 0.0}

    total_pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["won"])
    win_rate = wins / len(trades)
    avg_pnl = total_pnl / len(trades)
    ev_per_trade = avg_pnl

    running = starting_balance
    peak = starting_balance
    max_dd = 0.0
    for t in trades:
        running += t["pnl"]
        if running > peak:
            peak = running
        dd = (peak - running) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    return {
        "ev": ev_per_trade,
        "total_pnl": total_pnl,
        "n_trades": len(trades),
        "win_rate": win_rate,
        "avg_pnl": avg_pnl,
        "final_balance": balance,
        "max_drawdown": max_dd,
        "trades": trades,
    }


def _backtest_one(args):
    combo, starting_balance = args
    params = StratParams(**combo)
    result = run_backtest(params, _cached_data, starting_balance)
    result["params"] = combo
    return result


def parameter_grid(quick=False):
    if quick:
        grids = {
            "ENTRY_BAND_MIN": [82, 86, 88],
            "ENTRY_BAND_MAX": [88, 91, 93],
            "MAX_NO_ENTRY_CENTS": [60, 65, 68],
            "PER_TRADE_PCT": [0.10, 0.20],
            "MAX_TRADE_DOLLARS": [2.0, 3.0, 5.0],
            "BALANCE_PCT_PER_TRADE": [0.10, 0.25],
        }
    else:
        grids = {
            "ENTRY_BAND_MIN": [75, 80, 82, 85, 88],
            "ENTRY_BAND_MAX": [88, 90, 92, 95, 98],
            "MAX_NO_ENTRY_CENTS": [55, 60, 65, 70, 75],
            "PER_TRADE_PCT": [0.10, 0.20],
            "MAX_TRADE_DOLLARS": [2.0, 3.0, 5.0, 10.0],
            "BALANCE_PCT_PER_TRADE": [0.10, 0.25],
        }
    keys = list(grids.keys())
    for combo in itertools.product(*[grids[k] for k in keys]):
        yield dict(zip(keys, combo))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()

    logging.getLogger().setLevel(logging.WARNING)

    log.info("Loading HuggingFace dataset...")
    data = load_all_data()
    log.info("Total market windows: %d", len(data))

    if not data:
        log.error("No data loaded — cannot backtest")
        sys.exit(1)

    combos = list(parameter_grid(args.quick))
    log.info("Testing %d parameter combinations with %d workers...", len(combos), args.jobs)

    work_items = [(c, 10.0) for c in combos]

    with Pool(processes=args.jobs, initializer=init_worker, initargs=(data,)) as pool:
        results = pool.map(_backtest_one, work_items)

    results.sort(key=lambda r: r["ev"], reverse=True)

    print("\n" + "=" * 90)
    print(f"BACKTEST RESULTS — {len(data)} markets, {len(combos)} parameter sets")
    print("=" * 90)
    print(f"\n{'Rank':<5} {'EV/trade':<9} {'Total P&L':<10} {'Trades':<7} {'Win%':<7} {'Avg P&L':<9} {'MaxDD':<7} {'Params'}")
    print("-" * 90)
    for i, r in enumerate(results[:args.top]):
        p = r["params"]
        print(f"{i+1:<5} {r['ev']:<9.4f} {r['total_pnl']:<10.2f} {r['n_trades']:<7} "
              f"{r['win_rate']*100:<6.1f}% {r['avg_pnl']:<9.4f} {r['max_drawdown']*100:<6.1f}% "
              f"band={p.get('ENTRY_BAND_MIN','-')}-{p.get('ENTRY_BAND_MAX','-')} "
              f"no_cap={p.get('MAX_NO_ENTRY_CENTS','-')} "
              f"pct={p.get('PER_TRADE_PCT','-')} max_$={p.get('MAX_TRADE_DOLLARS','-')} "
              f"bal_pct={p.get('BALANCE_PCT_PER_TRADE','-')}")

    out = {
        "n_markets": len(data),
        "n_combos": len(combos),
        "results": [
            {**{"params": r["params"]}, **{k: v for k, v in r.items() if k != "trades"}}
            for r in results[:50]
        ],
    }
    out_path = Path("hf_backtest_results.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    log.info("Full results saved to %s", out_path)

    best = results[0]
    print("\n" + "=" * 80)
    print("BEST PARAMETERS (highest EV):")
    print("=" * 80)
    for k, v in best["params"].items():
        print(f"  {k}: {v}")
    print(f"\n  EV/trade: ${best['ev']:.4f}")
    print(f"  Total P&L: ${best['total_pnl']:.2f}")
    print(f"  Trades: {best['n_trades']}")
    print(f"  Win rate: {best['win_rate']*100:.1f}%")
    print(f"  Max drawdown: {best['max_drawdown']*100:.1f}%")
    print(f"  Final balance: ${best['final_balance']:.2f}")


if __name__ == "__main__":
    main()
