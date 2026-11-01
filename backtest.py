#!/usr/bin/env python3
"""
backtest.py — Parameter sweep backtester for Kalshi SuperBot.

Fetches historical closed markets from Kalshi API with settlement outcomes,
simulates the signal engine against each market using realistic entry price
estimation based on last_price (NOT on settlement direction — no lookahead bias),
and reports EV per parameter set.

Uses multiprocessing for parallel evaluation.

USAGE:
    python backtest.py          # full sweep
    python backtest.py --quick  # smaller grid for speed
    python backtest.py --limit N  # max markets per series
    python backtest.py --jobs N   # parallel workers (default: 4)

WARNING: Last price is a rough proxy for entry price. Real backtesting
requires historical orderbook snapshots (see kalshieye_downloader.py).
Fee model uses fees.py for exact Kalshi taker fee calculation.
"""

import argparse, itertools, json, logging, sys, time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

import config
import signals as sig_mod
from kalshi_client import KalshiClient
from market_finder import _parse_api_time, _parse_market_prices
from fees import buy_cash_out, sell_cash_in

log = logging.getLogger("backtest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")

# Module-level cache for markets, populated once per worker process via init_worker.
# Avoids pickling the full markets list into every work item (O(workers × markets) memory).
_cached_markets = None


def init_worker(markets):
    """Pool initializer — load markets once per worker process."""
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
    BALANCE_PCT_PER_TRADE: float = 0.10  # Kelly = 0.10
    STARTING_BALANCE: float = 10.00


def fetch_closed_markets(client: KalshiClient, series_list, limit_per_series=100):
    """Fetch closed (settled) markets for each series.

    Tries the historical tier (/historical/markets) first — it returns
    settled markets with expiration_value, close_time, floor_strike and
    other settlement fields, which is exactly what backtesting needs.
    Falls back to the public /markets endpoint if the historical tier
    is unavailable.
    """
    all_markets = []
    for series in series_list:
        series_markets = None

        # Historical tier: settled markets with settlement fields (no auth required)
        try:
            series_markets = client.fetch_all_historical_markets(
                series_ticker=series, limit=limit_per_series
            )
            if series_markets:
                log.info("%s: %d historical markets fetched", series, len(series_markets))
        except Exception as e:
            log.warning("%s historical fetch failed: %s", series, e)

        # Fall back to public /markets endpoint
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


