#!/usr/bin/env python3
"""Taker favourite backtest on the real narutowindy/kalshiBTC15min tick set.

77 real days (2026-06-21..2026-10-17) of Kalshi BTC 15-minute tick data, ~3x the
papylove quote_history horizon, so the favourite-taker edge can be checked on
dates the papylove sweep never saw. Nothing is synthesised: yes_ask / no_ask are
the recorded venue quotes and the win/loss is the recorded settlement.

Settlement derivation (validated 203/203 against papylove's recorded ``result``):
each ticker covers one 15-minute window ending at its last tick; the market
resolves YES when the book's final yes_ask sat at >= 50c (the book converges to
99/1 at expiry). The ticker's encoded HHMM carries a fixed clock offset, so the
window is taken from the data (expiry = max ts per ticker), never from the string.

Fair-value / lag filter (--edges): the underlying is the recorded BRTI index
(Kalshi's own Bitcoin Real-Time settlement index, 1s cadence). P(BRTI_expiry >
strike) is priced from the BRTI spot at entry and realised per-minute volatility
(trailing 15-minute stdev of 1s log returns). A trade is kept only when that fair
value beats the venue's taker ask by >= edge cents after the fee -- the lag edge
the objective asks for.

Usage:
  python scripts/naru_sweep.py --start 104.48 --fracs 0.10,0.70 --edges 0,2
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import ndtr

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from hf_sweep import _compound  # noqa: E402

FEE_RATE = 0.07
KDATA = Path.home() / "tmp/opencode/naru/kalshi"
BDATA = Path.home() / "tmp/opencode/naru/brti"


def all_dates():
    return sorted(p.stem for p in KDATA.glob("*.parquet"))


def usable_dates():
    """Days with dense BRTI coverage, so fair-value runs are not silently dropped."""
    return [d for d in all_dates() if _brti_day(d) is not None]


def _brti_day(date: str):
    """(ts_s, spot, vol_per_min) arrays from one day of BRTI 1s ticks."""
    fp = BDATA / f"{date}.parquet"
    if not fp.exists():
        return None
    b = pd.read_parquet(fp, columns=["ts_ns", "value"])
    b["ts_ns"] = pd.to_numeric(b.ts_ns, errors="coerce")
    b["value"] = pd.to_numeric(b.value, errors="coerce")
    b = b.dropna().sort_values("ts_ns").reset_index(drop=True)
    # Sparse days (a few hundred rows) make the searchsorted lookup clamp to one
    # constant spot for the whole day, which silently prices every market wrong.
    if len(b) < 20000:
        return None
    ret = np.log(b.value.to_numpy(float)).dot(1.0)  # copy
    ret = np.diff(ret, prepend=ret[0])
    sigma1s = pd.Series(ret).rolling(900, min_periods=120).std().to_numpy(float)
    vol_min = sigma1s * np.sqrt(60.0)
    return (b.ts_ns.to_numpy() // 10**9, b.value.to_numpy(float), vol_min)


def _fair(spot, strike, vol_min, minutes_left):
    ok = (spot > 0) & (strike > 0) & (vol_min > 0) & (minutes_left > 0)
    denom = vol_min * np.sqrt(minutes_left)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (np.log(spot / strike) - 0.5 * denom * denom) / denom
    return np.where(ok, ndtr(z), np.nan)


def build_minute_table(attach_fv: bool) -> pd.DataFrame:
    """One row per (ticker, minute): last quote in the minute + settlement (+ fair)."""
    parts = []
    for d in all_dates():
        k = pd.read_parquet(KDATA / f"{d}.parquet",
                            columns=["ticker", "ts_ns", "yes_ask_cents",
                                     "no_ask_cents", "strike_price"]).dropna(subset=["ticker"])
        k["ts_ns"] = pd.to_numeric(k.ts_ns, errors="coerce")
        for c in ["yes_ask_cents", "no_ask_cents", "strike_price"]:
            k[c] = pd.to_numeric(k[c], errors="coerce")
        k = k.dropna(subset=["ts_ns"]).sort_values("ts_ns")
        g = k.groupby("ticker", sort=False)
        k = k.join(g.ts_ns.max().rename("close_ts"), on="ticker")
        k = k.join(g.yes_ask_cents.last().rename("last_ya"), on="ticker")
        k = k.join(g.strike_price.last().rename("strike"), on="ticker")
        k["minute"] = ((k.ts_ns - (k.close_ts - 900 * 10**9)) // (60 * 10**9)).clip(0, 14)
        k["result_yes"] = k.last_ya >= 50
        k = (k.sort_values("ts_ns").groupby(["ticker", "minute"], as_index=False).last())
        k = k[["ticker", "minute", "yes_ask_cents", "no_ask_cents", "strike",
               "close_ts", "result_yes"]]
        k["close_s"] = k.close_ts // 10**9
        k["entry_ts"] = k.close_s - (15 - k.minute) * 60
        if attach_fv:
            br = _brti_day(d)
            if br is None:
                k["fair"] = np.nan
            else:
                bts, bspot, bvol = br
                idx = np.searchsorted(bts, k.entry_ts.to_numpy(), side="right") - 1
                idx = np.clip(idx, 0, len(bspot) - 1)
                spot = bspot[idx]
                vol = bvol[idx]
                ml = (15 - k.minute.to_numpy()).clip(0.5, None)
                k["fair"] = _fair(spot, k.strike.to_numpy(float), vol, ml)
        parts.append(k)
    mt = pd.concat(parts, ignore_index=True)
    return mt


def trades(mt: pd.DataFrame, bmin, bmax, wstart, wend, sides,
           edge=None) -> pd.DataFrame:
    m = mt[(mt.minute >= wstart) & (mt.minute <= wend)]
    if edge is not None:
        m = m[m.fair.notna()]
    frames = []
    if "yes" in sides:
        p = m.yes_ask_cents / 100.0
        fee = FEE_RATE * p * (1.0 - p)
        keep = m.yes_ask_cents.between(bmin, bmax)
        if edge is not None:
            keep &= ((m.fair - p - fee) * 100.0 >= edge)
        f = m[keep].assign(side="yes", price=p[keep], win=m.result_yes[keep])
        frames.append(f)
    if "no" in sides:
        p = m.no_ask_cents / 100.0
        fee = FEE_RATE * p * (1.0 - p)
        keep = m.no_ask_cents.between(bmin, bmax)
        if edge is not None:
            keep &= (((1.0 - m.fair) - p - fee) * 100.0 >= edge)
        f = m[keep].assign(side="no", price=p[keep], win=~m.result_yes[keep])
        frames.append(f)
    if not frames:
        return pd.DataFrame()
    t = pd.concat(frames, ignore_index=True)
    t = t[t.price.gt(0) & t.price.lt(1)]
    if t.empty:
        return pd.DataFrame()
    t = t.sort_values(["ticker", "minute"]).groupby("ticker", as_index=False).first()
    t["fee"] = FEE_RATE * t.price * (1.0 - t.price)
    t["yes_win"] = t.win.astype(bool)
    t["net"] = t.yes_win.astype(float) * (1.0 - t.price) \
        - (~t.yes_win).astype(float) * t.price - t.fee
    return t[["ticker", "entry_ts", "close_s", "price", "yes_win", "fee", "net"]]


def evaluate(t: pd.DataFrame, start: float, frac: float) -> dict:
    comp = _compound(t.rename(columns={"close_s": "close_ts"}), start, frac)
    n = len(t)
    ad = int(pd.Series(pd.to_datetime(t.entry_ts, unit="s")).dt.date.nunique()) if n else 0
    return {"n": n, "winrate": float(t.yes_win.mean()) if n else 0.0,
            "mean_net_c": float(t.net.mean()) * 100.0 if n else 0.0,
            "final": comp["final"], "maxdd": comp["maxdd"], "taken": comp["taken"],
            "x": comp["final"] / start, "activedays": ad}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=104.48)
    ap.add_argument("--fracs", default="0.10,0.35,0.70,0.95")
    ap.add_argument("--edges", default=None,
                    help="comma list of fair-value edge cents; omit for band-only")
    ap.add_argument("--usable-only", action="store_true",
                    help="restrict to days with dense BRTI (like-for-like FV compare)")
    ap.add_argument("--top", type=int, default=40)
    a = ap.parse_args()
    fracs = [float(x) for x in a.fracs.split(",")]
    edges = [float(x) for x in a.edges.split(",")] if a.edges else [None]

    if a.usable_only:
        keep = set(usable_dates())
        all_dates = lambda: sorted(keep)  # noqa: E731
        globals()["all_dates"] = all_dates
        print(f"usable days: {len(keep)}", flush=True)

    mt = build_minute_table(attach_fv=any(e is not None for e in edges))
    print(f"minute table: {len(mt)} rows / {mt.ticker.nunique()} tickers / "
          f"{len(all_dates())} days", flush=True)

    bands = [(90, 99), (95, 99), (96, 99), (97, 99), (97, 98), (95, 98), (98, 99)]
    windows = [(0, 15), (3, 11), (4, 14), (6, 11)]
    sidesets = [("yes",), ("no",), ("yes", "no")]
    rows = []
    for edge in edges:
        for (b0, b1) in bands:
            for (w0, w1) in windows:
                for sides in sidesets:
                    t = trades(mt, b0, b1, w0, w1, sides, edge=edge)
                    if t.empty:
                        continue
                    for f in fracs:
                        r = evaluate(t, a.start, f)
                        r["cfg"] = (b0, b1, w0, w1, "+".join(sides),
                                    "band" if edge is None else f"e{edge:g}", f)
                        rows.append(r)
    rows.sort(key=lambda r: -r["final"])
    hdr = (f"{'band':>7} {'win':>6} {'side':>7} {'filter':>6} {'frac':>4} {'n':>6} "
           f"{'win%':>5} {'net_c':>6} {'final$':>11} {'x':>7} {'maxdd':>6} {'act':>4}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows[:a.top]:
        b0, b1, w0, w1, side, filt, f = r["cfg"]
        print(f"{b0:>3}-{b1:<3} {w0:>2}-{w1:<3} {side:>7} {filt:>6} {f:>4.2f} "
              f"{r['n']:>6} {r['winrate']*100:>5.1f} {r['mean_net_c']:>+6.3f} "
              f"{r['final']:>11.2f} {r['x']:>7.2f} {r['maxdd']*100:>5.1f}% "
              f"{r['activedays']:>4}")
    assert rows, "no naru configs traded"
    best = rows[0]
    print(f"\nbest: band {best['cfg'][0]}-{best['cfg'][1]} win {best['cfg'][2]}-"
          f"{best['cfg'][3]} {best['cfg'][4]} {best['cfg'][5]} frac {best['cfg'][6]} "
          f"-> ${best['final']:.2f} ({best['x']:.2f}x)")


if __name__ == "__main__":
    main()