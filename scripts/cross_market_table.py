#!/usr/bin/env python3
"""One table: does the late-window favourite taker clear $10,000 anywhere?

Reads only cached real data:
  * Kalshi   ~/tmp/opencode/kx/all_{15m,1h,1d}.parquet  (recorded 1m candlesticks)
  * Polymarket snapshots (implied_probability, liquidity, ask_depth_usdc)

Every row is the same rule: enter once per market, taker, inside the late window,
band 98-99c (Kalshi) / 98-100c (Polymarket), hold to settlement, 1c adverse slip,
30% of balance per trade. Fees: Kalshi taker formula, Polymarket 0.

Usage:
  python3 scripts/cross_market_table.py                 # Kalshi + Polymarket
  python3 scripts/cross_market_table.py --platform kalshi
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import kalshi_all_markets as K  # noqa: E402
import poly_ob_markets as P  # noqa: E402

START = 104.48
FRAC = 0.30
SLIP = 1.0
WIN = {"15m": (13, 14), "1h": (55, 59), "1d": (1425, 1439)}


def kalshi_rows() -> list[dict]:
    out = []
    for tf, (w0, w1) in WIN.items():
        f = Path(os.path.expanduser(f"~/tmp/opencode/kx/all_{tf}.parquet"))
        if not f.exists():
            print(f"kalshi {tf}: no data yet ({f.name})", flush=True)
            continue
        df = pd.read_parquet(f)
        if df.empty:
            continue
        # pooled across every series at once = the realistic "trade whatever qualifies"
        r = K.evaluate(df, (98, 99), (w0, w1), SLIP, FRAC)
        if r.get("n", 0) >= 10:
            out.append({"platform": "kalshi", "tf": tf, "scope": f"all {df.series.nunique()} series",
                        **r, "cap": float("nan")})
        else:
            print(f"kalshi {tf}: only {r.get('n', 0)} fills pooled", flush=True)
        best_list = []
        for s, g in df.groupby("series"):
            r2 = K.evaluate(g, (98, 99), (w0, w1), SLIP, FRAC)
            if r2.get("n", 0) >= 10:
                r2["series"] = s
                best_list.append(r2)
        if best_list:
            best_df = pd.DataFrame(best_list)
            top = best_df.sort_values("final", ascending=False).head(3)
            for _, r2 in top.iterrows():
                out.append({"platform": "kalshi", "tf": tf, "scope": f"{r2['series']} (best)",
                            **{k: r2[k] for k in ("n", "win", "net_c", "final", "dd", "fees")},
                            "cap": float("nan")})
    return out


def poly_rows(assets: str) -> list[dict]:
    out = []
    for tf in ("5m", "15m", "1h", "1d"):
        win = {"5m": (3.0, 5.0), "15m": (13.0, 15.0), "1h": (55.0, 60.0), "1d": (225.0, 240.0)}[tf]
        for asset in assets.split(","):
            asset = asset.strip()
            try:
                df = P.build(asset, tf)
            except SystemExit as e:
                print(f"poly {asset} {tf}: {e}", flush=True)
                continue
            r = P.run(df, (98, 100), win, SLIP, FRAC)
            if r.get("n", 0) < 5:
                print(f"poly {asset} {tf}: only {r.get('n', 0)} fills", flush=True)
                continue
            out.append({"platform": "poly", "tf": tf, "scope": asset,
                        "n": r["n"], "win": r["win"], "net_c": r["net_c"],
                        "final": r["final"], "dd": r["dd"], "fees": 0.0,
                        "cap": r.get("liq")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="both", choices=["both", "kalshi", "poly"])
    ap.add_argument("--assets", default="btc,eth,sol,xrp")
    a = ap.parse_args()
    rows = []
    if a.platform in ("both", "kalshi"):
        rows += kalshi_rows()
    if a.platform in ("both", "poly"):
        rows += poly_rows(a.assets)
    if not rows:
        print("no rows: no cached data matched")
        return 1
    t = pd.DataFrame(rows)
    t["clears_10k"] = t.final >= 10000
    print(f"\nrule: band 98+ in the late window, taker, {SLIP:.0f}c slip, "
          f"{FRAC:.0%} of balance, hold to settlement, ${START:.2f} start\n")
    print(f"{'platform':8} {'tf':4} {'scope':26} {'n':>5} {'win%':>6} {'net_c':>7} "
          f"{'final$':>11} {'dd':>6} {'fees$':>8} {'capacity$':>10} {'10k':>4}")
    for r in rows:
        print(f"{r['platform']:8} {r['tf']:4} {r['scope'][:26]:26} {r['n']:>5} "
              f"{r['win']:>6.2f} {r['net_c']:>+7.2f} {r['final']:>11.2f} "
              f"{r['dd']:>5.1f}% {r['fees']:>8.2f} {r['cap']:>10.0f} "
              f"{'YES' if r['final'] >= 10000 else 'no':>4}")
    yes = int(t.clears_10k.sum())
    print(f"\n{len(rows)} configurations tested; {yes} clear $10,000 after reality tax")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
