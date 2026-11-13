#!/usr/bin/env python3
"""Same late-window favourite taker, tested on Polymarket 15M markets.

Data: the real recorded snapshots of Polymarket "Up or Down" binaries
(Solal9/polymarket-crypto-updown-binary) — top-of-book and 5-level depth polled
every 60s. Nothing synthetic.

Entry rule: at minute m of the window, if the Up ask is inside the band, buy Up
at the ask and hold to resolution. One entry per market.

Unlike the Kalshi tick set, this data has ask_depth_usdc, so the same run also
reports how many dollars were actually offered at that price — i.e. capacity,
the open risk on the Kalshi side.

Usage:
  python3 scripts/poly_all_markets.py --assets btc,eth,sol,xrp --tf 15m
  python3 scripts/poly_all_markets.py --assets btc --tf 1h
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pandas as pd

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


def _binance() -> pd.Series:
    """Cached real Binance BTCUSDT 1m closes (unix second -> price)."""
    p = Path(os.path.expanduser("~/tmp/opencode/binance/btcusdt_1m.parquet"))
    bn = pd.read_parquet(p)
    idx = (bn.ts_ns // 10**9).astype("int64")
    return pd.Series(bn.value.to_numpy(float), index=idx.to_numpy()).sort_index()


def _resolve_by_spot(df: pd.DataFrame) -> pd.Series:
    """Up/Down from real Binance spot at the window boundaries.

    Polymarket resolves from the Chainlink BTC/USD stream; Binance spot is used
    here as the resolution proxy because the snapshot file carries no settlement
    field (closed_status is False on every row). The proxy is validated in
    resolve_check() against the markets whose final quote vector is decisive.
    """
    px = _binance()
    def at(ts: int) -> float:
        i = px.index.searchsorted(ts, side="right") - 1
        return float(px.iloc[max(i, 0)])
    ends = (df.window_start_unix + 900).astype("int64") if "window_end" not in df else df.window_end
    up = []
    for s, e in zip(df.window_start_unix.astype("int64"), ends.astype("int64")):
        up.append(at(e) >= at(s))
    return pd.Series(up, index=df.index)


def resolve_check(df: pd.DataFrame) -> None:
    """Agreement between recorded decisive vectors and the Binance proxy."""
    def decisive(s):
        try:
            a = json.loads(s)
            return max(float(a[0]), float(a[1])) > 0.99
        except (TypeError, ValueError, IndexError):
            return False

    d = df[df.outcome_prices.map(decisive)].copy()
    if d.empty:
        print("resolve check: no decisive vectors to compare", flush=True)
        return
    recorded = d.outcome_prices.map(lambda s: float(json.loads(s)[0]) > 0.5)
    proxy = _resolve_by_spot(d)
    agree = (recorded.values == proxy.values).mean()
    print(f"resolve check: {len(d)} decisive snapshots, binance proxy agrees "
          f"{agree * 100:.1f}%", flush=True)


def load(asset: str, tf: str) -> pd.DataFrame | None:
    p = _path(asset, tf)
    if p is None:
        return None
    df = pd.read_csv(p, usecols=COLS, low_memory=False)
    df["t"] = pd.to_datetime(df.collection_timestamp_utc, utc=True, format="mixed")
    # pandas may store us or ns here, so go through total_seconds (unit-safe)
    df["minute"] = ((df.t - pd.Timestamp(0, tz="UTC")).dt.total_seconds()
                    - df.window_start_unix) / 60.0
    df = df.dropna(subset=["best_ask"])
    # resolution: recorded decisive vector where available, else Binance spot
    df["up_won"] = None
    df["up_won"] = _resolve_by_spot(df).astype(object)

    def decisive(s):
        try:
            a = json.loads(s)
            return max(float(a[0]), float(a[1])) > 0.99
        except (TypeError, ValueError, IndexError):
            return False

    rec = df.outcome_prices.map(lambda s: float(json.loads(s)[0]) > 0.5
                                if decisive(s) else None)
    df["up_won"] = rec.where(rec.notna(), df["up_won"])
    return df.dropna(subset=["up_won"])


def run(df: pd.DataFrame, band: tuple[float, float], win: tuple[float, float],
        slip_c: float, frac: float, fee_rate: float = 0.0) -> dict:
    d = df[(df.minute >= win[0]) & (df.minute <= win[1])
           & (df.best_ask * 100).between(band[0], band[1])]
    if d.empty:
        return {"n": 0}
    t = d.sort_values(["slug", "t"]).groupby("slug", as_index=False).first()
    p = ((t.best_ask * 100 + slip_c) / 100.0).clip(0.01, 1.0)
    fee = fee_rate * p * (1 - p)
    net = t.up_won.astype(float) * (1 - p) - (~t.up_won).astype(float) * p - fee
    bal, peak, dd = START, START, 0.0
    for n, pr in zip(net, p):
        stake = bal * frac
        bal += stake * n / pr
        peak = max(peak, bal)
        dd = max(dd, 1 - bal / peak)
        if bal <= 1:
            break
    return {"n": len(t), "win": t.up_won.mean() * 100, "net_c": net.mean() * 100,
            "final": bal, "dd": dd * 100,
            "cap_usd": float(t.ask_depth_usdc.median() or 0),
            "min_ask": float(t.best_ask.min() * 100)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default="btc,eth,sol,xrp")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--slip", type=float, default=0.0)
    ap.add_argument("--fee", type=float, default=0.0, help="Polymarket taker fee rate (default 0)")
    a = ap.parse_args()
    win = (13.0, 15.0) if a.tf == "15m" else (55.0, 60.0)
    cfgs = [((98, 99.9), win), ((97, 99.9), win), ((95, 99.9), win),
            ((98, 99.9), (0, 99 if a.tf == "1h" else 15))]
    print(f"polymarket {a.tf}: band x window, entry=taker at ask, "
          f"slip {a.slip}c, fee {a.fee:.2f}, frac 0.30, ${START:.2f} start")
    print(f"{'asset':6} {'cfg':18} {'n':>5} {'win%':>6} {'net_c':>7} {'final$':>10} "
          f"{'dd':>6} {'med_depth$':>10}")
    best = []
    for asset in a.assets.split(","):
        df = load(asset.strip(), a.tf)
        if df is None or df.empty:
            print(f"{asset:6} (no data)")
            continue
        for band, w in cfgs:
            r = run(df, band, w, a.slip, 0.30, a.fee)
            if r["n"] < 5:
                continue
            tag = f"{band[0]:g}-{band[1]:g} m{w[0]:g}-{w[1]:g}"
            print(f"{asset:6} {tag:18} {r['n']:>5} {r['win']:>6.2f} {r['net_c']:>+7.2f} "
                  f"{r['final']:>10.2f} {r['dd']:>5.1f}% {r['cap_usd']:>10.0f}")
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
