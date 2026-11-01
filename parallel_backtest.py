#!/usr/bin/env python3
"""
parallel_backtest.py — Parallel parameter sweep backtest using public Kalshi API.

Fetches settled (finalized) markets from the public Kalshi markets endpoint
(does not require authentication), then simulates the signal engine against
each market across a parameter grid using multiprocessing.

Settlement outcome for Kalshi 15-min binary markets:
  YES wins if expiration_value (BTC price at settlement) >= floor_strike (strike)
  NO wins otherwise

Entry price proxy: last_price_dollars (YES price in dollars, 0-1) — the price
at which we would have entered as a maker.
"""
import argparse, itertools, json, logging, sys, time
from dataclasses import dataclass
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

import requests
import config
from market_finder import _parse_api_time, _parse_market_prices
from fees import buy_cash_out, sell_cash_in

log = logging.getLogger("parallel_backtest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

_cached_markets = None


def init_worker(markets):
    global _cached_markets
    _cached_markets = markets


@dataclass
class StratParams:
    MIN_SKEW_CENTS: int = 5
    MIN_TREND_CENTS: int = 12
    ENTRY_WINDOW_START_MIN: float = 6.0
    ENTRY_WINDOW_END_MIN: float = 11.0
    ENTRY_BAND_MIN: int = 82
    ENTRY_BAND_MAX: int = 93
    MAX_NO_ENTRY_CENTS: int = 88
    AGGRESSIVE_FILL: bool = False
    PER_TRADE_PCT: float = 0.08
    MAX_TRADE_DOLLARS: float = 3.00
    MAX_OPEN_POSITIONS: int = 4
    BALANCE_PCT_PER_TRADE: float = 0.10
    STARTING_BALANCE: float = 10.00


def fetch_settled_markets_public(series_list, limit_per_series=1000):
    """Fetch settled markets from public Kalshi endpoint (no auth required)."""
    all_markets = []
    for series in series_list:
        url = f"{BASE_URL}/markets"
        params = {"series_ticker": series, "limit": limit_per_series}
        try:
            r = requests.get(url, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            markets = data.get("markets", [])
            # Keep only finalized markets with settlement data
            settled = []
            for m in markets:
                if m.get("status") == "finalized" and m.get("expiration_value"):
                    m["_series"] = series
                    settled.append(m)
            all_markets.extend(settled)
            log.info("%s: %d settled markets fetched (out of %d total)", series, len(settled), len(markets))
        except Exception as e:
            log.warning("%s fetch failed: %s", series, e)
        time.sleep(0.2)
    return all_markets


def settlement_yes_won(market):
    """Determine if YES won for a Kalshi 15-min binary market."""
    exp_val = market.get("expiration_value")
    floor_strike = market.get("floor_strike")
    if exp_val is None or exp_val == "" or floor_strike is None or floor_strike == "":
        return None
    try:
        ev = float(exp_val)
        fs = float(floor_strike)
        return ev >= fs
    except (ValueError, TypeError):
        return None


def simulate_trade(market, params, balance=10.0):
    """Simulate one market through the signal engine."""
    ticker = market.get("ticker", "")
    ct = _parse_api_time(market.get("close_time", ""))
    ot = _parse_api_time(market.get("open_time", ""))
    if not ct or not ot:
        return None

    minutes_in = (ct - ot).total_seconds() / 60
    if minutes_in <= 0 or minutes_in > 15.5:
        return None  # not a valid 15-min market window

    # Timing gate: simulate entering at ENTRY_WINDOW_START_MIN into the window.
    # For settled markets (minutes_in == 15), we simulate entering at the start
    # of the entry window using last_price_dollars as a rough entry price proxy.
    # NOTE: last_price is at settlement, not entry — this is a limitation.
    entry_minute = params.ENTRY_WINDOW_START_MIN
    if entry_minute < 0 or entry_minute > 15.0:
        return None

    # Parse prices
    yb, ya, nb, na, yq, nq = _parse_market_prices(market)

    # Entry price proxy: last_price_dollars (YES price in dollars)
    last_price = market.get("last_price_dollars")
    if last_price and float(last_price) > 0:
        mid_proxy = int(round(float(last_price) * 100))
    elif yb and ya:
        mid_proxy = (yb + ya) // 2
    else:
        return None

    # Settlement outcome
    yes_won = settlement_yes_won(market)
    if yes_won is None:
        return None

    # Entry price simulation - favorite maker strategy
    if mid_proxy >= params.ENTRY_BAND_MIN:
        bullish = mid_proxy >= params.ENTRY_BAND_MIN and mid_proxy <= params.ENTRY_BAND_MAX
        if bullish:
            ep = mid_proxy
            side_bullish = True
        else:
            ep = 100 - mid_proxy  # NO price complement
            side_bullish = False
    else:
        return None  # Price outside favorite band

    ep = max(1, min(99, int(ep)))

    if not side_bullish and ep > params.MAX_NO_ENTRY_CENTS:
        return None

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

    # Fee-aware P&L: buy at ep, sell at settlement (100 for YES win, 0 for YES loss)
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
        "minutes_in": minutes_in,
        "mid": mid_proxy,
    }


def run_backtest(params, markets, starting_balance=10.0):
    balance = starting_balance
    trades = []

    for m in markets:
        result = simulate_trade(m, params, balance)
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
    result = run_backtest(params, _cached_markets, starting_balance)
    result["params"] = combo
    return result


def parameter_grid(quick=False):
    if quick:
        grids = {
            "MIN_SKEW_CENTS": [3, 5, 7],
            "MIN_TREND_CENTS": [8, 12, 16],
            "ENTRY_WINDOW_START_MIN": [6.0, 8.0],
            "ENTRY_WINDOW_END_MIN": [11.0, 13.0],
            "ENTRY_BAND_MIN": [82, 88],
            "ENTRY_BAND_MAX": [90, 93],
            "AGGRESSIVE_FILL": [False],
            "PER_TRADE_PCT": [0.05, 0.08, 0.12],
            "MAX_TRADE_DOLLARS": [2.0, 3.0, 5.0],
            "BALANCE_PCT_PER_TRADE": [0.05, 0.10, 0.15],
        }
    else:
        grids = {
            "MIN_SKEW_CENTS": [3, 5, 7, 10],
            "MIN_TREND_CENTS": [8, 12, 15, 20],
            "ENTRY_WINDOW_START_MIN": [6.0, 8.0, 10.0],
            "ENTRY_WINDOW_END_MIN": [11.0, 13.0, 14.0],
            "ENTRY_BAND_MIN": [82, 86, 88, 90],
            "ENTRY_BAND_MAX": [88, 91, 93, 95],
            "MAX_NO_ENTRY_CENTS": [60, 65, 68],
            "AGGRESSIVE_FILL": [False, True],
            "PER_TRADE_PCT": [0.04, 0.08, 0.12, 0.20],
            "MAX_TRADE_DOLLARS": [2.0, 3.0, 5.0, 10.0],
            "BALANCE_PCT_PER_TRADE": [0.05, 0.10, 0.15, 0.25],
        }
    keys = list(grids.keys())
    for combo in itertools.product(*[grids[k] for k in keys]):
        yield dict(zip(keys, combo))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()

    logging.getLogger().setLevel(logging.WARNING)

    series = config.TARGET_SERIES
    log.info("Fetching settled markets for %s...", series)
    markets = fetch_settled_markets_public(series, args.limit)
    log.info("Total settled markets: %d", len(markets))

    if not markets:
        log.error("No settled markets fetched — cannot backtest")
        sys.exit(1)

    combos = list(parameter_grid(args.quick))
    log.info("Testing %d parameter combinations with %d workers...", len(combos), args.jobs)

    work_items = [(c, 10.0) for c in combos]

    with Pool(processes=args.jobs, initializer=init_worker, initargs=(markets,)) as pool:
        results = pool.map(_backtest_one, work_items)

    results.sort(key=lambda r: r["ev"], reverse=True)

    print("\n" + "=" * 90)
    print(f"BACKTEST RESULTS — {len(markets)} markets, {len(combos)} parameter sets")
    print("=" * 90)
    print(f"\n{'Rank':<5} {'EV/trade':<9} {'Total P&L':<10} {'Trades':<7} {'Win%':<7} {'Avg P&L':<9} {'MaxDD':<7} {'Params'}")
    print("-" * 90)
    for i, r in enumerate(results[:args.top]):
        p = r["params"]
        print(f"{i+1:<5} {r['ev']:<9.4f} {r['total_pnl']:<10.2f} {r['n_trades']:<7} "
              f"{r['win_rate']*100:<6.1f}% {r['avg_pnl']:<9.4f} {r['max_drawdown']*100:<6.1f}% "
              f"skew={p.get('MIN_SKEW_CENTS','-')} trend={p.get('MIN_TREND_CENTS','-')} "
              f"win={p.get('ENTRY_WINDOW_START_MIN','-')}-{p.get('ENTRY_WINDOW_END_MIN','-')}min "
              f"band={p.get('ENTRY_BAND_MIN','-')}-{p.get('ENTRY_BAND_MAX','-')} "
              f"agg={p.get('AGGRESSIVE_FILL','-')}")

    out = {
        "n_markets": len(markets),
        "n_combos": len(combos),
        "results": [
            {**{"params": r["params"]}, **{k: v for k, v in r.items() if k != "trades"}}
            for r in results[:50]
        ],
    }
    out_path = Path("parallel_backtest_results.json")
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
