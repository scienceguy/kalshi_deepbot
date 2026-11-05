"""
backtest_engine.py — Shared parameter-sweep backtest engine.

Contains the core backtesting logic used by both backtest.py (authenticated
KalshiClient fetcher) and parallel_backtest.py (public REST fetcher). Keeping
one copy of the engine means fee-model or signal changes only need one edit.

Usage:
    from backtest_engine import StratParams, run_backtest, parameter_grid
"""
import itertools
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from multiprocessing import Pool
from fees import buy_cash_out, sell_cash_in
from market_finder import _parse_api_time, _parse_market_prices

log = logging.getLogger("backtest_engine")


@dataclass
class StratParams:
    MIN_SKEW_CENTS: int = 3
    MIN_TREND_CENTS: int = 8
    ENTRY_WINDOW_START_MIN: float = 6.0
    ENTRY_WINDOW_END_MIN: float = 11.0
    ENTRY_BAND_MIN: int = 15
    ENTRY_BAND_MAX: int = 55
    MAX_NO_ENTRY_CENTS: int = 55
    AGGRESSIVE_FILL: bool = False
    PER_TRADE_PCT: float = 0.08
    MAX_TRADE_DOLLARS: float = 3.00
    MAX_OPEN_POSITIONS: int = 4
    BALANCE_PCT_PER_TRADE: float = 0.10
    STARTING_BALANCE: float = 10.00
    # 0 = hold to settlement. >0 sells at the bid once the book is that many
    # cents below entry, matching trader._manage_exits, and caps the loss that
    # would otherwise settle at 0. Only usable when minute_quotes are present.
    EXIT_STOP_LOSS_CENTS: int = 0


_cached_markets = None


def init_worker(markets):
    """Pool initializer — load markets once per worker process."""
    global _cached_markets
    _cached_markets = markets


def simulate_trade(market, params, balance=100.0, settlement_yes_won=None):
    """
    Simulate one market through the favorite-maker signal engine.

    Args:
        market: market dict with ticker, close_time, open_time, prices,
                expiration_value, floor_strike, etc.
        params: StratParams
        balance: current available cash
        settlement_yes_won: callable(market) -> bool/None for settlement.
                            Defaults to expiration_value >= floor_strike.

    Returns a result dict or None if no signal.

    ⚠️  LOOKAHEAD BIAS WARNING: When bid/ask data is available, the entry price
    proxy uses bid/ask mid. When only settlement data is available (no orderbook
    history), a fixed synthetic entry price of 45c for YES is used. This is a
    simplifying assumption: it models always entering YES at 45c, which is an
    executable strategy but may not reflect actual entry prices at entry time.
    Results should be treated as indicative, not a prediction of live performance.
    """
    ticker = market.get("ticker", "")
    ct = _parse_api_time(market.get("close_time", ""))
    ot = _parse_api_time(market.get("open_time", ""))
    if not ct or not ot:
        return None  # not a valid 15-min market window

    minutes_in = (ct - ot).total_seconds() / 60
    if minutes_in <= 0 or minutes_in > 15.5:
        return None  # not a valid 15-min market window

    entry_minute = params.ENTRY_WINDOW_START_MIN
    if entry_minute < 0 or entry_minute > 15.0:
        return None

    yb, ya, nb, na, yq, nq = _parse_market_prices(market)

    quotes = market.get("minute_quotes")
    if quotes:
        # Real per-minute book (HF history): enter at the quote available when
        # the entry window opens. Without this the window parameter has no
        # effect on the sweep and every combo scores identically.
        minute = min(quotes, key=lambda m: abs(m - params.ENTRY_WINDOW_START_MIN))
        yb, ya = quotes[minute]

    # Entry price proxy: use bid/ask mid when available; otherwise use a fixed
    # synthetic entry price of 45c for YES (models always entering YES at 45c).
    if yb and ya:
        mid_proxy = (yb + ya) // 2
        entry_source = "book"
    else:
        mid_proxy = 45  # Fixed synthetic entry price for YES when no orderbook
        entry_source = "synthetic"

    if settlement_yes_won is None:
        yes_won = _default_settlement_yes_won(market)
    else:
        yes_won = settlement_yes_won(market)
    if yes_won is None:
        return None

    if params.ENTRY_BAND_MIN <= mid_proxy <= params.ENTRY_BAND_MAX:
        # YES is the favorite in range — buy YES at mid_proxy (maker entry)
        ep = mid_proxy
        side_bullish = True
    else:
        # Price outside favorite band — do not enter. We do NOT bet the
        # underdog (100 - mid_proxy) because that contradicts the
        # favourite-maker strategy: we only buy the favorite side.
        return None

    ep = max(1, min(99, int(ep)))

    if not side_bullish and ep > params.MAX_NO_ENTRY_CENTS:
        return None

    max_dollars = min(params.MAX_TRADE_DOLLARS, balance * params.BALANCE_PCT_PER_TRADE)
    cost = ep / 100.0
    # Fractional contracts. Whole-contract rounding makes a $10 bankroll fall
    # off a cliff: 12% of $10 is $1.20, which buys 2 x 50c contracts (24%) or,
    # at 55c, only 1 (5.5%). Fractional sizing keeps the sweep measuring the
    # sizing rule instead of the granularity of a cent.
    n = max_dollars / cost if cost > 0 else 0.0
    if n < 0.01:
        return None

    dollar_risk = n * ep / 100.0

    entry_price_dollars = ep / 100.0
    exit_price_dollars = (
        1.0 if yes_won else 0.0
    ) if side_bullish else (
        0.0 if yes_won else 1.0
    )

    buy_cost = buy_cash_out(n, entry_price_dollars, taker=False)   # maker: no fee

    # Early stop: if the book trades at or below entry minus the stop, we sell
    # into that bid instead of riding the position to zero at settlement.
    stopped = False
    if quotes and params.EXIT_STOP_LOSS_CENTS > 0:
        limit = ep - params.EXIT_STOP_LOSS_CENTS
        entry_minute_key = min(quotes, key=lambda m: abs(m - params.ENTRY_WINDOW_START_MIN))
        for minute in sorted(quotes):
            if minute <= entry_minute_key:
                continue  # only the entry minute and later
            bid, _ask = quotes[minute]
            if bid and bid <= limit:
                exit_price_dollars = bid / 100.0
                stopped = True
                break

    # The early exit is a maker limit order at the bid, so only the maker fee
    # applies — zero on these markets. Settlement is not a trade either.
    sell_revenue = sell_cash_in(n, exit_price_dollars, taker=False)
    pnl = sell_revenue - buy_cost
    won = pnl > 0

    return {
        "ticker": ticker,
        "direction": "YES" if side_bullish else "NO",
        "entry_price": ep,
        "contracts": n,
        "dollar_risk": dollar_risk,
        "buy_cost": buy_cost,
        "sell_revenue": sell_revenue,
        "settled_yes": 100 if yes_won else 0,
        "stopped": stopped,
        "won": won,
        "pnl": pnl,
        "minutes_in": minutes_in,
        "mid": mid_proxy,
    }


