#!/usr/bin/env python3
"""Greedy refinement of a fair-value config: drop the coins and hours that hurt.

The objective now explicitly permits in-sample optimisation, so this starts from
a fair-value base config and greedily adds the exclusion predicate (one coin, or
one hour-of-day bucket) that most raises the simulated final balance, repeating
until no exclusion helps. Everything is real: recorded spot/strike/vol/minutes,
recorded quotes, recorded settlement; taker fill + Kalshi fee on every leg.

The reported maxdd is the equity drawdown of the same walk, so the winning
config can be judged on risk as well as on the final number.

Usage:
  python scripts/hf_refine.py --band 95 99 --window 3 11 --edge 2 --vscale 0.5 \
      --frac 0.35
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import hf_fairvalue as F  # noqa: E402
from hf_sweep import _compound  # noqa: E402


def trades(base: pd.DataFrame, edge_c: float, scale: float) -> pd.DataFrame:
    """Taker trades for a YES-band/window selection on the fair-value frame."""
    fair = base[f"fair_{scale}"].to_numpy(float)
    keep = ~np.isnan(fair)
    p = base.kalshi_yes_ask.to_numpy(float)
    edge = (fair - p - F.FEE_RATE * p * (1.0 - p)) * 100.0
    sel = keep & (edge >= edge_c)
    t = base.loc[sel, ["ticker", "minutes_remaining", "entry_ts", "close_ts",
                       "result"]].copy()
    t["price"] = p[sel]
    win = base.result.to_numpy()[sel] == "yes"
    t["yes_win"] = win
    t["net"] = np.where(win, 1.0 - t.price, -t.price) \
        - F.FEE_RATE * t.price * (1.0 - t.price)
    t = t.sort_values(["ticker", "minutes_remaining"]).groupby(
        "ticker", as_index=False).first()
    t["coin"] = t.ticker.str.extract(r"^KX([A-Z]+)15M", expand=False)
    t["hour"] = pd.to_datetime(t.entry_ts, unit="s", utc=True).dt.hour
    return t


def score(t: pd.DataFrame, start: float, frac: float) -> dict:
    if t.empty:
        return {"final": start, "maxdd": 0.0, "n": 0}
    c = _compound(t, start, frac)
    return {"final": c["final"], "maxdd": c["maxdd"], "n": len(t),
            "win": t.yes_win.mean(), "net_c": t.net.mean() * 100.0}


def greedy(t: pd.DataFrame, start: float, frac: float, step: float):
    """Add the single exclusion (one coin or one hour bucket) that helps most."""
    excl_coin: set = set()
    excl_hour: set = set()
    best = score(t, start, frac)
    log = []
    while True:
        cand = []
        for c in sorted(t.coin.dropna().unique()):
            if c not in excl_coin:
                m = ~(t.coin.isin(excl_coin | {c}) | t.hour.isin(excl_hour))
                cand.append((score(t[m], start, frac), ("coin", c)))
        # hours grouped into 6h buckets so the exclusion is not a 26-day fluke
        for b in range(4):
            hrs = set(range(b * 6, b * 6 + 6))
            if not excl_hour >= hrs:
                m = ~(t.coin.isin(excl_coin) | t.hour.isin(excl_hour | hrs))
                cand.append((score(t[m], start, frac), ("hour", f"{b*6}-{b*6+5}")))
        if not cand:
            break
        cand.sort(key=lambda x: -x[0]["final"])
        top, tag = cand[0]
        if top["final"] < best["final"] * (1.0 + step):
            break
        best = top
        if tag[0] == "coin":
            excl_coin.add(tag[1])
        else:
            b = int(tag[1].split("-")[0]) // 6
            excl_hour |= set(range(b * 6, b * 6 + 6))
        log.append((tag, best))
        print(f"  drop {tag[0]:4} {tag[1]:>10} -> n={best['n']:>5} "
              f"win={best['win']*100:>5.1f}% net={best['net_c']:>+6.3f}c "
              f"final=${best['final']:>9.2f} maxdd={best['maxdd']*100:>5.1f}%",
              flush=True)
    return best, excl_coin, excl_hour, log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--band", nargs=2, type=float, default=[95, 99])
    ap.add_argument("--window", nargs=2, type=float, default=[3, 11])
    ap.add_argument("--edge", type=float, default=2.0)
    ap.add_argument("--vscale", type=float, default=0.5)
    ap.add_argument("--frac", type=float, default=0.35)
    ap.add_argument("--start", type=float, default=104.48)
    ap.add_argument("--step", type=float, default=0.01, help="min gain to accept")
    a = ap.parse_args()

    F._init()
    d = F._FV
    d = d[(d.minutes_remaining >= a.window[0]) & (d.minutes_remaining <= a.window[1])
          & (d.kalshi_yes_ask * 100 >= a.band[0])
          & (d.kalshi_yes_ask * 100 <= a.band[1])]
    t = trades(d, a.edge, a.vscale)
    base = score(t, a.start, a.frac)
    print(f"BASE band {a.band} win {a.window} edge>={a.edge}c vscale={a.vscale} "
          f"frac={a.frac}: n={base['n']} win={base['win']*100:.1f}% "
          f"net={base['net_c']:+.3f}c final=${base['final']:.2f} "
          f"({base['final']/a.start:.2f}x, maxdd {base['maxdd']*100:.1f}%)")
    print("greedy exclusions:")
    best, ec, eh, _ = greedy(t, a.start, a.frac, a.step)
    print(f"\nREFINED: exclude coins {sorted(ec) or '{}'}, hours {sorted(eh) or '{}'}")
    print(f"  n={best['n']} win={best['win']*100:.1f}% net={best['net_c']:+.3f}c "
          f"final=${best['final']:.2f} ({best['final']/a.start:.2f}x, "
          f"maxdd {best['maxdd']*100:.1f}%)")
    assert best["n"] > 0


if __name__ == "__main__":
    main()
