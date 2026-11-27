#!/usr/bin/env python3
"""Same late-window favourite taker, tested on Polymarket 15M markets.

Data: the real recorded snapshots of Polymarket "Up or Down" binaries
(Solal9/polymarket-crypto-updown-binary) - top-of-book and 5-level depth polled
every 60s. Nothing synthetic.

Entry rule: at minute m of the window, if the Up ask is inside the band, buy Up
at the ask and hold to resolution. One entry per market.

Unlike the Kalshi tick set, this data has ask_depth_usdc, so the same run also
reports how many dollars were actually offered at that price - i.e. capacity,
the open risk on the Kalshi side.

Usage:
  python3 scripts/poly_all_markets.py --assets btc,eth,sol,xrp --tf 15m
  python3 scripts/poly_all_markets.py --assets btc --tf 1h
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from fees import POLY_US_API_TAKER_THETA  # noqa: E402
from execution_realism import evaluate_poly_quotes, official_poly_outcomes  # noqa: E402

HF = Path(os.path.expanduser(
    "~/.cache/huggingface/hub/datasets--Solal9--polymarket-crypto-updown-binary/"
    "snapshots/c17800caa413042941bbd8a9f266b23b6f5f8ed4"))
LOCAL = Path(os.path.expanduser("~/tmp/opencode/poly"))
START = 104.48
COLS = ["collection_timestamp_utc", "slug", "best_ask", "outcome_prices",
        "window_start_unix", "ask_depth_usdc", "ask_depth_shares"]


def _path(asset: str, tf: str) -> Path | None:
    for p in (HF / f"polymarket_{asset}_{tf}_snapshots.csv",
              LOCAL / f"polymarket_{asset}_{tf}_snapshots.csv"):
        if p.exists() and p.stat().st_size > 100_000:
            return p
    return None


def _official_outcomes(df: pd.DataFrame) -> pd.Series:
    return official_poly_outcomes(df)


def load(asset: str, tf: str) -> pd.DataFrame | None:
    p = _path(asset, tf)
    if p is None:
        return None
    df = pd.read_csv(
        p, low_memory=False)
    df["t"] = pd.to_datetime(df.collection_timestamp_utc, utc=True, format="mixed")
    # pandas may store us or ns here, so go through total_seconds (unit-safe)
    df["minute"] = ((df.t - pd.Timestamp(0, tz="UTC")).dt.total_seconds()
                    - df.window_start_unix) / 60.0
    df = df.dropna(subset=["best_ask"])
    df["up_won"] = _official_outcomes(df)
    return df.dropna(subset=["up_won"])


def run(df: pd.DataFrame, band: tuple[float, float], win: tuple[float, float],
        slip_c: float, frac: float) -> dict:
    result = evaluate_poly_quotes(
        df, "best_ask", band, win, slip_c, frac, START, 1.0)
    if result["n"] == 0:
        return result
    t = (df[(df.minute >= win[0]) & (df.minute <= win[1])
            & (df.best_ask * 100).between(band[0], band[1])]
         .sort_values(["slug", "t"]).drop_duplicates("slug", keep="first"))
    depth = pd.to_numeric(t.ask_depth_usdc, errors="coerce")
    return {**result, "cap_usd": float(depth.median()),
            "min_ask": float(t.best_ask.min() * 100)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default="btc,eth,sol,xrp")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--slip", type=float, default=0.0)
    a = ap.parse_args()
    win = (13.0, 15.0) if a.tf == "15m" else (55.0, 60.0)
    cfgs = [((98, 99.9), win), ((97, 99.9), win), ((95, 99.9), win),
            ((98, 99.9), (0, 99 if a.tf == "1h" else 15))]
    print(f"polymarket {a.tf}: band x window, entry=taker at ask, "
          f"slip {a.slip}c, gross fee theta {POLY_US_API_TAKER_THETA}, "
          f"frac 0.30 depth-capped, ${START:.2f} start")
    print(f"{'asset':6} {'cfg':18} {'n':>5} {'win%':>6} {'net_c':>7} {'final$':>10} "
          f"{'dd':>6} {'med_depth$':>10}")
    best = []
    for asset in a.assets.split(","):
        df = load(asset.strip(), a.tf)
        if df is None or df.empty:
            print(f"{asset:6} (no data)")
            continue
        for band, w in cfgs:
            r = run(
                df, band, w, a.slip, 0.30)
            if r["n"] < 5:
                continue
            tag = f"{band[0]:g}-{band[1]:g} m{w[0]:g}-{w[1]:g}"
            final = f"{r['final']:10.2f}" if r["final"] is not None else f"{'n/a':>10}"
            dd = f"{r['dd']:5.1f}%" if r["dd"] is not None else f"{'n/a':>6}"
            print(f"{asset:6} {tag:18} {r['n']:>5} {r['win']:>6.2f} {r['net_c']:>+7.2f} "
                  f"{final} {dd} {r['cap_usd']:>10.0f}")
            if r["final"] is not None:
                best.append((asset, tag, r))
    if best:
        b = max(best, key=lambda x: x[2]["final"])
        print(f"\nbest: {b[0]} {b[1]} n={b[2]['n']} win={b[2]['win']:.2f}% "
              f"net={b[2]['net_c']:+.2f}c -> ${b[2]['final']:.2f}, "
              f"median depth ${b[2]['cap_usd']:.0f}")
        pos = [x for x in best if x[2]["net_c"] > 0]
        print(f"{len(best)} asset-config pairs with n>=5; {len(pos)} positive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
