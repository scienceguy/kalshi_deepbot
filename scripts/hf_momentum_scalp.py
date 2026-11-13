#!/usr/bin/env python3
"""Momentum-scalper backtest on Kalshi 15m (VPS, cached).

A scalp, not a hold-to-settlement: signal from a 1-minute spot return at minute
t (causal), enter at the ask of minute t, exit after k minutes at the bid of the
held side, or hold to settlement when k=0. Both legs pay the taker fee (a scalper
is a two-leg taker by construction).

Signals: the ALT's own spot return, and BTC's spot return (cross-asset momentum /
the "use spot to gauge momentum / lag arbitration" lever).

Input: /opt/superbot/hf_data/{quote_history,spot_history}/*.parquet
"""
import glob
import json

import polars as pl

ROOT = "/opt/superbot/hf_data"
FEE_RATE = 0.07
ALTS = ["ETH", "SOL", "DOGE", "XRP", "HYPE", "ZEC", "NEAR", "BNB"]
THRS = [0.001, 0.002, 0.003, 0.005, 0.01]
HOLDS = [1, 2, 3, 5]             # minutes held before selling at the bid
WINDOWS = [(1, 10), (4, 12)]
BANDS = [(0, 100), (90, 99), (50, 95)]


def fee(px):
    return (FEE_RATE * px * (100.0 - px) / 100.0).ceil()


def load():
    q = pl.read_parquet(sorted(glob.glob(ROOT + "/quote_history/*.parquet")),
                        columns=["coin", "open_ts", "minute", "yes_bid", "yes_ask", "result"])
    q = q.filter(pl.col("coin").is_in(ALTS)).with_columns([
        (pl.col("yes_ask") * 100.0).alias("ya"),
        (pl.col("yes_bid") * 100.0).alias("yb"),
        ((1.0 - pl.col("yes_bid")) * 100.0).alias("na"),   # NO ask
        ((1.0 - pl.col("yes_ask")) * 100.0).alias("nb"),   # NO bid
        (pl.col("open_ts") + pl.col("minute") * 60).alias("ts"),
    ]).sort(["coin", "open_ts", "minute"])
    for k in HOLDS:
        q = q.with_columns([
            pl.col("yb").shift(-k).over(["coin", "open_ts"]).alias("yb%d" % k),
            pl.col("nb").shift(-k).over(["coin", "open_ts"]).alias("nb%d" % k),
        ])
    s = pl.read_parquet(sorted(glob.glob(ROOT + "/spot_history/*.parquet")),
                        columns=["coin", "ts", "close"]).sort(["coin", "ts"])
    s = s.with_columns((pl.col("close").log() - pl.col("close").log().shift(1))
                       .over("coin").alias("ret1"))
    return q, s


def build_entries(j, thr, w0, w1, band):
    e = (j.filter((pl.col("minute") >= w0) & (pl.col("minute") <= w1)
                  & (pl.col("ret1").abs() >= thr))
           .with_columns(pl.when(pl.col("ret1") >= thr).then(pl.lit("yes"))
                           .otherwise(pl.lit("no")).alias("side"),
                         pl.when(pl.col("ret1") >= thr).then(pl.col("ya"))
                           .otherwise(pl.col("na")).alias("entry_c"))
           .filter((pl.col("entry_c") >= band[0]) & (pl.col("entry_c") <= band[1]))
           .sort(["coin", "open_ts", "minute"]).group_by(["coin", "open_ts"]).first())
    return e


def pnl(e, hold):
    if e is None or e.height == 0:
        return None
    win = pl.col("result") == pl.col("side")
    ycol, ncol = "yb%d" % hold, "nb%d" % hold
    ev = pl.when(pl.col("side") == "yes").then(pl.col(ycol)).otherwise(pl.col(ncol))
    e = e.filter(ev.is_not_null())
    if e.height == 0:
        return {"n": 0, "ev_c": 0.0, "win_rate": 0.0, "total_c": 0.0}
    ev = pl.when(pl.col("side") == "yes").then(pl.col(ycol)).otherwise(pl.col(ncol))
    net = ev - pl.col("entry_c") - fee(pl.col("entry_c")) - fee(ev)
    e2 = e.with_columns([net.alias("net"), win.alias("_win")])
    return {"n": e2.height, "ev_c": float(e2["net"].mean()),
            "win_rate": float(e2["_win"].mean()), "total_c": float(e2["net"].sum())}


def main():
    q, s = load()
    print("alt rows:", q.height, flush=True)
    sigs = {
        "alt": s.select("coin", "ts", "ret1"),
        "btc": (s.filter(pl.col("coin") == "BTC").select("ts", pl.col("ret1"))
                  .join(q.select("coin").unique(), how="cross")),
    }
    rows = []
    for who, sig in sigs.items():
        join_on = ["coin", "ts"] if who == "alt" else ["coin", "ts"]
        j = q.join(sig, on=join_on, how="inner")
        for band in BANDS:
            for w in WINDOWS:
                for thr in THRS:
                    e = build_entries(j, thr, w[0], w[1], band)
                    for hold in HOLDS:
                        m = pnl(e, hold)
                        if m and m["n"] >= 50:
                            rows.append({"sig": who, "band": list(band), "window": list(w),
                                         "thr": thr, "hold": hold, **m})
    rows.sort(key=lambda r: -r["ev_c"])
    json.dump(rows, open("/opt/superbot/hf_momentum_scalp_results.json", "w"), indent=1)
    print("\n=== momentum scalper — top 20 by EV/trade (n>=50) ===")
    print("%4s %9s %7s %6s %5s %6s %8s %7s %9s" %
          ("sig", "band", "win", "thr", "hold", "n", "ev_c", "win%", "tot$"))
    for r in rows[:20]:
        print("%4s %9s %7s %6.4f %5d %6d %8.3f %7.1f %9.1f" % (
            r["sig"], str(r["band"]), str(r["window"]), r["thr"], r["hold"], r["n"],
            r["ev_c"], 100 * r["win_rate"], r["total_c"] / 100))
    pos = [r for r in rows if r["ev_c"] > 0]
    print("\npositive-EV scalper configs (n>=50): %d / %d" % (len(pos), len(rows)))


if __name__ == "__main__":
    main()