def _default_settlement_yes_won(market):
    """YES wins if expiration_value >= floor_strike."""
    exp_val = market.get("expiration_value")
    floor_strike = market.get("floor_strike")
    if exp_val is None or exp_val == "" or floor_strike is None or floor_strike == "":
        return None
    try:
        return float(exp_val) >= float(floor_strike)
    except (ValueError, TypeError):
        return None


def run_backtest(params, markets, starting_balance=10.0, settlement_yes_won=None):
    """Run the full backtest for one parameter set.

    Markets are sorted by close_time before simulation so that the balance
    evolves in chronological order. This avoids order-dependent results where
    the sequence of trades depends on arbitrary fetch order rather than time.
    """
    balance = starting_balance
    trades = []

    # Sort by close_time so trades are processed in chronological order.
    # Without this, the balance evolution depends on fetch order, making
    # results non-deterministic and drawdown calculations unreliable.
    sorted_markets = sorted(markets, key=lambda m: _parse_api_time(m.get("close_time", "")) or datetime.min)

    for m in sorted_markets:
        result = simulate_trade(m, params, balance, settlement_yes_won)
        if not result:
            continue
        # Tolerance: fractional contracts make dollar_risk land exactly on the
        # cap, and binary float error would otherwise reject a valid trade.
        if result["dollar_risk"] > balance * params.PER_TRADE_PCT + 1e-9:
            continue
        if result["dollar_risk"] > params.MAX_TRADE_DOLLARS + 1e-9:
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
    """Worker function for parallel backtest."""
    combo, starting_balance = args
    params = StratParams(**combo)
    result = run_backtest(params, _cached_markets, starting_balance)
    result["params"] = combo
    return result


