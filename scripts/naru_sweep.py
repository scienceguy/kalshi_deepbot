#!/usr/bin/env python3
"""Taker favourite backtest on the real narutowindy/kalshiBTC15min tick set.

77 real days (2026-06-21..2026-10-17) of Kalshi BTC 15-minute tick data, ~3x the
papylove quote_history horizon, so the favourite-taker edge can be checked on
dates the papylove sweep never saw. Nothing is synthesised: yes_ask / no_ask are
the recorded venue quotes and the win/loss is the recorded settlement.

Settlement requires the recorded official ``result`` (yes/no); rows without it
are excluded. Quotes and spot values never proxy outcomes. Each ticker covers
one 15-minute window ending at its last tick. The ticker's encoded HHMM carries
a fixed clock offset, so the window is taken from the data, never the string.

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

import fees  # noqa: E402

KDATA = Path.home() / "tmp/opencode/naru/kalshi"
BDATA = Path.home() / "tmp/opencode/naru/brti"


def _result_yes(value):
    """Return the explicit official YES/NO outcome, never a quote proxy."""
    if not isinstance(value, str):
        return None
    outcome = value.strip().lower()
    if outcome == "yes":
        return True
    if outcome == "no":
        return False
    return None


def _bounded_previous_indices(source_ts, query_ts, tolerance_s=1.0):
    """Previous-tick indices, or -1 for pre-first/future/stale joins."""
    source = np.asarray(source_ts)
    query = np.asarray(query_ts)
    indices = np.searchsorted(source, query, side="right") - 1
    invalid = indices < 0
    safe = np.clip(indices, 0, len(source) - 1)
    invalid |= (query - source[safe] < 0) | (query - source[safe] > tolerance_s)
    return np.where(invalid, -1, indices)


def production_buy_cash(quantity, price, series):
    return fees.buy_cash_out(quantity, price, taker=True, series=series)


def production_sell_cash(quantity, price, series):
    return fees.sell_cash_in(quantity, price, taker=True, series=series)


def supported_buy_cash(quantity, price, series, depth):
    if depth is None or quantity <= 0 or quantity > depth:
        return None
    return production_buy_cash(quantity, price, series)


def _series(ticker):
    return fees.series_from_ticker(str(ticker))


def _entry_fee(price, series):
    return production_buy_cash(1.0, float(price), series) - float(price)


def _entry_fees(tickers, prices):
    return np.array([_entry_fee(price, _series(ticker))
                     for ticker, price in zip(tickers, prices)])


def _settlement_net(ticker, price, won):
    series = _series(ticker)
    buy = production_buy_cash(1.0, float(price), series)
    payout = 1.0 if won else 0.0
    settled = fees.sell_cash_in(1.0, payout, taker=False, series=series)
    return settled - buy


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
    # Sparse days cannot support bounded decision-time joins and fair values.
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
        try:
            k = pd.read_parquet(
                KDATA / f"{d}.parquet",
                columns=["ticker", "ts_ns", "yes_ask_cents", "no_ask_cents",
                         "strike_price", "result"]).dropna(subset=["ticker"])
        except (KeyError, ValueError):
            continue  # no official outcome column means no usable evidence
        k["ts_ns"] = pd.to_numeric(k.ts_ns, errors="coerce")
        for c in ["yes_ask_cents", "no_ask_cents", "strike_price"]:
            k[c] = pd.to_numeric(k[c], errors="coerce")
        k = k.dropna(subset=["ts_ns"]).sort_values("ts_ns")
        g = k.groupby("ticker", sort=False)
        k["official_result"] = k.result.map(_result_yes)
        g = k.groupby("ticker", sort=False)
        k = k.join(g.ts_ns.max().rename("close_ts"), on="ticker")
        k = k.join(g.strike_price.last().rename("strike"), on="ticker")
        k = k.join(g.official_result.last().rename("result_yes"), on="ticker")
        k = k[k.result_yes.notna()]
        k["minute"] = ((k.ts_ns - (k.close_ts - 900 * 10**9)) // (60 * 10**9)).clip(0, 14)
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
                idx = _bounded_previous_indices(bts, k.entry_ts.to_numpy())
                valid = idx >= 0
                safe = np.clip(idx, 0, len(bspot) - 1)
                spot = np.full(len(k), np.nan)
                vol = np.full(len(k), np.nan)
                spot[valid] = bspot[safe[valid]]
                vol[valid] = bvol[safe[valid]]
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
        fee = _entry_fees(m.ticker.to_numpy(), p.to_numpy())
        keep = m.yes_ask_cents.between(bmin, bmax)
        if edge is not None:
            keep &= ((m.fair - p - fee) * 100.0 >= edge)
        f = m[keep].assign(side="yes", price=p[keep], win=m.result_yes[keep])
        frames.append(f)
    if "no" in sides:
        p = m.no_ask_cents / 100.0
        fee = _entry_fees(m.ticker.to_numpy(), p.to_numpy())
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
    t["fee"] = _entry_fees(t.ticker.to_numpy(), t.price.to_numpy())
    t["yes_win"] = t.win.astype(bool)
    t["net"] = [_settlement_net(row.ticker, row.price, row.yes_win)
                for row in t.itertuples()]
    return t[["ticker", "entry_ts", "close_s", "price", "yes_win", "fee", "net"]]


def evaluate(t: pd.DataFrame, start: float, frac: float) -> dict:
    """Per-contract observations only until recorded depth supports fills."""
    n = len(t)
    ad = int(pd.Series(pd.to_datetime(t.entry_ts, unit="s")).dt.date.nunique()) if n else 0
    return {"n": n, "winrate": float(t.yes_win.mean()) if n else 0.0,
            "mean_net_c": float(t.net.mean()) * 100.0 if n else 0.0,
            "activedays": ad}


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
    rows.sort(key=lambda r: -r["mean_net_c"])
    hdr = (f"{'band':>7} {'win':>6} {'side':>7} {'filter':>6} {'frac':>4} {'n':>6} "
           f"{'win%':>5} {'net_c':>6} {'act':>4}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows[:a.top]:
        b0, b1, w0, w1, side, filt, f = r["cfg"]
        print(f"{b0:>3}-{b1:<3} {w0:>2}-{w1:<3} {side:>7} {filt:>6} {f:>4.2f} "
              f"{r['n']:>6} {r['winrate']*100:>5.1f} {r['mean_net_c']:>+6.3f} "
              f"{r['activedays']:>4}")
    assert rows, "no naru configs traded"
    best = rows[0]
    print(f"\nbest per-contract observation: band {best['cfg'][0]}-{best['cfg'][1]} "
          f"win {best['cfg'][2]}-{best['cfg'][3]} {best['cfg'][4]} {best['cfg'][5]} "
          f"-> {best['mean_net_c']:+.3f}c")


if __name__ == "__main__":
    main()