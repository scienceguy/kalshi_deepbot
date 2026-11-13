#!/usr/bin/env python3
"""Fair-value / lag-arbitration edge on the real HF Kalshi 15m set.

The edge hypothesis the objective names: the Kalshi quote lags the spot-derived
fair value. Every minute of fair_value_log carries the real Binance spot, the
strike, the realised per-minute volatility and the minutes remaining, alongside
the Kalshi YES bid/ask. So we can price P(YES) ourselves and compare it to the
venue ask. When the ask sits below our fair value by more than the fee we have a
lag edge, and it is a fill-guaranteed taker entry -- no queue model needed.

Nothing is synthesised: spot, strike, vol, minutes, quotes and the settlement
result are all recorded values.

Implementation note: the fair value depends only on (spot, strike, vol, minutes,
vol-scale), not on the band/edge/window filters, so it is computed once per
worker as numpy columns and each config is then a vector filter. (The earlier
per-row Python loop recomputed the same fair value 448 times.)

Usage:
  python scripts/hf_fairvalue.py --jobs 8 --top 30
"""
import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import ndtr

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from hf_sweep import SNAP, _compound  # noqa: E402

FEE_RATE = 0.07
SCALES = [0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0]
_FV: pd.DataFrame | None = None


def _load() -> pd.DataFrame:
    fv = pd.concat([pd.read_parquet(p) for p in
                    sorted((SNAP / "fair_value_log").glob("*.parquet"))],
                   ignore_index=True)
    res = {}
    for p in sorted((SNAP / "quote_history").glob("*.parquet")):
        q = pd.read_parquet(p, columns=["ticker", "result", "close_ts"])
        for tk, r, ct in zip(q.ticker, q.result, q.close_ts):
            r = str(r).strip().lower()
            if r in ("yes", "no"):
                res[tk] = (r, int(ct))
    fv = fv[fv.ticker.isin(res)].copy()
    fv = fv.dropna(subset=["kalshi_yes_bid", "kalshi_yes_ask", "current_price",
                           "strike_price", "volatility_per_min",
                           "minutes_remaining"]).reset_index(drop=True)
    if fv.empty:
        return fv
    fv["result"] = fv.ticker.map(lambda t: res[t][0])
    fv["close_ts"] = fv.ticker.map(lambda t: res[t][1]).astype("int64")
    fv["entry_ts"] = fv.close_ts - (fv.minutes_remaining * 60).astype("int64")
    spot = fv.current_price.to_numpy(float)
    strike = fv.strike_price.to_numpy(float)
    vol = fv.volatility_per_min.to_numpy(float)
    mins = fv.minutes_remaining.to_numpy(float)
    ok = (spot > 0) & (strike > 0) & (vol > 0) & (mins > 0)
    for s in SCALES:
        denom = vol * s * np.sqrt(mins)
        with np.errstate(divide="ignore", invalid="ignore"):
            z = (np.log(spot / strike) - 0.5 * denom * denom) / denom
        col = np.where(ok, ndtr(z), np.nan)
        fv[f"fair_{s}"] = col
    return fv


def _init():
    global _FV
    _FV = _load()