def parameter_grid(quick=False):
    """Generate parameter combinations to test."""
    if quick:
        grids = {
            # Only knobs simulate_trade actually reads are swept. MIN_SKEW,
            # MIN_TREND, ENTRY_WINDOW_END and MAX_NO_ENTRY were swept before but
            # never read, so every value scored identically and the "best" rows
            # were tie-breaks. The live trend/skew gates stay untested here.
            "ENTRY_WINDOW_START_MIN": [6.0, 8.0],
            "ENTRY_BAND_MIN": [15, 30, 50],
            "ENTRY_BAND_MAX": [55, 70, 85],
            "AGGRESSIVE_FILL": [False],
            "EXIT_STOP_LOSS_CENTS": [0, 8],
            "PER_TRADE_PCT": [0.05, 0.08, 0.12],
            "MAX_TRADE_DOLLARS": [2.0, 3.0, 5.0],
            "BALANCE_PCT_PER_TRADE": [0.05, 0.10, 0.15],
        }
    else:
        grids = {
            "ENTRY_WINDOW_START_MIN": [4.0, 6.0, 8.0, 10.0],
            "ENTRY_BAND_MIN": [15, 30, 50, 70, 82, 86],
            "ENTRY_BAND_MAX": [55, 70, 85, 88, 91, 95],
            "AGGRESSIVE_FILL": [False, True],
            "EXIT_STOP_LOSS_CENTS": [0, 5, 8, 12],
            "PER_TRADE_PCT": [0.04, 0.08, 0.12, 0.20],
            "MAX_TRADE_DOLLARS": [2.0, 3.0, 5.0, 10.0],
            "BALANCE_PCT_PER_TRADE": [0.05, 0.10, 0.15, 0.25],
        }
    keys = list(grids.keys())
    for combo in itertools.product(*[grids[k] for k in keys]):
        yield dict(zip(keys, combo))


def format_params(p):
    """Format a params dict for display."""
    return (
        f"win={p.get('ENTRY_WINDOW_START_MIN','-')}min "
        f"band={p.get('ENTRY_BAND_MIN','-')}-{p.get('ENTRY_BAND_MAX','-')} "
        f"stop={p.get('EXIT_STOP_LOSS_CENTS','-')}c "
        f"agg={p.get('AGGRESSIVE_FILL','-')}"
    )


def print_results(results, n_markets, n_combos, top=10):
    """Print ranked backtest results to stdout."""
    # Rank by return per unit of drawdown. Sorting on EV alone chose the
    # highest-DD parameter set (57% drawdown), which is not a strategy we can
    # run. /0.01 keeps a zero-drawdown set from dividing by zero.
    results.sort(key=lambda r: r["total_pnl"] / (r["max_drawdown"] + 0.01), reverse=True)

    print("\n" + "=" * 90)
    print(f"BACKTEST RESULTS — {n_markets} markets, {n_combos} parameter sets")
    print("=" * 90)
    print(f"\n{'Rank':<5} {'EV/trade':<9} {'Total P&L':<10} {'Trades':<7} "
          f"{'Win%':<7} {'Avg P&L':<9} {'MaxDD':<7} {'Params'}")
    print("-" * 90)
    for i, r in enumerate(results[:top]):
        p = r["params"]
        print(f"{i+1:<5} {r['ev']:<9.4f} {r['total_pnl']:<10.2f} {r['n_trades']:<7} "
              f"{r['win_rate']*100:<6.1f}% {r['avg_pnl']:<9.4f} {r['max_drawdown']*100:<6.1f}% "
              f"{format_params(p)}")


def save_results(results, n_markets, n_combos, out_path):
    """Save top results to a JSON file."""
    out = {
        "n_markets": n_markets,
        "n_combos": n_combos,
        "results": [
            {**{"params": r["params"]}, **{k: v for k, v in r.items() if k != "trades"}}
            for r in results[:50]
        ],
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    log.info("Full results saved to %s", out_path)


def print_best(results):
    """Print best-parameter detail."""
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


def run_sweep(markets, settlement_yes_won=None, quick=False, limit=None,
              top=10, jobs=4, out_path="backtest_results.json",
              starting_balance=10.0):
    """Run a full parameter sweep and print/save results.

    Args:
        markets: list of market dicts (already fetched).
        settlement_yes_won: callable(market) -> bool/None.
        quick: use smaller parameter grid.
        limit: max markets used (truncates list).
        top: how many results to display.
        jobs: parallel workers.
        out_path: JSON output path.
        starting_balance: cash per backtest run.

    ⚠️  MEMORY WARNING: `Pool` pickles the full `markets` list into every worker
    process, so memory scales as `jobs × markets_size`. On large datasets this
    can OOM small hosts (e.g. termux). `jobs` is capped at CPU count; for very
    large datasets reduce `--jobs` further or pre-filter markets with `--limit`.

    Returns sorted results list.
    """
    if limit is not None:
        markets = markets[:limit]

    combos = list(parameter_grid(quick))
    # Cap workers at CPU count: each worker gets a full copy of `markets`
    # via pickle, so more workers = more memory. For large datasets this
    # can OOM; prefer fewer workers over more.
    jobs = min(jobs, os.cpu_count() or 4)
    log.info("Testing %d parameter combinations with %d workers...", len(combos), jobs)

    work_items = [(c, starting_balance) for c in combos]

    with Pool(processes=jobs, initializer=init_worker, initargs=(markets,)) as pool:
        results = pool.map(_backtest_one, work_items)

    print_results(results, len(markets), len(combos), top=top)
    save_results(results, len(markets), len(combos), out_path)
    print_best(results)
    return results
