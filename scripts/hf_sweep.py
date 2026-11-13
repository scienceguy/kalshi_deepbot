#!/usr/bin/env python3
"""Vectorized HF sweep over Kalshi 15m quote_history: highest net-EV config.

Reality tax: taker variants pay the Kalshi quadratic taker fee rounded up to the
cent; maker_ask1 rests one cent inside the ask (no fee) and is reported with its
observed fill rate so adverse selection is visible, not assumed away.
Input: /opt/superbot/hf_data/quote_history/*.parquet
Output: ranked JSON + table.
"""
import glob
import json

import polars as pl

HF = "/opt/superbot/hf_data/quote_history"
FEE_RATE = 0.07
BANDS = [(95, 99), (96, 99), (97, 99), (98, 99), (93, 99), (90, 99), (85, 99),
         (80, 99), (75, 99), (70, 99), (70, 95), (60, 95), (50, 95), (15, 55)]
WINDOWS = [(6, 11), (4, 12), (1, 15), (6, 14), (10, 15), (11, 14), (13, 15),
           (1, 14), (3, 9)]
SIDES = ["yes", "no", "either"]
MODES = ["taker_fee", "taker_nofee", "maker_ask1"]


def load():
    df = pl.read_parquet(sorted(glob.glob(HF + "/*.parquet")),
                         columns=["ticker", "minute", "yes_bid", "yes_ask", "result"])
    # Prices in the dataset are DOLLARS (0-1); convert to cents for the sweep.
    df = df.with_columns([
        (pl.col("yes_bid") * 100.0).alias("yes_bid"),
        (pl.col("yes_ask") * 100.0).alias("yes_ask"),
    ])
    df = df.with_columns((100.0 - pl.col("yes_bid")).alias("no_ask"))
    return df


def entries(df, b0, b1, w0, w1, side):
    out = {}
    if side in ("yes", "either"):
        e = (df.filter((pl.col("minute") >= w0) & (pl.col("minute") <= w1)
                       & (pl.col("yes_ask") >= b0) & (pl.col("yes_ask") <= b1))
               .sort(["ticker", "minute"]).group_by("ticker").first()
               .select("ticker", "result", pl.col("yes_ask").alias("px"))
               .with_columns(pl.col("result").eq("yes").alias("win")))
        out["yes"] = e
    if side in ("no", "either"):
        e = (df.filter((pl.col("minute") >= w0) & (pl.col("minute") <= w1)
                       & (pl.col("no_ask") >= b0) & (pl.col("no_ask") <= b1))
               .sort(["ticker", "minute"]).group_by("ticker").first()
               .select("ticker", "result", pl.col("no_ask").alias("px"))
               .with_columns(pl.col("result").eq("no").alias("win")))
        out["no"] = e
    return out


def metrics(e, mode):
    if e is None or e.height == 0:
        return None
    payout = pl.when(pl.col("win")).then(100.0).otherwise(0.0)
    if mode == "maker_ask1":
        px = pl.max_horizontal(pl.col("px") - 1.0, pl.lit(1.0))
        fee = pl.lit(0.0)
    elif mode == "taker_nofee":
        px = pl.col("px")
        fee = pl.lit(0.0)
    else:
        px = pl.col("px")
        fee = (FEE_RATE * pl.col("px") * (100.0 - pl.col("px")) / 100.0).ceil()
    e2 = e.with_columns((payout - px - fee).alias("net"))
    return {"n": e.height, "ev_c": float(e2["net"].mean()),
            "total_c": float(e2["net"].sum()),
            "win_rate": float(e2["win"].mean())}


def main():
    df = load()
    print("rows:", df.height, "tickers:", df["ticker"].n_unique(), flush=True)
    rows = []
    for band in BANDS:
        for window in WINDOWS:
            ent = entries(df, band[0], band[1], window[0], window[1], "either")
            for side in SIDES:
                for mode in MODES:
                    legs = []
                    if side == "yes":
                        legs = [("yes", ent.get("yes"))]
                    elif side == "no":
                        legs = [("no", ent.get("no"))]
                    else:
                        legs = [("yes", ent.get("yes")), ("no", ent.get("no"))]
                    for leg, e in legs:
                        m = metrics(e, mode)
                        if m and m["n"] > 0:
                            rows.append({"band": list(band), "window": list(window),
                                         "side": side, "leg": leg, "mode": mode, **m})
    rows.sort(key=lambda r: -r["ev_c"])
    json.dump(rows, open("/opt/superbot/hf_sweep_results.json", "w"), indent=1)
    print("configs: %d" % len(rows))
    print("%9s %7s %6s %4s %11s %7s %8s %6s %9s" %
          ("band", "win", "side", "leg", "mode", "n", "ev_c", "win%", "tot_usd"))
    for r in rows[:45]:
        print("%9s %7s %6s %4s %11s %7d %8.3f %6.1f %9.1f" %
              (str(r["band"]), str(r["window"]), r["side"], r["leg"], r["mode"],
               r["n"], r["ev_c"], 100 * r["win_rate"], r["total_c"] / 100.0))


if __name__ == "__main__":
    main()
