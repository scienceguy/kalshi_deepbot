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

Resolution: recorded decisive vector where the file has one, else real Binance
1m closes at the window boundaries (Polymarket settles on a Chainlink feed; the
proxy is validated against the decisive subset and printed as a %.

Usage:
  python3 scripts/poly_ob_markets.py --assets btc,eth,sol,xrp --tf 15m
  python3 scripts/poly_ob_markets.py --assets btc --tf 1h
  python3 scripts/poly_ob_markets.py --assets btc --tf 1d
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

HF = Path(os.path.expanduser(
    "~/.cache/huggingface/hub/datasets--Solal9--polymarket-crypto-updown-binary/"
    "snapshots/c17800caa413042941bbd8a9f266b23b6f5f8ed4"))
LOCAL = Path(os.path.expanduser("~/tmp/opencode/poly"))
START = 104.48
SEG = {"5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
BINANCE_SYMBOL = {"btc": "btcusdt", "eth": "ethusdt", "sol": "solusdt",
                  "xrp": "xrpusdt", "bnb": "bnbusdt"}
COLS = ["collection_timestamp_utc", "slug", "window_start_unix",
        "implied_probability", "outcome_prices",
        "liquidity", "ask_depth_usdc", "volume"]


def _file(asset: str, tf: str, kind: str) -> Path | None:
    for p in (HF / f"polymarket_{asset}_{tf}_{kind}.csv",
              LOCAL / f"polymarket_{asset}_{tf}_{kind}.csv"):
        if p.exists() and p.stat().st_size > 100_000:
            return p
    return None


def spot(asset: str = "btc") -> pd.Series:
    """Real Binance 1m closes; both cached windows are concatenated."""
    sym = BINANCE_SYMBOL.get(asset, "btcusdt")
    parts = []
    for name in (f"{sym}_1m.parquet", f"{sym}_1m_mar.parquet"):
        p = Path(os.path.expanduser(f"~/tmp/opencode/binance/{name}"))
        if p.exists():
            parts.append(pd.read_parquet(p))
    if not parts:
        raise SystemExit(f"no binance 1m parquet for {sym}")
    bn = pd.concat(parts, ignore_index=True).drop_duplicates("ts_ns").sort_values("ts_ns")
    idx = (bn.ts_ns // 10**9).astype("int64").to_numpy()
    return pd.Series(bn.value.to_numpy(float), index=idx)


def build(asset: str, tf: str) -> pd.DataFrame:
    f = _file(asset, tf, "snapshots")
    if f is None:
        raise SystemExit(f"missing snapshots for {asset} {tf}")
    df = pd.read_csv(f, usecols=COLS, low_memory=False)
    df = df.dropna(subset=["implied_probability", "window_start_unix"])
    df["t"] = pd.to_datetime(df.collection_timestamp_utc, utc=True, format="mixed")
    df["minute"] = ((df.t - pd.Timestamp(0, tz="UTC")).dt.total_seconds()
                    - df.window_start_unix) / 60.0
    df["up_price"] = df.implied_probability.astype(float)

    seg = SEG[tf]
    px = spot(asset)

    def at(ts: int) -> float:
        i = int(px.index.searchsorted(ts, side="right")) - 1
        return float(px.iloc[max(i, 0)])

    def decisive(s) -> bool:
        try:
            a = json.loads(s)
            return max(float(a[0]), float(a[1])) > 0.99
        except (TypeError, ValueError, IndexError):
            return False

    # A decisive (>0.99) price vector only settles the market if it was printed
    # at/after the window close. A 0.99 print with hours still to run is just a
    # strong favourite and can reverse, which is why using every decisive row
    # agreed with spot only 70% of the time on 1d markets.
    end_ts = (df.window_start_unix + seg).astype("int64")
    t_unix = ((df.t - pd.Timestamp(0, tz="UTC")).dt.total_seconds()).astype("int64")
    settled = df.outcome_prices.map(decisive) & (t_unix >= end_ts - 120)
    def _up(s):
        if not isinstance(s, str) or not s.startswith("["):
            return np.nan
        try:
            return float(json.loads(s)[0]) > 0.5
        except (TypeError, ValueError, IndexError):
            return np.nan
    up_val = df.outcome_prices.map(_up)
    df["decisive_up"] = up_val.where(settled)
    rec_by_slug = df[df.decisive_up.notna()].groupby("slug").decisive_up.last()
    proxy_by_slug = pd.Series(
        [at(int(s) + seg) >= at(int(s)) for s in
         df.drop_duplicates("slug").set_index("slug").window_start_unix],
        index=df.drop_duplicates("slug").slug.to_numpy())
    df["up_won"] = df.slug.map(rec_by_slug).where(
        df.slug.map(rec_by_slug).notna(), df.slug.map(proxy_by_slug))
    ok = rec_by_slug.dropna()
    if len(ok):
        prox = proxy_by_slug.reindex(ok.index).to_numpy(bool)
        agree = (ok.to_numpy(bool) == prox).mean()
        print(f"resolve check {asset} {tf}: {len(ok)}/{df.slug.nunique()} markets "
              f"settled by a post-close quote; binance proxy agrees {agree * 100:.1f}%",
              flush=True)
    return df


def run(df: pd.DataFrame, band: tuple[float, float], win: tuple[float, float],
        slip_c: float, frac: float) -> dict:
    ask_c = df.up_price * 100.0
    d = df[(df.minute >= win[0]) & (df.minute <= win[1]) & ask_c.between(*band)]
    if d.empty:
        return {"n": 0}
    t = d.sort_values(["slug", "t"]).groupby("slug", as_index=False).first()
    p = ((t.up_price * 100 + slip_c) / 100.0).clip(0.01, 0.995)
    net = t.up_won.astype(float) * (1 - p) - (~t.up_won.astype(bool)).astype(float) * p
    bal, peak, dd = START, START, 0.0
    for n, pr in zip(net, p):
        bal += bal * frac * n / pr
        peak = max(peak, bal)
        dd = max(dd, 1 - bal / peak)
        if bal <= 1:
            break
    num = lambda c: pd.to_numeric(t[c], errors="coerce").median()  # noqa: E731
    return {"n": len(t), "win": t.up_won.mean() * 100, "net_c": net.mean() * 100,
            "final": bal, "dd": dd * 100, "liq": num("liquidity"),
            "depth": num("ask_depth_usdc"), "vol": num("volume")}


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
            rows.append((asset, tag, r))
            print(f"{asset:6} {tag:20} {r['n']:>5} {r['win']:>6.2f} "
                  f"{r['net_c']:>+7.2f} {r['final']:>10.2f} {r['dd']:>5.1f}% "
                  f"{r['liq']:>8.0f} {r['depth']:>10.0f} {r['vol']:>8.0f}")
    if rows:
        b = max(rows, key=lambda x: x[2]["final"])
        print(f"\nbest: {b[0]} {b[1]} n={b[2]['n']} win={b[2]['win']:.2f}% "
              f"net={b[2]['net_c']:+.2f}c -> ${b[2]['final']:.2f}")
        pos = [x for x in rows if x[2]["net_c"] > 0]
        print(f"{len(rows)} asset-config pairs with n>=5; {len(pos)} positive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