def _run(cfg) -> dict:
    min_edge_c, bmin, bmax, w0, w1, scale, sides, start, frac = cfg
    d = _FV
    d = d[(d.minutes_remaining >= w0) & (d.minutes_remaining <= w1)]
    if d.empty:
        return {"cfg": cfg[:7], "n": 0}
    fair = d[f"fair_{scale}"].to_numpy(float)
    keep = ~np.isnan(fair)
    frames = []
    for side in sides:
        p = d.kalshi_yes_ask.to_numpy(float) if side == "yes" \
            else 1.0 - d.kalshi_yes_bid.to_numpy(float)
        q = fair if side == "yes" else 1.0 - fair
        price_ok = (p * 100 >= bmin) & (p * 100 <= bmax)
        edge = (q - p - FEE_RATE * p * (1.0 - p)) * 100.0
        sel = keep & price_ok & (edge >= min_edge_c)
        if not sel.any():
            continue
        f = d.loc[sel, ["ticker", "minutes_remaining", "entry_ts", "close_ts",
                        "result"]].copy()
        f["side"] = side
        f["price"] = p[sel]
        win = (d.result.to_numpy()[sel] == side)
        f["yes_win"] = win
        f["net"] = np.where(win, 1.0 - f.price, -f.price) \
            - FEE_RATE * f.price * (1.0 - f.price)
        frames.append(f)
    if not frames:
        return {"cfg": cfg[:7], "n": 0}
    t = pd.concat(frames, ignore_index=True)
    t = t.sort_values(["ticker", "minutes_remaining"]).groupby(
        "ticker", as_index=False).first()
    t["fee"] = FEE_RATE * t.price * (1.0 - t.price)
    t["ret_on_stake"] = t.net / t.price
    comp = _compound(t, start, frac)
    return {
        "cfg": cfg[:7], "n": len(t), "winrate": float(t.yes_win.mean()),
        "mean_net_c": float(t.net.mean()) * 100.0,
        "mean_ret_pct": float(t.ret_on_stake.mean()) * 100.0,
        "final": comp["final"], "maxdd": comp["maxdd"], "taken": comp["taken"],
        "x": comp["final"] / start, "frac": frac,
        "activedays": int(pd.Series(t.entry_ts).floordiv(86400).nunique()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=104.48)
    ap.add_argument("--frac", type=float, default=0.08)
    ap.add_argument("--fracs", default=None,
                    help="comma list of stake fractions, overrides --frac")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--top", type=int, default=30)
    a = ap.parse_args()
    fracs = [float(x) for x in a.fracs.split(",")] if a.fracs else [a.frac]

    edges = [0.0, 0.5, 1.0, 2.0]
    bands = [(90, 99), (95, 99), (96, 99), (97, 99), (95, 98),
             (85, 99), (70, 99)]
    windows = [(4, 14), (3, 11), (6, 11), (0, 15)]
    cfgs = [(e, b0, b1, w0, w1, s, ("yes", "no"), a.start, f)
            for e in edges for (b0, b1) in bands for (w0, w1) in windows
            for s in SCALES for f in fracs]
    print(f"fair-value sweep: {len(cfgs)} configs over 13 real days, "
          f"start=${a.start}, fracs={fracs}", flush=True)

    with ProcessPoolExecutor(max_workers=a.jobs, initializer=_init) as ex:
        rows = list(ex.map(_run, cfgs, chunksize=2))
    rows = [r for r in rows if r.get("n")]
    rows.sort(key=lambda r: -r["final"])
    hdr = (f"{'edge':>4} {'band':>7} {'win':>7} {'vscale':>6} {'frac':>4} {'n':>5} "
           f"{'win%':>5} {'net_c':>6} {'ret%':>6} {'final$':>10} {'x':>6} "
           f"{'maxdd':>6} {'act':>4}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows[:a.top]:
        e, b0, b1, w0, w1, s, _sides = r["cfg"]
        print(f"{e:>4.1f} {b0:>3}-{b1:<3} {w0:>2}-{w1:<4} {s:>6.2f} "
              f"{r['frac']:>4.2f} {r['n']:>5} "
              f"{r['winrate']*100:>5.1f} {r['mean_net_c']:>+6.3f} "
              f"{r['mean_ret_pct']:>+6.3f} {r['final']:>10.2f} {r['x']:>6.2f} "
              f"{r['maxdd']*100:>5.1f}% {r['activedays']:>4}")
    assert rows, "no fair-value configs traded"
    best = rows[0]
    print(f"\nbest: edge>={best['cfg'][0]}c band {best['cfg'][1]}-{best['cfg'][2]} "
          f"window {best['cfg'][3]}-{best['cfg'][4]} vscale {best['cfg'][5]} "
          f"-> ${best['final']:.2f} ({best['x']:.2f}x)")


if __name__ == "__main__":
    main()
