#!/usr/bin/env python3
"""Parallel config sweep on the real HF Kalshi 15m quote_history.

Every trade is a real per-minute quote from papylove/kalshi-15m-data, settled by
the row's recorded ``result`` -- no synthetic prices anywhere. The sweep varies
the favourite band, the entry window, the side and the entry style, then runs a
fee-aware, capital-limited compounding simulation from a start balance so configs
can be ranked by how fast they grow, not only by raw edge.

Taker entry pays the ask and the Kalshi taker fee 0.07*p*(1-p). Maker entry pays
the bid and no fee, but no queue/fill model is applied, so maker numbers are an
optimistic upper bound (real maker fill rates carry adverse selection).

Usage:
  python scripts/hf_sweep.py --start 104.48 --frac 0.08 --jobs 4
"""
import argparse
import math
import statistics
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SNAP = sorted((Path.home() / ".cache/huggingface/hub/"
               "datasets--papylove--kalshi-15m-data/snapshots").glob("*"))[0]
FEE_RATE = 0.07
_DF: pd.DataFrame | None = None  # worker-global, filled by _init


def _load_quotes() -> pd.DataFrame:
    files = sorted((SNAP / "quote_history").glob("*.parquet"))
    parts = [pd.read_parquet(p) for p in files]
    d = pd.concat(parts, ignore_index=True)
    d = d[d.result.isin(["yes", "no"])].copy()
    d = d.dropna(subset=["yes_bid", "yes_ask", "open_ts", "close_ts"])
    d["open_ts"] = d.open_ts.astype("int64")
    d["close_ts"] = d.close_ts.astype("int64")
    d["entry_ts"] = d.open_ts + d.minute.astype("int64") * 60
    return d[["ticker", "minute", "open_ts", "close_ts", "entry_ts",
              "yes_bid", "yes_ask", "result"]]


def _init():
    global _DF
    _DF = _load_quotes()


def _trades(df, bmin, bmax, wstart, wend, sides, entry) -> pd.DataFrame:
    m = df[(df.minute >= wstart) & (df.minute <= wend)]
    frames = []
    if "yes" in sides:
        p = m.yes_ask if entry == "taker" else m.yes_bid
        f = m[(p * 100).between(bmin, bmax)].assign(side="yes", price=p)
        frames.append(f)
    if "no" in sides:
        p = (1.0 - m.yes_bid) if entry == "taker" else (1.0 - m.yes_ask)
        f = m[(p * 100).between(bmin, bmax)].assign(side="no", price=p)
        frames.append(f)
    if not frames:
        return pd.DataFrame()
    t = pd.concat(frames, ignore_index=True)
    t["yes_win"] = ((t.side == "yes") & (t.result == "yes")) | \
                   ((t.side == "no") & (t.result == "no"))
    # first eligible minute per ticker
    t = t.sort_values(["ticker", "minute"]).groupby("ticker", as_index=False).first()
    t["fee"] = 0.0 if entry == "maker" else FEE_RATE * t.price * (1.0 - t.price)
    t["net"] = t.yes_win.map({True: 1.0, False: 0.0}) * (1.0 - t.price) \
        - (~t.yes_win).astype(float) * t.price - t.fee
    t["ret_on_stake"] = t.net / t.price
    return t


def _compound(t: pd.DataFrame, start: float, frac: float) -> dict:
    """Event-driven, cash-limited compounding (no leverage, concurrency shares cash).

    Entry: stake = frac * free cash, contracts = floor(stake/price), cash -= cost.
    Settlement: credit $1/contract on a win, $0 on a loss. A trade is skipped when
    free cash cannot buy one contract, which is what makes this honest: concurrent
    open positions in the 15-minute overlap draw down the same cash pool.
    """
    if t.empty:
        return {"final": start, "maxdd": 0.0, "taken": 0, "days": 0.0}
    trades = [(r.entry_ts, r.close_ts, float(r.price), bool(r.yes_win), float(r.fee))
              for r in t.itertuples()]
    ev = []
    for i, (e_ts, c_ts, price, win, fee) in enumerate(trades):
        ev.append((e_ts, 0, i))
        ev.append((c_ts, 1, i))
    ev.sort(key=lambda x: (x[0], x[1]))
    cash = start
    open_cost = 0.0
    peak = start
    maxdd = 0.0
    taken = 0
    pos = {}
    t0 = min(e for e, _, _, _, _ in trades)
    t1 = max(c for _, c, _, _, _ in trades)
    for ts, kind, i in ev:
        e_ts, c_ts, price, win, fee = trades[i]
        if kind == 0:
            stake = min(frac * cash, cash)
            contracts = int(stake / price)
            if contracts < 1:
                continue
            cost = contracts * (price + fee)
            if cost > cash:
                continue
            cash -= cost
            open_cost += cost
            pos[i] = (contracts, cost)
            taken += 1
        else:
            contracts, cost = pos.pop(i, (0, 0.0))
            open_cost -= cost
            if win and contracts:
                cash += contracts * 1.0
        equity = cash + open_cost  # entry moves cash<->cost, so equity is flat there
        if equity > peak:
            peak = equity
        if peak > 0:
            maxdd = max(maxdd, (peak - equity) / peak)
    days = (t1 - t0) / 86400.0
    return {"final": cash, "maxdd": maxdd, "taken": taken, "days": days}


