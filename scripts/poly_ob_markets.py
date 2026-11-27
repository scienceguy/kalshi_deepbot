#!/usr/bin/env python3
"""Polymarket 15M / 1H / 1D late-window test on the only usable quote field.

What is and is not in Solal9/polymarket-crypto-updown-binary (measured, not
assumed):

  * `best_bid` / `best_ask` in the snapshot files are constant placeholders
    (0.01 / 0.99 on every row, corr 0.03 with the traded price). Unusable.
  * the `*_orderbook.csv` files are worse, not better: level-1 is the same
    constant 0.01/0.99 and the deeper levels are a fixed 1c ladder
    (asks 0.99..0.95, bids 0.01..0.05) regardless of the market. Unusable.
  * `implied_probability` is real: it tracks the recorded Up price vector with
    corr 1.000 and varies per minute. This is the only real quote in the set.

So the entry price here is `implied_probability` (the venue's own current Up
price) plus a configurable slippage, which is the honest way to use the field:
it is a quoted price, not an executable ask, so it cannot be taken as free of
spread. The Kalshi-side test pays the recorded ask; this one pays the recorded
quote and the difference is stated rather than hidden.

Resolution: only immutable binary result/settlement records from the official
US venue are accepted. Price vectors are quotes, never settlement evidence.

Usage:
  python3 scripts/poly_ob_markets.py --assets btc,eth,sol,xrp --tf 15m
  python3 scripts/poly_ob_markets.py --assets btc --tf 1h
  python3 scripts/poly_ob_markets.py --assets btc --tf 1d
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from execution_realism import evaluate_poly_quotes, official_poly_outcomes  # noqa: E402

HF = Path(os.path.expanduser(
    "~/.cache/huggingface/hub/datasets--Solal9--polymarket-crypto-updown-binary/"
    "snapshots/c17800caa413042941bbd8a9f266b23b6f5f8ed4"))
LOCAL = Path(os.path.expanduser("~/tmp/opencode/poly"))
START = 104.48
SEG = {"5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
COLS = ["collection_timestamp_utc", "slug", "window_start_unix",
        "implied_probability", "outcome_prices",
        "liquidity", "ask_depth_usdc", "volume"]


def _file(asset: str, tf: str, kind: str) -> Path | None:
    for p in (HF / f"polymarket_{asset}_{tf}_{kind}.csv",
              LOCAL / f"polymarket_{asset}_{tf}_{kind}.csv"):
        if p.exists() and p.stat().st_size > 100_000:
            return p
    return None


def _official_outcomes(df: pd.DataFrame) -> pd.Series:
    return official_poly_outcomes(df)


def build(asset: str, tf: str) -> pd.DataFrame:
    f = _file(asset, tf, "snapshots")
    if f is None:
        raise SystemExit(f"missing snapshots for {asset} {tf}")
    df = pd.read_csv(
        f, low_memory=False)
    df = df.dropna(subset=["implied_probability", "window_start_unix"])
    df["t"] = pd.to_datetime(df.collection_timestamp_utc, utc=True, format="mixed")
    df["minute"] = ((df.t - pd.Timestamp(0, tz="UTC")).dt.total_seconds()
                    - df.window_start_unix) / 60.0
    df["up_price"] = df.implied_probability.astype(float)

    df["up_won"] = _official_outcomes(df)
    return df[df.up_won.notna()].copy()


def run(df: pd.DataFrame, band: tuple[float, float], win: tuple[float, float],
        slip_c: float, frac: float) -> dict:
    result = evaluate_poly_quotes(
        df, "up_price", band, win, slip_c, frac, START, 0.995)
    if result["n"] == 0:
        return result
    ask_c = df.up_price * 100.0
    t = (df[(df.minute >= win[0]) & (df.minute <= win[1]) & ask_c.between(*band)]
         .sort_values(["slug", "t"]).drop_duplicates("slug", keep="first"))
    num = lambda c: pd.to_numeric(t[c], errors="coerce").median()  # noqa: E731
    return {**result, "liq": num("liquidity"), "depth": num("ask_depth_usdc"),
            "vol": num("volume")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default="btc")
    ap.add_argument("--tf", default="15m", choices=sorted(SEG))
    ap.add_argument("--slip", type=float, default=1.0,
                    help="cents added to the quoted price (default 1.0)")
    a = ap.parse_args()
    full = {"5m": 5, "15m": 15, "1h": 60, "1d": 240}[a.tf]
    win = {"5m": (3.0, 5.0), "15m": (13.0, 15.0), "1h": (55.0, 60.0), "1d": (225.0, 240.0)}[a.tf]
    print(f"polymarket {a.tf}: entry at quoted implied_probability + {a.slip}c, "
          f"frac 0.30, ${START:.2f} start")
    print(f"{'asset':6} {'cfg':20} {'n':>5} {'win%':>6} {'net_c':>7} {'final$':>10} "
          f"{'dd':>6} {'liq$':>8} {'askdepth$':>10} {'vol$':>8}")
    rows = []
    for asset in a.assets.split(","):
        try:
            df = build(asset.strip(), a.tf)
        except SystemExit as e:
            print(f"{asset}: {e}")
            continue
        for band, w in [((98, 100), win), ((97, 100), win), ((95, 100), win),
                        ((98, 100), (0, full))]:
            r = run(df, band, w, a.slip, 0.30)
            if r["n"] < 5:
                continue
            tag = f"{band[0]:g}-{band[1]:g} m{w[0]:g}-{w[1]:g}"
            final = f"{r['final']:10.2f}" if r["final"] is not None else f"{'n/a':>10}"
            dd = f"{r['dd']:5.1f}%" if r["dd"] is not None else f"{'n/a':>6}"
            print(f"{asset:6} {tag:20} {r['n']:>5} {r['win']:>6.2f} "
                  f"{r['net_c']:>+7.2f} {final} {dd} "
                  f"{r['liq']:>8.0f} {r['depth']:>10.0f} {r['vol']:>8.0f}")
            if r["final"] is not None:
                rows.append((asset, tag, r))
    if rows:
        b = max(rows, key=lambda x: x[2]["final"])
        print(f"\nbest: {b[0]} {b[1]} n={b[2]['n']} win={b[2]['win']:.2f}% "
              f"net={b[2]['net_c']:+.2f}c -> ${b[2]['final']:.2f}")
        pos = [x for x in rows if x[2]["net_c"] > 0]
        print(f"{len(rows)} asset-config pairs with n>=5; {len(pos)} positive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
