#!/usr/bin/env python3
"""Slice the honest taker edge by coin and by hour of day.

The 26-day sweep pools 15 coins into one +0.42c/trade figure. A pooled mean can
hide one coin whose edge is several times larger, which would change the growth
math. This fixes the best config (band 97-98c, minutes 3-11, YES, taker) and
reports n / win% / net cents per trade, plus a per-slice cash-limited compound,
so a materially better sub-population is visible rather than assumed away.

Real per-minute quotes and recorded settlements only. A per-coin compound is an
optimistic ceiling because it gives each coin its own capital pool.

Usage:  python scripts/hf_slice.py
"""
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import hf_sweep as S  # noqa: E402

BAND = (97, 98)
WIN = (3, 11)
START = 104.48
FRAC = 0.10


def _table(t, by, title):
    print(f"\n=== {title} ===")
    hdr = f"{'slice':>8} {'n':>5} {'win%':>6} {'net_c':>7} {'final$':>9} {'x':>5}"
    print(hdr)
    print("-" * len(hdr))
    for k, g in t.groupby(by):
        comp = S._compound(g, START, FRAC)
        print(f"{str(k):>8} {len(g):>5} {g.yes_win.mean()*100:>6.1f} "
              f"{g.net.mean()*100:>+7.3f} {comp['final']:>9.2f} "
              f"{comp['final']/START:>5.2f}")


def main():
    d = S._load_quotes()
    t = S._trades(d, BAND[0], BAND[1], WIN[0], WIN[1], "yes", "taker")
    assert not t.empty, "no trades for the best config"
    t = t.copy()
    t["coin"] = t.ticker.str.extract(r"^KX([A-Z]+)15M", expand=False)
    t["hour"] = pd.to_datetime(t.entry_ts, unit="s", utc=True).dt.hour

    pooled = S._compound(t, START, FRAC)
    print(f"config band {BAND[0]}-{BAND[1]} min {WIN[0]}-{WIN[1]} YES taker "
          f"frac={FRAC}")
    print(f"POOLED: n={len(t)} win={t.yes_win.mean()*100:.1f}% "
          f"net={t.net.mean()*100:+.3f}c final=${pooled['final']:.2f} "
          f"({pooled['final']/START:.2f}x, maxdd {pooled['maxdd']*100:.1f}%)")
    _table(t, "coin", "by coin")
    _table(t, "hour", "by hour (UTC)")
    assert len(t) == t.groupby("coin").size().sum()


if __name__ == "__main__":
    main()
