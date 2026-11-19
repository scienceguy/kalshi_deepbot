"""Bankroll compounding simulation: $35 -> $1,000,000 on Polymarket US 15-min favorites.

ILLUSTRATIVE, ASSUMPTION-DRIVEN — NOT A FORECAST.

Every number below rests on assumptions that are usually printed next to it.
Nothing here is a prediction about any market; the measured inputs are two
local ledgers (--ledger), everything else is an assumption documented in
DEFAULT_WIN_RATES / BANDS.

Models trade-by-trade compounding under the bot's strict risk limits:
  * Position size capped at 3% of balance per trade (min 1 contract).
  * 30% cash reserve floor -> max 70% deployed -> max floor(70/3) = 23
    concurrent 3% rungs.
  * High-certainty favorites priced 92c-99c (BANDS), per-band win rate from
    measured outcomes when available (Wilson 90% lower confidence bound),
    otherwise the documented DEFAULT_WIN_RATES assumptions.
  * Taker fee charged at entry (A4 model, theta=0.095): fee = theta*q*(1-q).
    Maker fills pay no fee; this sim models taker entries only.
  * Slippage of one tick ($0.01) above the ask.
  * A common-factor shock: every CONCURRENT_RUNGS consecutive entries form
    one "concurrent window"; with SHOCK_PROB probability that window loses
    all its rungs together (same-asset tail event).
  * Size capped by LIQUIDITY_CONTRACTS per trade, not only by balance.

Compares 3% fixed-fraction sizing vs Half-Kelly sizing, with Monte Carlo
estimates of trades/time to each milestone, risk of ruin, and max drawdown.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

START = 35.0
MILESTONES = [100.0, 1_000.0, 10_000.0, 100_000.0, 1_000_000.0]
BANDS = ((0.92, 0.95), (0.95, 0.97), (0.97, 0.99))   # favorite price bands (lo, hi)
# (planning win rate, measured n). n == 0 -> documented assumption, not data.
# Ordered to rise with band price: a 92c favorite needs >~95% just to break even.
DEFAULT_WIN_RATES = {0.92: (0.94, 0), 0.95: (0.97, 0), 0.97: (0.985, 0)}
PAYOUT = 1.00
FRACTION_3PCT = 0.03
CASH_FLOOR = 0.30                  # reserve -> 0.70 deployable
RUIN_BALANCE = BANDS[0][0] / (1.0 - CASH_FLOOR)
N_RUNS = 8_000
SEED = 20261114
MAX_TRADES = 200_000
SLIPPAGE_DOLLARS = 0.01            # one tick above the ask
FEE_THETA = 0.095                  # A4 taker fee theta (Tier 1), fee = theta*q*(1-q)
LIQUIDITY_CONTRACTS = 100          # assumed max contracts available per trade
CONCURRENT_RUNGS = 5               # concurrent positions per window (A5)
SHOCK_PROB = 0.01                  # P(common-factor shock per window)
LCB_Z = 1.2816                     # z for a 90% one-sided lower confidence bound
MIN_OBS_LCB = 30                   # measured samples required before LCB replaces assumptions
CHUNK = 40_000                     # safety cap on precomputed draws per chunk

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEDGERS = [REPO_ROOT / "local_data" / "poly_us_paper_ledger.jsonl",
                   REPO_ROOT / "local_data" / "bs_calibration_ledger.jsonl"]


def fee_per_contract(price: float) -> float:
    """A4 taker fee per contract: theta * q * (1 - q)."""
    return FEE_THETA * price * (1.0 - price)


def breakeven_win_rate(ask: float) -> float:
    """Win rate at which a taker entry at `ask` breaks even, after slippage and fee."""
    entry = ask + SLIPPAGE_DOLLARS
    return entry + fee_per_contract(entry)


def band_lo(price: float) -> float:
    """Lower edge of the band containing `price` (half-open [lo, hi))."""
    for lo, hi in BANDS:
        if price < hi:
            return lo
    return BANDS[-1][0]


def wilson_lower_bound(wins: float, n: float) -> float:
    """Wilson lower confidence bound on a binomial win rate (one-sided, LCB_Z)."""
    if n <= 0:
        return 0.0
    z = LCB_Z
    p_hat = wins / n
    denom = 1.0 + z * z / n
    center = (p_hat + z * z / (2.0 * n)) / denom
    margin = z * math.sqrt(p_hat * (1.0 - p_hat) / n + z * z / (4.0 * n * n)) / denom
    return center - margin


def ledger_observations(paths) -> dict:
    """Collect per-band win counts from the ledgers (A12).

    poly_us_paper_ledger.jsonl: settlement events; a win is pnl > 0.
    bs_calibration_ledger.jsonl: outcome >= 0.5 counts as the YES side winning;
    for side=no the row wins when outcome < 0.5. Rows with a null outcome are
    skipped. Returns {band_lo: [wins, n]} for every band.
    """
    obs = {lo: [0, 0] for lo, _ in BANDS}
    for path in paths:
        try:
            with open(path) as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry = row.get("entry_price", row.get("price"))
            if entry is None:
                continue
            try:
                lo = band_lo(float(entry))
            except (TypeError, ValueError):
                continue
            if "outcome" in row:  # calibration ledger
                outcome = row.get("outcome")
                if outcome is None:
                    continue
                side = str(row.get("side", "")).lower()
                yes_wins = float(outcome) >= 0.5
                win = yes_wins if side == "yes" else (not yes_wins) if side == "no" else None
                if win is None:
                    continue
            elif row.get("event") == "settlement":  # paper ledger
                try:
                    win = float(row.get("pnl") or 0.0) > 0.0
                except (TypeError, ValueError):
                    continue
            else:
                continue
            obs[lo][0] += int(win)
            obs[lo][1] += 1
    return obs


def planning_win_rates(obs: dict | None) -> dict:
    """Per-band planning win rate: Wilson LCB when n >= MIN_OBS_LCB, else the assumption.

    Returns {band_lo: (p, n)}. n == 0 means the value is an assumption.
    """
    obs = obs or {}
    rates = {}
    for lo, _ in BANDS:
        w, n = obs.get(lo, [0, 0])
        if n >= MIN_OBS_LCB:
            rates[lo] = (wilson_lower_bound(w, n), n)
        else:
            rates[lo] = DEFAULT_WIN_RATES[lo]
    return rates


def _trade_wins(rng: np.random.Generator, ps, concurrent: int = CONCURRENT_RUNGS,
                shock_prob: float = SHOCK_PROB) -> np.ndarray:
    """Boolean win array; every `concurrent` consecutive entries share one shock draw.

    A shock sets the whole window to losses (same-asset tail event, A5).
    """
    ps = list(ps)
    n = len(ps)
    wins = np.empty(n, dtype=bool)
    step = max(1, int(concurrent))
    for start in range(0, n, step):
        batch = ps[start:start + step]
        if shock_prob > 0.0 and rng.random() < shock_prob:
            wins[start:start + len(batch)] = False
            continue
        wins[start:start + len(batch)] = rng.random(len(batch)) < np.array(batch)
    return wins


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
    max_contracts: int


def _p_of(win_rates, lo) -> float:
    v = (win_rates or {}).get(lo, DEFAULT_WIN_RATES[lo])
    return float(v[0]) if isinstance(v, (tuple, list)) else float(v)


def run_one(rng: np.random.Generator, fraction: float, start: float = START,
            win_rates: dict | None = None, shock_prob: float = SHOCK_PROB,
            concurrent: int = CONCURRENT_RUNGS, max_trades: int = MAX_TRADES,
            liquidity: int | None = None) -> RunResult:
    liq = int(liquidity) if liquidity is not None else LIQUIDITY_CONTRACTS
    rates = {lo: _p_of(win_rates, lo) for lo, _ in BANDS}
    bal = start
    peak = start
    max_dd = 0.0
    n_trades = 0
    max_contracts = 0
    hits = {m: None for m in MILESTONES}
    ruined = stuck = False

    while n_trades < max_trades and not (ruined or stuck) and any(h is None for h in hits.values()):
        batch = min(concurrent, max_trades - n_trades)
        band_idx = rng.integers(0, len(BANDS), size=batch)
        qs = np.array([BANDS[i][0] + (BANDS[i][1] - BANDS[i][0]) * rng.random() for i in band_idx])
        ps = np.array([rates[BANDS[i][0]] for i in band_idx])
        wins = _trade_wins(rng, ps, concurrent=concurrent, shock_prob=shock_prob)
        for i in range(batch):
            q = float(qs[i])
            entry = q + SLIPPAGE_DOLLARS
            fee = fee_per_contract(entry)
            size = fraction * bal
            if size < entry + fee:            # cannot fund even 1 contract
                stuck = True
                break
            contracts = min(int(size // (entry + fee)), liq)
            if contracts < 1:
                stuck = True
                break
            max_contracts = max(max_contracts, contracts)
            cost = contracts * entry + contracts * fee
            if wins[i]:
                bal += contracts * (PAYOUT - entry - fee)
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
            for m in MILESTONES:
                if hits[m] is None and bal >= m:
                    hits[m] = n_trades

    for m in MILESTONES:                      # final milestone check at loop exit
        if hits[m] is None and bal >= m:
            hits[m] = n_trades

    return RunResult(
        trades=n_trades,
        ruined=ruined,
        stuck=stuck,
        final_balance=bal,
        max_drawdown=max_dd,
        hits=hits,
        max_contracts=max_contracts,
    )


def mc_stats(fraction: float, n_runs: int = N_RUNS, win_rates: dict | None = None,
             shock_prob: float = SHOCK_PROB, seed: int = SEED):
    rng = np.random.default_rng(seed)
    results = [run_one(rng, fraction, win_rates=win_rates,
                       shock_prob=shock_prob) for _ in range(n_runs)]

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


def representative_path(fraction: float, win_rates: dict | None = None,
                        shock_prob: float = SHOCK_PROB, n_show: int = 40, seed: int = 99):
    import random
    rng = random.Random(seed)
    rates = {lo: _p_of(win_rates, lo) for lo, _ in BANDS}
    bal = START
    rows = []
    peak = START
    max_dd = 0.0
    t = 0
    n_con = max(1, CONCURRENT_RUNGS)
    pending_shock = False
    while t < MAX_TRADES:
        if t % n_con == 0:
            pending_shock = rng.random() < shock_prob
        i = int(rng.randrange(len(BANDS)))
        q = BANDS[i][0] + (BANDS[i][1] - BANDS[i][0]) * rng.random()
        entry = q + SLIPPAGE_DOLLARS
        fee = fee_per_contract(entry)
        size = fraction * bal
        contracts = min(int(size // (entry + fee)), LIQUIDITY_CONTRACTS)
        if contracts < 1:
            break
        win = (not pending_shock) and rng.random() < rates[BANDS[i][0]]
        cost = contracts * entry + contracts * fee
        pnl = contracts * (PAYOUT - entry - fee) if win else -cost
        bal += pnl
        t += 1
        if bal > peak:
            peak = bal
        dd = (peak - bal) / peak
        max_dd = max(max_dd, dd)
        if t <= n_show:
            rows.append((t, q, contracts, "W" if win else "L", pnl, bal, dd))
    return rows, t, bal, max_dd


def _print_band_table(rates: dict, ledgers_used) -> None:
    print("PER-BAND ASSUMPTIONS vs BREAK-EVEN  (planning p = Wilson 90% LCB when n >= 30, else assumption)")
    print(f"{'Band':<12} {'assumed p':>10} {'planning p':>11} {'measured n':>11} {'break-even p':>13}")
    for lo, hi in BANDS:
        assumed = DEFAULT_WIN_RATES[lo][0]
        p, n = rates[lo]
        mid = (lo + hi) / 2.0
        source = "measured LCB" if n >= MIN_OBS_LCB else "assumption"
        print(f"{f'{lo:.2f}-{hi:.2f}':<12} {assumed*100:>9.1f}% {p*100:>10.1f}% {n:>11} {breakeven_win_rate(mid)*100:>12.1f}%"
              f"   ({source})")
    print("Break-even is measured at the band mid, assuming entry at ask + 1 tick, after fees.")
    if ledgers_used:
        print("Ledger observations from: " + ", ".join(str(p) for p in ledgers_used))
    else:
        print("No ledger data used — planning p is the DEFAULT_WIN_RATES assumption, not a measurement.")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Bankroll compounding sim (illustrative, not a forecast)")
    ap.add_argument("--ledger", nargs="*", default=None, metavar="PATH",
                    help="read measured outcomes from these JSONL ledgers (default when the flag is "
                         "given without paths: local_data/poly_us_paper_ledger.jsonl and "
                         "local_data/bs_calibration_ledger.jsonl)")
    ap.add_argument("--runs", type=int, default=N_RUNS)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    ledgers = None
    if args.ledger is not None:
        ledgers = [Path(p) for p in args.ledger] if args.ledger else DEFAULT_LEDGERS
    obs = ledger_observations(ledgers) if ledgers else None
    rates = planning_win_rates(obs)

    print("=" * 78)
    print("BANKROLL COMPOUNDING MODEL: $35 -> $1,000,000  (Polymarket US favorites)")
    print("ILLUSTRATIVE, ASSUMPTION-DRIVEN — NOT A FORECAST")
    print("=" * 78)
    print(f"Start balance:           ${START:,.2f}")
    print("Bands (lo-hi):           " + ", ".join(f"{lo:.2f}-{hi:.2f}" for lo, hi in BANDS))
    print(f"Payout:                  ${PAYOUT:.2f} per contract")
    print(f"Entry:                   ask + ${SLIPPAGE_DOLLARS:.2f} slippage; taker fee theta={FEE_THETA} per contract")
    print(f"Common-factor shock:     p={SHOCK_PROB:.2f} per {CONCURRENT_RUNGS}-rung window (all rungs lose together)")
    print(f"Liquidity cap:           {LIQUIDITY_CONTRACTS} contracts per trade")
    print(f"Position cap:            {FRACTION_3PCT*100:.0f}% of balance per trade (min 1 contract)")
    print(f"Cash reserve floor:      {CASH_FLOOR*100:.0f}% -> {100*(1-CASH_FLOOR):.0f}% deployable")
    print(f"Max concurrent 3% rungs: {int((1.0 - CASH_FLOOR) / FRACTION_3PCT)}  (20-25 rung assumption fits only at <=23)")
    print()
    _print_band_table(rates, ledgers)

    q_mid = (BANDS[0][0] + BANDS[-1][1]) / 2.0
    print("Kelly math at band mid q={:.3f} (no fees):".format(q_mid))
    for lo, hi in BANDS:
        p, n = rates[lo]
        mid = (lo + hi) / 2.0
        f_full = kelly_fraction(p, mid)
        print(f"  band {lo:.2f}-{hi:.2f}: planning p={p*100:.1f}% -> full Kelly {f_full*100:.1f}%, half-Kelly {f_full*50:.1f}%")
    print()

    f3 = FRACTION_3PCT
    f_half = max(1e-6, kelly_fraction(rates[BANDS[len(BANDS) // 2][0]][0], q_mid) / 2.0)
    max_concurrent_3pct = int((1.0 - CASH_FLOOR) / f3)
    max_concurrent_half = int((1.0 - CASH_FLOOR) / f_half) if f_half > 0 else 0

    print("-" * 78)
    print(f"TRADE-BY-TRADE COMPOUNDING TRAJECTORY (3% fixed-fraction, Monte Carlo {args.runs} runs)")
    print("-" * 78)
    s3 = mc_stats(f3, n_runs=args.runs, win_rates=rates, shock_prob=SHOCK_PROB, seed=args.seed)
    sh = mc_stats(f_half, n_runs=args.runs, win_rates=rates, shock_prob=SHOCK_PROB, seed=args.seed)
    print(f"{'Milestone':<14} {'Target':>10} {'Trades (med)':>14} {'p10':>8} {'p90':>8} {'P(reach)':>9}")
    for m in MILESTONES:
        st = s3["hit_stats"][m]
        print(f"{'-> $' + f'{m:,.0f}':<14} ${m:>9,.0f} {st['trades_median']:>14,.0f} "
              f"{st['trades_p10']:>8,.0f} {st['trades_p90']:>8,.0f} {st['p_hit']:>8.4f}")

    print()
    print("TIME TO MILESTONES  (rungs per window; window = 15 min)")
    print(f"{'Milestone':<14} {'@20 rung/15m':>16} {'@22 rung/15m':>16} {'@25 rung/15m':>16} {'@22 rung/hr':>14}")
    for m in MILESTONES:
        tr = s3["hit_stats"][m]["trades_median"]
        row = f"{'-> $' + f'{m:,.0f}':<14}"
        for rungs, wmin in [(20, 15), (22, 15), (25, 15), (22, 60)]:
            row += f" {hours(tr, rungs, wmin)/24:>15.2f}"
        print(row)

    print()
    print("EXAMPLE TRADE-BY-TRADE PATH (first 40 trades, 3% sizing, seed 99)")
    print(f"{'T':>3} {'Entry':>7} {'Contr':>6} {'W/L':>4} {'PnL':>9} {'Balance':>12} {'Drawdown':>10}")
    rows, t_reached, bal_end, path_dd = representative_path(f3, win_rates=rates, shock_prob=SHOCK_PROB)
    for t, q, c, wl, pnl, bal, dd in rows:
        print(f"{t:>3} {q + SLIPPAGE_DOLLARS:>7.3f} {c:>6} {wl:>4} {pnl:>9.2f} ${bal:>10,.2f} {dd*100:>8.2f}%")
    print(f"... ({len(rows)} trades shown; median run needs ~{s3['hit_stats'][1_000_000]['trades_median']:,.0f} "
          f"trades to reach $1,000,000 — if it ever does.)")

    print()
    print("-" * 78)
    print(f"RISK OF RUIN & MAX DRAWDOWN  (Monte Carlo, {args.runs} runs)")
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
    print("WHAT THIS DOES AND DOES NOT SHOW")
    print("-" * 78)
    print("1. With a constant 98% win rate and no fees, the 99c band always looked "
          "free money; it is not — its break-even is above its own price only after "
          "measured edge exceeds fee + slippage, which the ledgers must demonstrate.")
    print("2. This run used planning p from: "
          + ("measured ledgers (Wilson 90% LCB where n >= 30)" if obs else "DEFAULT_WIN_RATES assumptions (no --ledger)"))
    print("3. Compounding from $35 at 3% sizing means one integer contract; the model "
          "stops as 'stuck' when it cannot fund a single contract.")
    print("4. The common-factor shock is the dominant ruin channel at low balances: "
          f"p={SHOCK_PROB} per {CONCURRENT_RUNGS}-rung window.")
    print("5. ILLUSTRATIVE, ASSUMPTION-DRIVEN — NOT A FORECAST. Re-check every assumption "
          "against local_data ledgers before sizing anything on this.")

    out = {
        "label": "ILLUSTRATIVE, ASSUMPTION-DRIVEN — NOT A FORECAST",
        "bands": [list(b) for b in BANDS],
        "planning_win_rates": {str(lo): list(rates[lo]) for lo, _ in BANDS},
        "measured_observations": obs,
        "kelly": {"full_mid_band": kelly_fraction(rates[BANDS[len(BANDS) // 2][0]][0], q_mid),
                  "half_mid_band": f_half},
        "max_concurrent": {"pct3": max_concurrent_3pct, "half_kelly": max_concurrent_half},
        "stats_3pct": s3,
        "stats_half_kelly": sh,
    }
    out_path = REPO_ROOT / "scripts" / "bankroll_compounding_results.json"
    with open(out_path, "w") as fh:
        json.dump(out, fh, indent=2, default=float)
    print()
    print(f"Raw results written to {out_path}")


if __name__ == "__main__":
    main()
