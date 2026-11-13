#!/usr/bin/env python3
"""Tick-level lag-arbitrage backtest on the real Kalshi 15m BTC book.

The minute-level sweep (naru_sweep.py) samples one quote per minute, which
discards the intra-minute latency the strategy is supposed to trade. This
script uses every recorded book tick instead: at each Kalshi quote we look up
the BRTI index print at that instant, price P(BRTI_expiry > strike) from the
trailing realised 1s volatility, and enter the first tick whose fair value
beats the venue ask by the required edge after the Kalshi taker fee.

That measures the actual lag between the settlement index and the venue's
quote, which is where the edge either exists or does not.

Reality tax: taker fee 0.07*C*P*(1-P) (Kalshi's published schedule, rounded up
per order), fills at the recorded ask, optional --slip cents for adverse fills.

Usage:
  python scripts/naru_tick.py --edges 0,1,2,3 --fracs 0.35,0.70 --slip 0,1
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import naru_sweep as N  # noqa: E402

COLS = ["ticker", "ts_ns", "yes_ask_cents", "no_ask_cents", "strike_price"]


def ticks_for_day(date: str) -> pd.DataFrame | None:
    """Every book tick of one day, with fair value attached at the tick instant."""
    fp = N.KDATA / f"{date}.parquet"
    br = N._brti_day(date)
    if br is None or not fp.exists():
        return None
    bts, bspot, bvol = br

    k = pd.read_parquet(fp, columns=COLS).dropna(subset=["ticker"])
    k["ts_ns"] = pd.to_numeric(k.ts_ns, errors="coerce")
    for c in ["yes_ask_cents", "no_ask_cents", "strike_price"]:
        k[c] = pd.to_numeric(k[c], errors="coerce")
    k = k.dropna(subset=["ts_ns", "yes_ask_cents", "no_ask_cents"]).sort_values("ts_ns")
    if k.empty:
        return None

    g = k.groupby("ticker", sort=False)
    close = g.ts_ns.max().rename("close_ts")
    last_ya = g.yes_ask_cents.last().rename("last_ya")
    strike = g.strike_price.last().rename("strike")
    k = k.join(close, on="ticker").join(last_ya, on="ticker").join(strike, on="ticker")
    k = k[k.close_ts > k.ts_ns]  # drop ticks at/after own expiry

    sec = (k.ts_ns.to_numpy() // 10**9).astype(np.int64)
    idx = np.searchsorted(bts, sec, side="right") - 1
    idx = np.clip(idx, 0, len(bspot) - 1)
    spot = bspot[idx]
    vol = bvol[idx]
    ml = (k.close_ts.to_numpy() - k.ts_ns.to_numpy()) / 6e10  # minutes left
    k["fair"] = N._fair(spot, k.strike.to_numpy(float), vol, ml)
    k["result_yes"] = k.last_ya >= 50
    return k[["ticker", "ts_ns", "yes_ask_cents", "no_ask_cents", "fair",
              "close_ts", "result_yes"]]


def first_entries(k: pd.DataFrame, bmin: int, bmax: int, edge: float,
                  side: str = "yes") -> pd.DataFrame:
    """Earliest tick per ticker whose fair value clears the ask plus fee plus edge."""
    q = k.yes_ask_cents if side == "yes" else k.no_ask_cents
    p = q / 100.0
    fee = 0.07 * p * (1.0 - p)
    fv = k.fair if side == "yes" else (1.0 - k.fair)
    keep = q.between(bmin, bmax) & fv.notna() & (((fv - p - fee) * 100.0) >= edge)
    sel = k[keep]
    if sel.empty:
        return sel
    sel = sel.sort_values("ts_ns").groupby("ticker", as_index=False).first()
    p = (sel.yes_ask_cents if side == "yes" else sel.no_ask_cents) / 100.0
    win = sel.result_yes if side == "yes" else ~sel.result_yes
    out = pd.DataFrame({
        "ticker": sel.ticker, "entry_ts": sel.ts_ns // 10**9,
        "close_s": sel.close_ts // 10**9, "price": p.to_numpy(),
        "yes_win": win.to_numpy(),
    })
    out["fee"] = 0.07 * out.price * (1.0 - out.price)
    out["net"] = out.yes_win.astype(float) * (1.0 - out.price) \
        - (~out.yes_win).astype(float) * out.price - out.fee
    return out


def compound(t: pd.DataFrame, start: float, frac: float, slip: float = 0.0) -> dict:
    """Event-driven, cash-limited compounding with integer contracts (Kalshi reality).

    Entry: stake = frac * free cash, contracts = floor(stake/price), cash -= cost.
    Settlement: credit $1/contract on a win, $0 on a loss. Skipped when
    free cash cannot buy one contract.
    """
    if t.empty:
        return {"final": start, "maxdd": 0.0, "fees": 0.0, "fills": 0}
    trades = [(r.close_s, float(r.price), bool(r.yes_win)) for r in t.itertuples()]
    ev = []
    for i, (c_ts, price, win) in enumerate(trades):
        ev.append((c_ts, 0, i))
        ev.append((c_ts + 1, 1, i))  # settlement immediately after close in this model
    ev.sort(key=lambda x: (x[0], x[1]))
    bal, peak, maxdd, fees = start, start, 0.0, 0.0
    fills = 0
    step = slip / 100.0
    pos = {}
    for ts, kind, i in ev:
        c_ts, price, win = trades[i]
        if kind == 0:
            price = min(0.99, price + step)
            stake = min(frac * bal, bal)
            contracts = int(stake / price)
            if contracts < 1:
                continue
            cost = contracts * price
            fee = contracts * 0.07 * price * (1.0 - price)
            if cost + fee > bal:
                continue
            bal -= cost + fee
            fees += fee
            fills += 1
            pos[i] = (contracts, price)
        else:
            contracts, entry_price = pos.pop(i, (0, 0.0))
            if win and contracts:
                bal += contracts * 1.0
        peak = max(peak, bal)
        if peak > 0:
            maxdd = max(maxdd, 1.0 - bal / peak)
        if bal < 1.0:
            break
    return {"final": bal, "maxdd": maxdd, "fees": fees, "fills": fills}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=104.48)
    ap.add_argument("--edges", default="0,1,2,3")
    ap.add_argument("--fracs", default="0.35,0.70")
    ap.add_argument("--slips", default="0,1")
    ap.add_argument("--band", default="95,99")
    ap.add_argument("--target", type=float, default=10000.0)
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()

    b0, b1 = (int(x) for x in a.band.split(","))
    edges = [float(x) for x in a.edges.split(",")]
    fracs = [float(x) for x in a.fracs.split(",")]
    slips = [float(x) for x in a.slips.split(",")]

    days = N.usable_dates()
    frames = []
    for i, d in enumerate(days, 1):
        t = ticks_for_day(d)
        if t is not None and not t.empty:
            frames.append(t)
        if i % 10 == 0:
            print(f"  scanned {i}/{len(days)} days", flush=True)
    allt = pd.concat(frames, ignore_index=True)
    print(f"tick rows: {len(allt)} / {allt.ticker.nunique()} tickers / {len(days)} days",
          flush=True)

    rows = []
    for e in edges:
        ent = first_entries(allt, b0, b1, e, "yes")
        if ent.empty:
            continue
        for slip in slips:
            for f in fracs:
                r = compound(ent, a.start, f, slip)
                rows.append({"edge": e, "slip": slip, "frac": f, "n": len(ent),
                             "win": ent.yes_win.mean() * 100,
                             "net_c": ent.net.mean() * 100, **r,
                             "x": r["final"] / a.start})
    rows.sort(key=lambda r: -r["final"])
    hdr = (f"{'edge':>5} {'slip':>5} {'frac':>5} {'n':>6} {'win%':>6} {'net_c':>7} "
           f"{'final$':>12} {'x':>7} {'maxdd':>6} {'fees$':>9} {'hit':>4}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows[:a.top]:
        print(f"{r['edge']:>5.0f} {r['slip']:>5.0f} {r['frac']:>5.2f} {r['n']:>6} "
              f"{r['win']:>6.1f} {r['net_c']:>+7.3f} {r['final']:>12.2f} "
              f"{r['x']:>7.2f} {r['maxdd']*100:>5.1f}% {r['fees']:>9.2f} "
              f"{'YES' if r['final']>=a.target else 'no':>4}")
    assert rows, "no tick-level configs traded"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())