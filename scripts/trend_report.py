#!/usr/bin/env python3
"""Summarise trend_maker_log.jsonl: is the paper edge distinguishable from zero?

Usage: python scripts/trend_report.py [path] [--min-trips N]

Prints round trips, win rate, mean net P&L per trip with a bootstrap 95% CI,
and the exit-kind mix. The verdict is deliberately strict: the lower bound of
the CI must be above zero AND there must be at least --min-trips round trips.
Paper fills are conservative on queue position but ignore latency and market
impact, so passing this is necessary, not sufficient, for going live.
"""
import argparse
import json
import random
import statistics
import sys
from collections import Counter
from pathlib import Path


def load(path):
    rows = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def bootstrap_ci(xs, n=4000, seed=7):
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default="trend_maker_log.jsonl")
    ap.add_argument("--min-trips", type=int, default=100)
    a = ap.parse_args(argv)
    exits = [r for r in load(a.path) if r.get("event") == "exit"]
    if not exits:
        print("no round trips logged yet")
        return 1
    pnl = [float(r["pnl"]) for r in exits]
    wins = sum(1 for p in pnl if p > 0)
    mean = statistics.fmean(pnl)
    lo, hi = bootstrap_ci(pnl) if len(pnl) > 1 else (mean, mean)
    print(f"round trips      : {len(pnl)}")
    print(f"win rate         : {wins / len(pnl):.1%}")
    print(f"total net pnl    : ${sum(pnl):.2f}")
    print(f"mean per trip    : ${mean:.4f}  (95% CI ${lo:.4f} .. ${hi:.4f})")
    print(f"exit kinds       : {dict(Counter(r['kind'] for r in exits))}")
    ok = lo > 0 and len(pnl) >= a.min_trips
    print("verdict          :",
          "CI above zero - worth a tiny live trial" if ok else
          f"NOT proven (need CI lower bound > 0 and >= {a.min_trips} trips)")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