def simulate_trade(market: dict, params: StratParams, balance: float = 10.0):
    """
    Simulate one market through the strategy engine using real settlement data.

    Entry price is estimated from last_price_dollars (last traded price before
    settlement). This is a rough proxy — real backtesting needs historical
    orderbook snapshots from Kalshi Eye (kalshieye_downloader.py).

    Settlement outcome comes from expiration_value (0 or 100).

    Returns a result dict or None if no signal.
    """
    ticker = market.get("ticker", "")
    ct = _parse_api_time(market.get("close_time", ""))
    ot = _parse_api_time(market.get("open_time", ""))
    if not ct or not ot:
        return None

    # For closed markets, measure the 15-min window from open_time to close_time
    # (not now→close_time, which is negative for settled markets).
    minutes_in = (ct - ot).total_seconds() / 60
    if minutes_in <= 0 or minutes_in > 15.5:
        return None  # not a valid 15-min market window

    minutes_left = 15.0 - minutes_in

    # ── Timing gate ─────────────────────────────────────────────────────────
    if minutes_in < params.ENTRY_WINDOW_START_MIN:
        return None
    if minutes_in > params.ENTRY_WINDOW_END_MIN:
        return None

    # ── Parse prices ────────────────────────────────────────────────────────
    yb, ya, nb, na, yq, nq = _parse_market_prices(market)

    # Use last_price_dollars as entry price proxy (NOT settlement-derived)
    last_price = market.get("last_price_dollars")
    if last_price and float(last_price) > 0:
        mid_proxy = int(round(float(last_price) * 100))
    elif yb and ya:
        mid_proxy = (yb + ya) // 2
    else:
        return None  # no usable price data

    # ── Settlement outcome ──────────────────────────────────────────────────
    exp_val = market.get("expiration_value")
    if exp_val is not None:
        settled_yes = int(round(float(exp_val) * 100))  # 0 or 100
    else:
        return None  # skip markets without expiration_value (no lookahead bias)

    # ── Simulate entry price based on last_price + typical spread ───────────
    # At entry time, we don't know future skew. Use mid_proxy as best estimate.
    # For a favorite-maker strategy, entry is near mid (82-93c band).
    if mid_proxy >= params.ENTRY_BAND_MIN:
        # Price is in favorite range — this is where we'd enter
        bullish = mid_proxy >= params.ENTRY_BAND_MIN and mid_proxy <= params.ENTRY_BAND_MAX
        if bullish:
            # Buying YES at entry band
            ep = mid_proxy
            side_bullish = True
        else:
            ep = 100 - mid_proxy  # NO price complement
            side_bullish = False
    else:
        # Price outside favorite band — no signal
        return None

    ep = max(1, min(99, int(ep)))

    # NO cap
    if not side_bullish and ep > params.MAX_NO_ENTRY_CENTS:
        return None

    # ── Size using Kelly/position sizing ────────────────────────────────────
    max_dollars = min(params.MAX_TRADE_DOLLARS, balance * params.BALANCE_PCT_PER_TRADE)
    cost = ep / 100.0
    n = int(max_dollars / cost)
    if n < 1 and max_dollars >= cost:
        n = 1
    n = max(0, n)
    if n < 1:
        return None

    dollar_risk = n * ep / 100.0

    # ── Fee-aware P&L ───────────────────────────────────────────────────────
    # Round-trip fee model (see fees.py):
    #   buy_cash_out  = cash LEAVING the account on entry  = cost + entry taker fee, ceiled to cent
    #   sell_cash_in  = cash CREDITED on exit               = proceeds - exit taker fee, floored to cent
    # So pnl = sell_revenue - buy_cost already nets both legs' fees.
    # Buy at ep, sell at settlement (100 for YES win, 0 for YES loss)
    entry_price_dollars = ep / 100.0
    exit_price_dollars = (
        1.0 if settled_yes >= 100 else 0.0
    ) if side_bullish else (
        0.0 if settled_yes >= 100 else 1.0
    )

    # Verified arg types: contracts=n (int, float-compatible), price in dollars (not cents).
    buy_cost = buy_cash_out(n, entry_price_dollars)
    sell_revenue = sell_cash_in(n, exit_price_dollars)
    pnl = sell_revenue - buy_cost

    # `won` is fee-aware: a trade only counts as a win if the exit proceeds
    # exceed the all-in entry cost. If the price move is smaller than the
    # round-trip fee (or the market settles flat), sell_revenue <= buy_cost
    # and won is False — fees must be overcome to book a win.
    won = sell_revenue > buy_cost

    return {
        "ticker": ticker,
        "direction": "YES" if side_bullish else "NO",
        "entry_price": ep,
        "contracts": n,
        "dollar_risk": dollar_risk,
        "buy_cost": buy_cost,
        "sell_revenue": sell_revenue,
        "settled_yes": settled_yes,
        "won": won,
        "pnl": pnl,
        "minutes_in": minutes_in,
        "mid": mid_proxy,
    }


def run_backtest(params: StratParams, markets: list, starting_balance: float = 10.0):
    """Run the full backtest for one parameter set."""
    # `balance` = available cash. It is reduced by buy_cost on entry and
    # increased by sell_revenue on exit. Used for risk checks below.
    balance = starting_balance
    trades = []

    for m in markets:
        result = simulate_trade(m, params, balance)
        if not result:
            continue

        # Risk checks
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

    # Max drawdown: `running` is cumulative P&L (starting_balance + sum of
    # pnl), independent of `balance` above. It tracks peak-to-trough on the
    # equity curve, not available cash.
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
    """Worker function for parallel backtest."""
    combo, starting_balance = args
    params = StratParams(**combo)
    result = run_backtest(params, _cached_markets, starting_balance)
    result["params"] = combo
    return result


def parameter_grid(quick: bool = False):
    """Generate parameter combinations to test."""
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

    # Save full results
    out = {
        "n_markets": len(markets),
        "n_combos": len(combos),
        "results": [
            {**{"params": r["params"]}, **{k: v for k, v in r.items() if k != "trades"}}
            for r in results[:50]
        ],
    }
    out_path = Path("backtest_results.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    log.info("Full results saved to %s", out_path)

    # Show best params detail
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