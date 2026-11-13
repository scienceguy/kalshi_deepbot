#!/usr/bin/env python3
"""Out-of-sample check of the hf_sweep best configs.

The 26-day sweep picks its winners on the same days it scores them, which is
in-sample. This splits the real quote_history by calendar date: configs are run
on the earliest days (train) and re-scored on the later held-out days (test).
Only a config whose edge survives the split is trustworthy.

Usage:
  python scripts/hf_oos.py --train 20
"""
import argparse
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import hf_sweep as S  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", type=int, default=20, help="train days (rest = test)")
    ap.add_argument("--frac", type=float, default=0.10)
    ap.add_argument("--start", type=float, default=104.48)
    a = ap.parse_args()

    d = S._load_quotes()
    d["day"] = pd.to_datetime(d.open_ts, unit="s").dt.date
    days = sorted(d.day.unique())
    tr_days, te_days = days[:a.train], days[a.train:]
    tr = d[d.day.isin(tr_days)].copy()
    te = d[d.day.isin(te_days)].copy()
    print(f"train days {len(tr_days)} ({tr_days[0]}..{tr_days[-1]}), "
          f"test days {len(te_days)} ({te_days[0]}..{te_days[-1]})")

    bands = [(96, 99), (97, 98), (96, 98), (95, 98), (94, 97), (95, 99), (96, 97)]
    windows = [(3, 11), (6, 11), (4, 14), (3, 13)]
    cfg6 = [(b0, b1, w0, w1, "yes", "taker") for (b0, b1) in bands
            for (w0, w1) in windows]

    def score(df, cfg6):
        S._DF = df
        out = []
        for c in cfg6:
            r = S._run_cfg((*c, a.start, a.frac))
            out.append(r if r.get("n") else {"cfg": c, "n": 0})
        return out

    trs = score(tr, cfg6)
    tes = score(te, cfg6)
    te_map = {tuple(r["cfg"]): r for r in tes}
    hdr = (f"{'band':>7} {'win':>7} | {'tr_n':>5} {'tr_win%':>7} {'tr_net_c':>8} "
           f"{'tr_final$':>9} | {'te_n':>5} {'te_win%':>7} {'te_net_c':>8} "
           f"{'te_final$':>9} {'te_x':>5}")
    print(hdr)
    print("-" * len(hdr))
    rows = [r for r in trs if r.get("n")]
    rows.sort(key=lambda r: -r["final"])
    for r in rows:
        c = tuple(r["cfg"])
        b0, b1, w0, w1, s, e = c
        te = te_map.get(c, {"n": 0})
        if te.get("n"):
            print(f"{b0:>3}-{b1:<3} {w0:>2}-{w1:<4} | {r['n']:>5} "
                  f"{r['winrate']*100:>7.1f} {r['mean_net_c']:>+8.3f} "
                  f"{r['final']:>9.2f} | {te['n']:>5} {te['winrate']*100:>7.1f} "
                  f"{te['mean_net_c']:>+8.3f} {te['final']:>9.2f} {te['x']:>5.2f}")
        else:
            print(f"{b0:>3}-{b1:<3} {w0:>2}-{w1:<4} | {r['n']:>5} "
                  f"{r['winrate']*100:>7.1f} {r['mean_net_c']:>+8.3f} "
                  f"{r['final']:>9.2f} | (no test trades)")

    sv = [te_map.get(tuple(r["cfg"])) for r in rows]
    sv = [x for x in sv if x and x.get("n")]
    if sv:
        pos = sum(1 for x in sv if x["mean_net_c"] > 0)
        print(f"\ntest: {pos}/{len(sv)} configs still positive; "
              f"median test net {sorted(x['mean_net_c'] for x in sv)[len(sv)//2]:+.3f}c")


if __name__ == "__main__":
    main()
