#!/usr/bin/env python3
"""Test the late-window favourite taker across EVERY Kalshi 15M and 1H series.

Data is real: settled markets from the trade API plus their recorded 1-minute
candlesticks (yes_ask/yes_bid close per minute). Nothing is synthesised.

Entry rule tested (the one that survived 77 days of BTC tick data):
  at minute m of the window, if yes_ask is inside the band, buy YES at that ask
  and hold to settlement. One entry per market, first minute that qualifies.

Usage:
  python3 scripts/kalshi_all_markets.py --fetch 15m --per-series 300
  python3 scripts/kalshi_all_markets.py --report 15m
  python3 scripts/kalshi_all_markets.py --fetch 1h  --per-series 300
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kalshi_client import KalshiClient  # noqa: E402

OUT = Path(os.path.expanduser("~/tmp/opencode/kx"))
FEE = 0.07
START = 104.48


def _ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def series_list(c: KalshiClient, freq: str) -> list[str]:
    ser = c.get_series(limit=400)
    want = {"15m": "fifteen_min", "1h": "hourly", "1d": "daily"}[freq]
    # TEST series are published fixtures, not real markets
    return sorted(x["ticker"] for x in ser
                  if x.get("frequency") == want and "TEST" not in x["ticker"].upper())


def settled_markets(c: KalshiClient, series: str, want: int) -> list[dict]:
    out, cursor = [], ""
    while len(out) < want:
        params = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        try:
            r = c._request("GET", "/markets", params=params)
        except Exception:
            break
        page = r.get("markets", [])
        if not page:
            break
        out.extend(page)
        cursor = r.get("cursor") or ""
        if not cursor:
            break
    return out[:want]


def candles(c: KalshiClient, series: str, ticker: str, a: int, b: int) -> list[dict]:
    for _ in range(3):
        try:
            r = c._request("GET", f"/series/{series}/markets/{ticker}/candlesticks",
                           params={"start_ts": a, "end_ts": b, "period_interval": 1})
            return r.get("candlesticks", [])
        except Exception as e:
            if "404" in str(e):
                return []
            time.sleep(1)
    return []


def _f(d: dict, key: str) -> float | None:
    v = (d.get(key) or {}).get("close_dollars")
    try:
        return float(v) * 100.0 if v is not None else None
    except (TypeError, ValueError):
        return None


def fetch(freq: str, per_series: int) -> None:
    c = KalshiClient()
    OUT.mkdir(parents=True, exist_ok=True)
    out_file = OUT / f"all_{freq}.parquet"
    done = set()
    if out_file.exists():
        done = set(pd.read_parquet(out_file, columns=["ticker"]).ticker.unique())
    rows = []
    series = series_list(c, freq)
    print(f"{len(series)} {freq} series; {len(done)} markets already cached", flush=True)
    for i, s in enumerate(series):
        mk = settled_markets(c, s, per_series)
        got = 0
        for m in mk:
            if m["ticker"] in done:
                continue
            try:
                a, b = _ts(m["open_time"]), _ts(m["close_time"]) + 60
            except (KeyError, ValueError):
                continue
            cs = candles(c, s, m["ticker"], a, b)
            if not cs:
                continue
            got += 1
            for k, cd in enumerate(cs):
                # 1d windows are 1440 candles; only the late entry window matters,
                # so keep a 40-minute tail instead of the whole day.
                if freq == "1d" and k < 1400:
                    continue
                rows.append({
                    "series": s, "ticker": m["ticker"],
                    "minute": k, "end_ts": cd.get("end_period_ts"),
                    "yes_ask": _f(cd, "yes_ask"), "yes_bid": _f(cd, "yes_bid"),
                    "result_yes": m.get("result") == "yes",
                    "strike": m.get("floor_strike"),
                })
        print(f"[{i+1}/{len(series)}] {s}: {len(mk)} settled, {got} with candles, "
              f"{sum(1 for r in rows if r['series'] == s)} rows", flush=True)
        if rows:
            df = pd.DataFrame(rows)
            if out_file.exists():
                df = pd.concat([pd.read_parquet(out_file), df], ignore_index=True)
                df = df.drop_duplicates(["ticker", "minute"])
            df.to_parquet(out_file)
            rows = []
    print(f"done -> {out_file}", flush=True)


def evaluate(df: pd.DataFrame, band: tuple[int, int], win: tuple[int, int],
             slip: float, frac: float) -> dict:
    d = df[(df.minute >= win[0]) & (df.minute <= win[1])]
    d = d[d.yes_ask.between(band[0], band[1])]
    if d.empty:
        return {"n": 0}
    t = d.sort_values(["ticker", "minute"]).groupby("ticker", as_index=False).first()
    p = (t.yes_ask + slip) / 100.0
    p = p.clip(0.01, 0.99)
    fee = FEE * p * (1 - p)
    net = t.result_yes.astype(float) * (1 - p) - (~t.result_yes).astype(float) * p - fee
    bal, peak, dd = START, START, 0.0
    for n in net:
        stake = bal * frac
        bal += stake * n / p.mean()
        peak = max(peak, bal)
        dd = max(dd, 1 - bal / peak)
        if bal <= 1:
            break
    return {"n": len(t), "win": t.result_yes.mean() * 100, "net_c": net.mean() * 100,
            "final": bal, "dd": dd * 100, "fees": float(fee.sum())}


def report(freq: str, slip: float) -> None:
    f = OUT / f"all_{freq}.parquet"
    if not f.exists():
        raise SystemExit(f"no data at {f}; run --fetch {freq} first")
    df = pd.read_parquet(f)
    print(f"{len(df)} rows / {df.ticker.nunique()} markets / {df.series.nunique()} series "
          f"({freq})", flush=True)
    win, full = {"15m": ((13, 14), 14), "1h": ((55, 59), 59),
                 "1d": ((1425, 1439), 1439)}[freq]
    cfgs = [((98, 99), win), ((97, 99), win), ((95, 99), win), ((98, 99), (0, full))]
    print(f"\n{'series':22} {'cfg':16} {'n':>5} {'win%':>6} {'net_c':>7} {'final$':>10} {'dd':>6}")
    agg = []
    for s, g in df.groupby("series"):
        for band, w in cfgs:
            r = evaluate(g, band, w, slip, 0.30)
            if r.get("n", 0) < 10:
                continue
            agg.append((s, f"{band[0]}-{band[1]} m{w[0]}-{w[1]}", r))
            print(f"{s:22} {f'{band[0]}-{band[1]} m{w[0]}-{w[1]}':16} {r['n']:>5} "
                  f"{r['win']:>6.2f} {r['net_c']:>+7.2f} {r['final']:>10.2f} {r['dd']:>5.1f}%")
    if agg:
        best = sorted(agg, key=lambda x: -x[2]["final"])[:10]
        print("\n-- top 10 by final balance (frac 0.30, ${:.2f} start, {:.1f}c slip) --".format(START, slip))
        for s, cfg, r in best:
            print(f"  {s:22} {cfg:16} n={r['n']:>4} win={r['win']:5.2f}% "
                  f"net={r['net_c']:+.2f}c -> ${r['final']:.2f}")
        pos = [a for a in agg if a[2]["net_c"] > 0]
        print(f"\n{len(agg)} series-config pairs with n>=10; {len(pos)} positive after fees")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", choices=["15m", "1h", "1d"])
    ap.add_argument("--report", choices=["15m", "1h", "1d"])
    ap.add_argument("--per-series", type=int, default=300)
    ap.add_argument("--slip", type=float, default=1.0)
    a = ap.parse_args()
    if a.fetch:
        fetch(a.fetch, a.per_series)
    if a.report:
        report(a.report, a.slip)
    if not a.fetch and not a.report:
        ap.error("need --fetch or --report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
