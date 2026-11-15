"""Bankroll compounding simulation: $35 -> $1,000,000 on Kalshi 15-min binary favorites.

Models trade-by-trade compounding under the bot's strict risk limits:
  * Position size capped at 3% of total balance per trade (min 1 contract).
  * 30% cash reserve floor at all times -> max 70% deployed -> max
    floor(70/3) = 23 concurrent 3% rungs (the "20-25 active rungs" assumption
    is therefore feasible only at <= 23 rungs).
  * High-certainty favorites priced 92c-99c, win rate 97-99%, $1.00 payout
    per contract (buy favorite at q, win pays $1.00).

Compares 3% fixed-fraction sizing vs Half-Kelly sizing, with Monte Carlo
estimates of trades/time to each milestone, risk of ruin, and max drawdown.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass

import numpy as np

START = 35.0
MILESTONES = [100.0, 1_000.0, 10_000.0, 100_000.0, 1_000_000.0]
Q_LO, Q_HI = 0.92, 0.99            # favorite price band ($ per contract)
P_WIN = 0.98                       # average win rate (97-99%)
PAYOUT = 1.00
FRACTION_3PCT = 0.03
CASH_FLOOR = 0.30                  # reserve -> 0.70 deployable
RUIN_BALANCE = Q_LO / (1.0 - CASH_FLOOR)  # ~$1.31: can't fund 1 contract inside reserve
N_RUNS = 8_000
SEED = 20261114
MAX_TRADES = 200_000               # safety cap per run
CHUNK = 40_000                     # precompute this many random draws per run


def kelly_fraction(p: float, q: float) -> float:
    """Continuous Kelly fraction for a bet risking q to win (1-q)."""
    b = (PAYOUT - q) / q           # net $ won per $1 wagered
    f = (p * b - (1.0 - p)) / b
    return max(0.0, min(f, 1.0))


@dataclass
class RunResult:
    trades: int
    ruined: bool
    stuck: bool
    final_balance: float
    max_drawdown: float
    hits: dict                  # milestone -> trade count at crossing (None if never)


def run_one(rng: np.random.Generator, fraction: float, start: float = START) -> RunResult:
    bal = start
    peak = start
    max_dd = 0.0
    n_trades = 0
    hits = {m: None for m in MILESTONES}
    ruined = stuck = False
    deployable_frac = 1.0 - CASH_FLOOR
    milestones = MILESTONES

    while n_trades < MAX_TRADES and not (ruined or stuck) and any(h is None for h in hits.values()):
        take = min(CHUNK, MAX_TRADES - n_trades)
        qs = Q_LO + (Q_HI - Q_LO) * rng.random(take)
        wins = rng.random(take) < P_WIN
        for i in range(take):
            # record milestone crossings at the current balance
            for m in milestones:
                if hits[m] is None and bal >= m:
                    hits[m] = n_trades
            if all(h is not None for h in hits.values()):
                break

            q = qs[i]
            size = fraction * bal
            if size < q:                      # cannot fund even 1 contract
                stuck = True
                break
            # contracts*q <= size = fraction*bal <= deployable_frac*bal
            # (fraction <= 0.70 by construction), so the cash floor never binds
            # on a sequentially-settled single trade.
            contracts = int(size // q)
            cost = contracts * q
            if wins[i]:
                bal += contracts * (PAYOUT - q)
            else:
                bal -= cost
            n_trades += 1

            if bal > peak:
                peak = bal
            dd = (peak - bal) / peak
            if dd > max_dd:
                max_dd = dd
            if bal < RUIN_BALANCE:
                ruined = True
                break

    # final milestone check at loop exit
    for m in milestones:
        if hits[m] is None and bal >= m:
            hits[m] = n_trades

    return RunResult(
        trades=n_trades,
        ruined=ruined,
        stuck=stuck,
        final_balance=bal,
        max_drawdown=max_dd,
        hits=hits,
    )


def mc_stats(fraction: float, n_runs: int = N_RUNS):
    rng = np.random.default_rng(SEED)
    results = [run_one(rng, fraction) for _ in range(n_runs)]

    def pct(values, q):
        return float(np.percentile(values, q)) if values else float("nan")

    traded_counts = [r.trades for r in results if r.trades > 0]
    ruin_flag = [r.ruined or r.stuck for r in results]
    dd = [r.max_drawdown for r in results]

    hit_stats = {}
    for m in MILESTONES:
        hits = [r.hits[m] for r in results if r.hits[m] is not None]
        hit_stats[m] = {
            "n_hit": len(hits),
            "p_hit": len(hits) / n_runs,
            "trades_median": pct(hits, 50),
            "trades_p10": pct(hits, 10),
            "trades_p90": pct(hits, 90),
            "trades_max": max(hits) if hits else None,
        }

    return {
        "fraction": fraction,
        "n_runs": n_runs,
        "ruin_rate": sum(ruin_flag) / n_runs,
        "n_ruined": sum(1 for r in results if r.ruined),
        "n_stuck": sum(1 for r in results if r.stuck),
        "median_trades_to_1M": pct(traded_counts, 50),
        "max_drawdown": {
            "median": pct(dd, 50),
            "p90": pct(dd, 90),
            "p99": pct(dd, 99),
            "max_observed": max(dd),
            "p_gt_10pct": sum(1 for x in dd if x > 0.10) / n_runs,
            "p_gt_30pct": sum(1 for x in dd if x > 0.30) / n_runs,
            "p_gt_50pct": sum(1 for x in dd if x > 0.50) / n_runs,
            "p_gt_80pct": sum(1 for x in dd if x > 0.80) / n_runs,
        },
        "hit_stats": hit_stats,
    }


def hours(trades: int, rungs_per_window: int, window_min: int = 15) -> float:
    trades_per_hour = rungs_per_window * (60.0 / window_min)
    return trades / trades_per_hour if trades_per_hour else float("inf")


def representative_path(fraction: float, n_show: int = 40):
    import random
    rng = random.Random(99)
    bal = START
    rows = []
    peak = START
    max_dd = 0.0
    t = 0
    while t < 200_000:
        q = round(Q_LO + (Q_HI - Q_LO) * rng.random(), 3)
        size = fraction * bal
        contracts = int(size // q)
        if contracts < 1:
            break
        win = rng.random() < P_WIN
        cost = contracts * q
        pnl = contracts * (PAYOUT - q) if win else -cost
        bal += pnl
        t += 1
        if bal > peak:
            peak = bal
        dd = (peak - bal) / peak
        max_dd = max(max_dd, dd)
        if t <= n_show:
            rows.append((t, q, contracts, "W" if win else "L", pnl, bal, dd))
    return rows, t, bal, max_dd


def main() -> None:
    f3 = FRACTION_3PCT
    q_mid = (Q_LO + Q_HI) / 2.0
    f_kelly = kelly_fraction(P_WIN, q_mid)
    f_half = f_kelly / 2.0
    max_concurrent_3pct = int((1.0 - CASH_FLOOR) / f3)          # 23
    max_concurrent_half = int((1.0 - CASH_FLOOR) / f_half) if f_half > 0 else 0  # 2

    print("=" * 78)
    print("BANKROLL COMPOUNDING MODEL: $35 -> $1,000,000  (Kalshi 15-min favorites)")
    print("=" * 78)
    print(f"Start balance:           ${START:,.2f}")
    print(f"Favorite price band:     {Q_LO*100:.0f}c - {Q_HI*100:.0f}c (mid {q_mid*100:.1f}c)")
    print(f"Win rate / payout:       {P_WIN*100:.0f}% / ${PAYOUT:.2f} per contract")
    print(f"Position cap:            {f3*100:.0f}% of balance per trade (min 1 contract)")
    print(f"Cash reserve floor:      {CASH_FLOOR*100:.0f}% -> {100*(1-CASH_FLOOR):.0f}% deployable")
    print(f"Max concurrent 3% rungs: {max_concurrent_3pct}  (20-25 rung assumption fits only at <=23)")
    print()
    print("Kelly math (p=0.98, q=$0.955): net odds b = (1-q)/q = {:.4f}".format(
        (PAYOUT - q_mid) / q_mid))
    print(f"Full Kelly fraction:     {f_kelly*100:.1f}% of balance per trade")
    print(f"Half-Kelly fraction:     {f_half*100:.1f}% of balance per trade")
    print(f"Max concurrent Half-Kelly rungs under 30% floor: {max_concurrent_half}")
    print()

    s3 = mc_stats(f3)
    sh = mc_stats(f_half)

    print("-" * 78)
    print("TRADE-BY-TRADE COMPOUNDING TRAJECTORY (3% fixed-fraction, median run)")
    print("-" * 78)
    print(f"{'Milestone':<14} {'Target':>10} {'Trades (med)':>14} {'p10':>8} {'p90':>8} {'P(reach)':>9}")
    for m in MILESTONES:
        st = s3["hit_stats"][m]
        print(f"{'-> $' + f'{m:,.0f}':<14} ${m:>9,.0f} {st['trades_median']:>14,.0f} "
              f"{st['trades_p10']:>8,.0f} {st['trades_p90']:>8,.0f} {st['p_hit']:>8.4f}")

    print()
    print("TIME TO MILESTONES  (rungs per window; window = 15 min unless noted)")
    print(f"{'Milestone':<14} {'@20 rung/15m':>16} {'@22 rung/15m':>16} {'@25 rung/15m':>16} {'@22 rung/hr':>14}")
    print(f"{'':<14} {'(days)':>16} {'(days)':>16} {'(days)':>16} {'(days)':>14}")
    for m in MILESTONES:
        tr = s3["hit_stats"][m]["trades_median"]
        row = f"{'-> $' + f'{m:,.0f}':<14}"
        for rungs, wmin in [(20, 15), (22, 15), (25, 15), (22, 60)]:
            row += f" {hours(tr, rungs, wmin)/24:>15.2f}"
        print(row)

    print()
    print("EXAMPLE TRADE-BY-TRADE PATH (first 40 trades, 3% sizing, seed 99)")
    print(f"{'T':>3} {'Price':>7} {'Contr':>6} {'W/L':>4} {'PnL':>9} {'Balance':>12} {'Drawdown':>10}")
    rows, t_reached, bal_end, path_dd = representative_path(f3)
    for t, q, c, wl, pnl, bal, dd in rows:
        print(f"{t:>3} {q:>7.3f} {c:>6} {wl:>4} {pnl:>9.2f} ${bal:>10,.2f} {dd*100:>8.2f}%")
    print(f"... ({len(rows)} trades shown; median run needs ~{s3['hit_stats'][1_000_000]['trades_median']:,.0f} "
          f"trades to reach $1,000,000.)")

    print()
    print("-" * 78)
    print("RISK OF RUIN & MAX DRAWDOWN  (Monte Carlo, {} runs)".format(N_RUNS))
    print("-" * 78)
    for label, st in [("3% fixed-fraction", s3), ("Half-Kelly ({:.1f}%)".format(f_half * 100), sh)]:
        print(f"\n{label}:")
        print(f"  Ruin/stuck rate:          {st['ruin_rate']*100:.4f}%  "
              f"(ruined={st['n_ruined']}, stuck={st['n_stuck']} of {st['n_runs']})")
        dd = st["max_drawdown"]
        print(f"  Median max drawdown:      {dd['median']*100:.2f}%")
        print(f"  p90 / p99 max drawdown:   {dd['p90']*100:.2f}% / {dd['p99']*100:.2f}%")
        print(f"  Worst observed drawdown:  {dd['max_observed']*100:.2f}%")
        print(f"  P(DD > 10%): {dd['p_gt_10pct']*100:.2f}%   "
              f"P(DD > 30%): {dd['p_gt_30pct']*100:.2f}%   "
              f"P(DD > 50%): {dd['p_gt_50pct']*100:.2f}%   "
              f"P(DD > 80%): {dd['p_gt_80pct']*100:.2f}%")

    print()
    print("-" * 78)
    print("WALL-CLOCK REALITY CHECK (Half-Kelly concurrency capped by 30% floor)")
    print("-" * 78)
    tr_3m = s3["hit_stats"][1_000_000]["trades_median"]
    tr_hk = sh["hit_stats"][1_000_000]["trades_median"]
    print(f"Median trades to $1M:  3% sizing = {tr_3m:,.0f}   Half-Kelly = {tr_hk:,.0f} "
          f"({tr_3m/tr_hk:.1f}x fewer trades for Half-Kelly)")
    print(f"  3% sizing at 22 rungs/15m:  {hours(tr_3m, 22)/24:.1f} days  ({hours(tr_3m, 22)/24/30:.2f} months)")
    print(f"  Half-Kelly at {max_concurrent_half} rungs/15m (floor cap): "
          f"{hours(tr_hk, max_concurrent_half)/24:.1f} days  ({hours(tr_hk, max_concurrent_half)/24/30:.2f} months)")
    print(f"  Half-Kelly risks {f_half*100:.0f}% of balance per trade but may run only "
          f"{max_concurrent_half} concurrent rungs vs {max_concurrent_3pct} -> no wall-clock advantage.")

    print()
    print("-" * 78)
    print("ACTIONABLE FINDINGS")
    print("-" * 78)
    print("1. Edge per trade is thin: ~$0.02/contract at q=0.96, p=0.98 -> "
          f"~0.06% of balance per trade at 3% sizing.")
    print(f"2. Median {s3['median_trades_to_1M']:,.0f} trades to $1M from $35 (~{hours(s3['median_trades_to_1M'], 22)/24:.0f} days at "
          f"22 rungs per 15-min window); reaching the first $100 alone takes "
          f"~{s3['hit_stats'][100]['trades_median']:,.0f} trades.")
    print(f"3. Risk of ruin at 3% sizing: {s3['ruin_rate']*100:.4f}% in {N_RUNS:,} runs "
          f"(effectively zero over the journey) since drawdowns compound at only 3%/trade.")
    print(f"4. Half-Kelly ({f_half*100:.0f}%/trade) needs {tr_hk:,.0f} trades to $1M (~{tr_3m/tr_hk:.0f}x fewer), but the "
          f"30% cash floor caps it at {max_concurrent_half} concurrent rungs, erasing the wall-clock "
          f"advantage; max drawdown is far worse (P(DD>50%) = {sh['max_drawdown']['p_gt_50pct']*100:.2f}% vs "
          f"{s3['max_drawdown']['p_gt_50pct']*100:.2f}% at 3%).")
    print("5. Binding constraint is flow, not sizing: the bot must source 20-23 qualifying "
          "92-99c favorites every 15 minutes; at low balances per-trade PnL is cents.")
    print("6. Drawdown risk sits in losing runs: 4 straight losses (p = 0.02^4) ~ an 11% "
          "drawdown at 3% sizing; 3 straight losses ~ a 63% drawdown at Half-Kelly.")

    out = {
        "kelly": {"full": f_kelly, "half": f_half},
        "max_concurrent": {"pct3": max_concurrent_3pct, "half_kelly": max_concurrent_half},
        "stats_3pct": s3,
        "stats_half_kelly": sh,
    }
    with open("scripts/bankroll_compounding_results.json", "w") as fh:
        json.dump(out, fh, indent=2, default=float)
    print()
    print("Raw results written to scripts/bankroll_compounding_results.json")


if __name__ == "__main__":
    main()
