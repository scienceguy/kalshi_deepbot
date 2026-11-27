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

Reality tax uses the production fee/cash helpers for every series. Quotes alone
produce per-contract observations; compounding requires recorded depth per row.

Usage:
  python scripts/naru_tick.py --edges 0,1,2,3 --fracs 0.35,0.70 --slip 0,1
"""
import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import naru_sweep as N  # noqa: E402

COLS = ["ticker", "ts_ns", "yes_ask_cents", "no_ask_cents", "strike_price", "result"]
DEPTH_COLS = ("depth", "yes_ask_depth", "no_ask_depth", "depth_ts_ns",
              "received_ts_ns", "venue_ts_ms")


def ticks_for_day(date: str) -> pd.DataFrame | None:
    """Every book tick of one day, with fair value attached at the tick instant."""
    fp = N.KDATA / f"{date}.parquet"
    br = N._brti_day(date)
    if br is None or not fp.exists():
        return None
    bts, bspot, bvol = br

    try:
        import pyarrow.parquet as pq
        available = set(pq.read_schema(fp).names)
        depth_cols = [c for c in DEPTH_COLS if c in available]
        k = pd.read_parquet(fp, columns=COLS + depth_cols).dropna(subset=["ticker"])
    except (KeyError, ValueError):
        return None  # official outcomes are mandatory
    k["ts_ns"] = pd.to_numeric(k.ts_ns, errors="coerce")
    for c in ["yes_ask_cents", "no_ask_cents", "strike_price"]:
        k[c] = pd.to_numeric(k[c], errors="coerce")
    k = k.dropna(subset=["ts_ns", "yes_ask_cents", "no_ask_cents"]).sort_values("ts_ns")
    if k.empty:
        return None

    k["official_result"] = k.result.map(N._result_yes)
    g = k.groupby("ticker", sort=False)
    close = g.ts_ns.max().rename("close_ts")
    strike = g.strike_price.last().rename("strike")
    outcome = g.official_result.last().rename("result_yes")
    k = k.join(close, on="ticker").join(strike, on="ticker").join(outcome, on="ticker")
    k = k[(k.close_ts > k.ts_ns) & k.result_yes.notna()]

    sec = (k.ts_ns.to_numpy() // 10**9).astype(np.int64)
    idx = N._bounded_previous_indices(bts, sec)
    valid = idx >= 0
    safe = np.clip(idx, 0, len(bspot) - 1)
    spot = np.full(len(k), np.nan)
    vol = np.full(len(k), np.nan)
    spot[valid] = bspot[safe[valid]]
    vol[valid] = bvol[safe[valid]]
    ml = (k.close_ts.to_numpy() - k.ts_ns.to_numpy()) / 6e10  # minutes left
    k["fair"] = N._fair(spot, k.strike.to_numpy(float), vol, ml)
    return k[["ticker", "ts_ns", "yes_ask_cents", "no_ask_cents", "fair",
              "close_ts", "result_yes"] + depth_cols]


def first_entries(k: pd.DataFrame, bmin: int, bmax: int, edge: float,
                  side: str = "yes") -> pd.DataFrame:
    """Earliest tick per ticker whose fair value clears the ask plus fee plus edge."""
    q = k.yes_ask_cents if side == "yes" else k.no_ask_cents
    p = q / 100.0
    fee = N._entry_fees(k.ticker.to_numpy(), p.to_numpy())
    fv = k.fair if side == "yes" else (1.0 - k.fair)
    keep = q.between(bmin, bmax) & fv.notna() & (((fv - p - fee) * 100.0) >= edge)
    sel = k[keep]
    if sel.empty:
        return sel
    sel = sel.sort_values("ts_ns").drop_duplicates("ticker", keep="first")
    p = (sel.yes_ask_cents if side == "yes" else sel.no_ask_cents) / 100.0
    win = sel.result_yes if side == "yes" else ~sel.result_yes
    out = pd.DataFrame({
        "ticker": sel.ticker, "entry_ts": sel.ts_ns // 10**9,
        "close_s": sel.close_ts // 10**9, "price": p.to_numpy(),
        "yes_win": win.to_numpy(),
    })
    if "depth" in sel:
        out["depth"] = sel.depth.to_numpy()
    else:
        depth_col = "yes_ask_depth" if side == "yes" else "no_ask_depth"
        if depth_col in sel:
            out["depth"] = sel[depth_col].to_numpy()
    for col in ("yes_ask_depth", "no_ask_depth", "depth_ts_ns",
                "received_ts_ns", "venue_ts_ms"):
        if col in sel:
            out[col] = sel[col].to_numpy()
    out["fee"] = N._entry_fees(out.ticker.to_numpy(), out.price.to_numpy())
    out["net"] = [N._settlement_net(row.ticker, row.price, row.yes_win)
                  for row in out.itertuples()]
    return out


def compound(t: pd.DataFrame, start: float, frac: float, slip: float = 0.0) -> dict:
    """Cash-limited fills only when every row has recorded executable depth."""
    if "depth" not in t:
        return {"final": None, "maxdd": None, "fees": 0.0, "fills": 0}
    if t.empty:
        return {"final": start, "maxdd": 0.0, "fees": 0.0, "fills": 0}
    trades = [(r.entry_ts, r.close_s, float(r.price), bool(r.yes_win), r.ticker, r.depth)
              for r in t.itertuples()]
    ev = []
    for i, (entry_ts, close_s, price, win, ticker, depth) in enumerate(trades):
        ev.append((close_s, 0, i))
        ev.append((entry_ts, 1, i))
    ev.sort(key=lambda x: (x[0], x[1]))  # settlements fund same-time entries
    bal, peak, maxdd, fees = start, start, 0.0, 0.0
    fills = 0
    step = slip / 100.0
    pos = {}
    for ts, kind, i in ev:
        entry_ts, close_s, price, win, ticker, depth = trades[i]
        if kind == 1:
            if depth is None or not math.isfinite(float(depth)) or depth <= 0:
                continue
            price = min(0.99, price + step)
            stake = min(frac * bal, bal)
            contracts = min(stake / price, float(depth))
            buy = N.supported_buy_cash(
                contracts, price, N._series(ticker), depth)
            if buy is None or buy > bal:
                continue
            cost = contracts * price
            fee = buy - cost
            bal -= buy
            fees += fee
            fills += 1
            pos[i] = (contracts, price)
        else:
            contracts, entry_price = pos.pop(i, (0, 0.0))
            if win and contracts:
                bal += N.fees.sell_cash_in(
                    contracts, 1.0, taker=False, series=N._series(ticker))
        peak = max(peak, bal)
        if peak > 0:
            maxdd = max(maxdd, 1.0 - bal / peak)
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
                r = N.evaluate(ent, a.start, f)
                rows.append({"edge": e, "slip": slip, "frac": f, "n": len(ent),
                             "win": ent.yes_win.mean() * 100,
                             "net_c": ent.net.mean() * 100, **r})
    rows.sort(key=lambda r: -r["net_c"])
    hdr = (f"{'edge':>5} {'slip':>5} {'frac':>5} {'n':>6} {'win%':>6} {'net_c':>7}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows[:a.top]:
        print(f"{r['edge']:>5.0f} {r['slip']:>5.0f} {r['frac']:>5.2f} {r['n']:>6} "
              f"{r['win']:>6.1f} {r['net_c']:>+7.3f}")
    assert rows, "no tick-level configs traded"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())