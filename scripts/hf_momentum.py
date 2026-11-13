#!/usr/bin/env python3
"""Momentum-scalper backtest on the real HF Kalshi 15m quote_history.

Signal: over a lookback of L minutes the favourite's mid has risen by >= thr
cents and its price is in the band -> buy the ask, exit after a fixed hold at
the then-current bid (both legs pay the taker fee). If the hold runs past the
window close the position holds to settlement. Inputs are the real per-minute
yes_bid/yes_ask and the recorded result; nothing is synthetic.

Vectorised: shift-based momentum and forward prices are precomputed once per
worker; each config is then a boolean filter. Compounds with the cash-limited
ledger (two taker fees per round trip).

Usage:
  python scripts/hf_momentum.py --jobs 8 --top 30
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FEE_RATE = 0.07
LOOKS = [1, 2, 3]
HOLDS = [2, 4]
_G = None


def _init():
    global _G
    snap = sorted((Path.home() / ".cache/huggingface/hub/"
                   "datasets--papylove--kalshi-15m-data/snapshots").glob("*"))[0]
    d = pd.concat([pd.read_parquet(p) for p in
                   sorted((snap / "quote_history").glob("*.parquet"))],
                  ignore_index=True)
    d = d[d.result.isin(["yes", "no"])].dropna(subset=["yes_bid", "yes_ask"]).copy()
    d["open_ts"] = d.open_ts.astype("int64")
    d["close_ts"] = d.close_ts.astype("int64")
    d["minute"] = d.minute.astype("int64")
    d = d.sort_values(["ticker", "minute"])
    d["mid"] = (d.yes_bid + d.yes_ask) / 2.0
    d["entry_ts"] = d.open_ts + d.minute * 60
    g = d.groupby("ticker", sort=False)
    for L in LOOKS:
        d[f"mp{L}"] = g.mid.shift(L)
    for H in HOLDS:
        d[f"bN{H}"] = g.yes_bid.shift(-H)
        d[f"aN{H}"] = g.yes_ask.shift(-H)
        d[f"mN{H}"] = g.minute.shift(-H)
    _G = d.reset_index(drop=True)


def _sim(trades, start, frac):
    """trades: [(entry_ts, exit_ts, entry_p, exit_p, fee_in, fee_out), ...]."""
    if not trades:
        return {"final": start, "maxdd": 0.0, "taken": 0}
    ev = []
    for i, t in enumerate(trades):
        ev.append((t[0], 0, i))
        ev.append((t[1], 1, i))
    ev.sort(key=lambda x: (x[0], x[1]))
    cash, open_cost, peak, maxdd, taken = start, 0.0, start, 0.0, 0
    pos = {}
    for ts, kind, i in ev:
        e_ts, x_ts, ep, xp, fin, fout = trades[i]
        if kind == 0:
            c = int(min(frac * cash, cash) / ep)
            if c < 1:
                continue
            cost = c * (ep + fin)
            if cost > cash:
                continue
            cash -= cost
            open_cost += cost
            pos[i] = (c, cost)
            taken += 1
        else:
            c, cost = pos.pop(i, (0, 0.0))
            open_cost -= cost
            cash += c * (xp - fout)
        eq = cash + open_cost
        peak = max(peak, eq)
        if peak > 0:
            maxdd = max(maxdd, (peak - eq) / peak)
    return {"final": cash, "maxdd": maxdd, "taken": taken}


def _run(cfg) -> dict:
    look, thr_c, hold, bmin, bmax, sides, start, frac = cfg
    moms = _G.mid - _G[f"mp{look}"]
    valid = _G[f"mp{look}"].notna()
    frames = []
    for side in sides:
        p = _G.yes_ask if side == "yes" else 1.0 - _G.yes_bid
        ms = moms if side == "yes" else -moms
        sel = valid & (p * 100).between(bmin, bmax) & (ms * 100 >= thr_c)
        if not sel.any():
            continue
        f = _G.loc[sel, ["ticker", "minute", "open_ts", "close_ts", "result"]].copy()
        f["side"] = side
        f["p"] = p[sel]
        f["contig"] = _G.loc[sel, f"mN{hold}"].to_numpy() == (f.minute + hold).to_numpy()
        bn = _G.loc[sel, f"bN{hold}"].to_numpy()
        an = _G.loc[sel, f"aN{hold}"].to_numpy()
        if side == "yes":
            settle = (f.result == "yes").to_numpy().astype(float)
            f["xp"] = np.where(f.contig.to_numpy(), bn, settle)
        else:
            settle = (f.result == "no").to_numpy().astype(float)
            f["xp"] = np.where(f.contig.to_numpy(), 1.0 - an, settle)
        f["x_ts"] = np.where(f.contig.to_numpy(),
                             f.open_ts.to_numpy() + (f.minute.to_numpy() + hold) * 60,
                             f.close_ts.to_numpy())
        frames.append(f)
    if not frames:
        return {"cfg": cfg[:6], "n": 0}
    t = pd.concat(frames, ignore_index=True)
    t = t.sort_values(["ticker", "minute"]).groupby("ticker", as_index=False).first()
    t["fin"] = FEE_RATE * t.p * (1.0 - t.p)
    t["fout"] = FEE_RATE * t.xp * (1.0 - t.xp)
    t = t[t.x_ts > t.open_ts + t.minute * 60]
    if t.empty:
        return {"cfg": cfg[:6], "n": 0}
    trades = [(int(r.open_ts + r.minute * 60), int(r.x_ts), float(r.p), float(r.xp),
               float(r.fin), float(r.fout)) for r in t.itertuples()]
    comp = _sim(trades, start, frac)
    pnlc = (t.xp - t.p - t.fin - t.fout) * 100.0
    return {
        "cfg": cfg[:6], "n": len(t), "mean_net_c": float(pnlc.mean()),
        "winrate": float((t.xp > t.p).mean()), "final": comp["final"],
        "maxdd": comp["maxdd"], "taken": comp["taken"], "x": comp["final"] / start,
        "frac": frac,
        "activedays": int(pd.Series(t.open_ts).floordiv(86400).nunique()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=104.48)
    ap.add_argument("--frac", type=float, default=0.08)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--top", type=int, default=30)
    a = ap.parse_args()

    thrs = [0.0, 1.0, 2.0, 3.0]
    bands = [(90, 99), (95, 99), (70, 99), (50, 99)]
    sides = [("yes",), ("yes", "no")]
    cfgs = [(l, th, h, b0, b1, s, a.start, a.frac)
            for l in LOOKS for th in thrs for h in HOLDS
            for (b0, b1) in bands for s in sides]
    print(f"momentum sweep: {len(cfgs)} configs over 26 real days, "
          f"start=${a.start}, frac={a.frac}", flush=True)
    with ProcessPoolExecutor(max_workers=a.jobs, initializer=_init) as ex:
        rows = list(ex.map(_run, cfgs, chunksize=2))
    rows = [r for r in rows if r.get("n")]
    rows.sort(key=lambda r: -r["final"])
    hdr = (f"{'look':>4} {'thr':>4} {'hold':>4} {'band':>7} {'sides':>8} {'n':>5} "
           f"{'net_c':>6} {'win%':>5} {'final$':>10} {'x':>6} {'maxdd':>6} {'act':>4}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows[:a.top]:
        l, th, h, b0, b1, s = r["cfg"]
        print(f"{l:>4} {th:>4.1f} {h:>4} {b0:>3}-{b1:<3} {'+'.join(s):>8} "
              f"{r['n']:>5} {r['mean_net_c']:>+6.3f} {r['winrate']*100:>5.1f} "
              f"{r['final']:>10.2f} {r['x']:>6.2f} {r['maxdd']*100:>5.1f}% "
              f"{r['activedays']:>4}")
    assert rows, "no momentum configs traded"
    print("\n(positive net_c = momentum paid AFTER two taker fees + spread)")


if __name__ == "__main__":
    main()