def _run_cfg(cfg) -> dict:
    bmin, bmax, wstart, wend, sides, entry, start, frac = cfg
    t = _trades(_DF, bmin, bmax, wstart, wend, sides, entry)
    if t.empty:
        return {"cfg": cfg[:6], "n": 0}
    comp = _compound(t, start, frac)
    n = len(t)
    wins = int(t.yes_win.sum())
    mean_net = float(t.net.mean())
    mean_ret = float(t.ret_on_stake.mean())
    sd = float(t.ret_on_stake.std()) if n > 1 else 0.0
    ad = int(pd.Series(t.entry_ts).floordiv(86400).nunique())
    return {
        "cfg": (bmin, bmax, wstart, wend, sides, entry),
        "n": n, "winrate": wins / n, "mean_net_c": mean_net * 100.0,
        "mean_ret_pct": mean_ret * 100.0, "sd_ret_pct": sd * 100.0,
        "final": comp["final"], "maxdd": comp["maxdd"],
        "taken": comp["taken"], "days": comp["days"], "activedays": ad,
        "x": comp["final"] / start, "frac": frac,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=104.48)
    ap.add_argument("--frac", type=float, default=0.08)
    ap.add_argument("--fracs", default=None,
                    help="comma list of stake fractions, overrides --frac")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--entries", default="taker,maker",
                    help="comma list: taker,maker (maker is an optimistic upper bound)")
    ap.add_argument("--sides", default="both,yes,no")
    a = ap.parse_args()
    want_entries = a.entries.split(",")
    want_sides = a.sides.split(",")
    fracs = [float(x) for x in a.fracs.split(",")] if a.fracs else [a.frac]

    bands = [(90, 99), (93, 99), (95, 99), (96, 99), (97, 99),
             (95, 98), (96, 98), (97, 98), (98, 100), (99, 100),
             (94, 97), (92, 96), (88, 95)]
    windows = [(4, 14), (3, 13), (6, 11), (6, 14), (3, 11),
               (1, 5), (0, 15), (10, 15), (12, 15)]
    sides = ["both", "yes", "no"]
    entries = ["taker", "maker"]

    cfgs = [(bmin, bmax, w0, w1, s, e, a.start, f)
            for (bmin, bmax) in bands for (w0, w1) in windows
            for s in sides for e in entries for f in fracs
            if s in want_sides and e in want_entries]
    print(f"sweeping {len(cfgs)} configs over 26 real days, start=${a.start}, "
          f"fracs={fracs}, jobs={a.jobs}")

    with ProcessPoolExecutor(max_workers=a.jobs, initializer=_init) as ex:
        rows = list(ex.map(_run_cfg, cfgs, chunksize=8))

    rows = [r for r in rows if r.get("n")]
    rows.sort(key=lambda r: -r["final"])
    hdr = (f"{'band':>7} {'win':>6} {'side':>4} {'ent':>5} {'frac':>4} {'n':>5} "
           f"{'win%':>5} {'net_c':>6} {'ret%':>6} {'final$':>10} {'x':>6} "
           f"{'maxdd':>6} {'act':>4}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows[:a.top]:
        b0, b1, w0, w1, s, e = r["cfg"]
        print(f"{b0:>3}-{b1:<3} {w0:>2}-{w1:<3} {s:>4} {e:>5} {r['frac']:>4.2f} "
              f"{r['n']:>5} {r['winrate']*100:>5.1f} {r['mean_net_c']:>+6.3f} "
              f"{r['mean_ret_pct']:>+6.3f} {r['final']:>10.2f} {r['x']:>6.2f} "
              f"{r['maxdd']*100:>5.1f}% {r['activedays']:>4}")

    assert rows, "no configs produced trades -- data load failed"
    best = rows[0]
    assert best["final"] >= a.start, "best config should not lose money"
    print(f"\nbest: {best['cfg']} final=${best['final']:.2f} "
          f"({best['x']:.2f}x over {best['days']:.1f} days)")


if __name__ == "__main__":
    main()
