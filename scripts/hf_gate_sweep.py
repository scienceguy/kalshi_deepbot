#!/usr/bin/env python3
"""Gate sweep: does the fair-value model turn the negative favourite band positive?

Joins fair_value_log (model P(up), calibrated volatility_per_min) to the realised
settlement result from quote_history, then enters the first minute in the window
where the ask is in band AND the model edge (P(up)*100 - ask_cents) clears a
margin. Reports taker (fee paid) and maker (ask-1, no fee, honest 1-min fill).

Input: /opt/superbot/hf_data/{fair_value_log,quote_history}/*.parquet
"""
import glob
import json

import polars as pl

ROOT = "/opt/superbot/hf_data"
FEE_RATE = 0.07
BANDS = [(98, 99), (97, 99), (95, 99), (93, 99), (90, 99), (85, 99), (75, 99)]
WINDOWS = [(1, 10), (3, 9), (4, 12), (6, 11), (0, 5)]
MARGINS = [0.0, 0.005, 0.01, 0.02, 0.03, 0.05, 0.08]


def load():
    fv = pl.read_parquet(sorted(glob.glob(ROOT + "/fair_value_log/*.parquet")))
    q = (pl.read_parquet(sorted(glob.glob(ROOT + "/quote_history/*.parquet")),
                         columns=["ticker", "minute", "yes_ask", "yes_bid", "result"])
           .with_columns([(pl.col("yes_ask") * 100.0).alias("ask_c"),
                          (pl.col("yes_bid") * 100.0).alias("bid_c")]))
    # next-minute ask per ticker for the maker fill test
    q = q.sort(["ticker", "minute"]).with_columns(
        pl.col("ask_c").shift(-1).over("ticker").alias("n1_ask"))
    res = q.group_by("ticker").agg(pl.col("result").first()).with_columns(
        pl.col("result").eq("yes").alias("win"))
    fv = fv.with_columns([
        (pl.col("kalshi_yes_ask") * 100.0).alias("ask_c"),
        (pl.col("fair_value_prob_up") * 100.0).alias("fv_c"),
        (15.0 - pl.col("minutes_remaining")).round().cast(pl.Int64).alias("minute"),
    ])
    # attach result/win + next ask by ticker+minute
    j = fv.join(res, on="ticker", how="inner")
    qk = q.select("ticker", "minute", "n1_ask", pl.col("ask_c").alias("q_ask"))
    j = j.join(qk, on=["ticker", "minute"], how="left")
    return j


def eval_cfg(j, b0, b1, w0, w1, margin, mode):
    e = (j.filter((pl.col("minute") >= w0) & (pl.col("minute") <= w1)
                  & (pl.col("ask_c") >= b0) & (pl.col("ask_c") <= b1)
                  & ((pl.col("fv_c") - pl.col("ask_c")) >= margin * 100.0))
           .sort(["ticker", "minute"]).group_by("ticker").first())
    if e.height == 0:
        return None
    payout = pl.when(pl.col("win")).then(100.0).otherwise(0.0)
    if mode == "taker":
        px = pl.col("ask_c")
        fee = (FEE_RATE * px * (100.0 - px) / 100.0).ceil()
        net = payout - px - fee
        e2 = e.with_columns(net.alias("net"))
        n = e2.height
    else:  # maker: rest at ask-1, no fee, fill if next ask <= price
        price = pl.max_horizontal(pl.col("ask_c") - 1.0, pl.lit(1.0))
        e2 = (e.with_columns(price.alias("px"))
                .filter(pl.col("n1_ask").is_not_null() & (pl.col("n1_ask") <= pl.col("px"))))
        if e2.height == 0:
            return {"n": 0, "fill_rate": 0.0, "ev_c": 0.0, "win_rate": 0.0, "total_c": 0.0}
        e2 = e2.with_columns((payout - pl.col("px")).alias("net"))
        n = e2.height
    return {"n": n, "fill_rate": n / e.height, "ev_c": float(e2["net"].mean()),
            "win_rate": float(e2["win"].mean()), "total_c": float(e2["net"].sum())}


def main():
    j = load()
    print("joined rows:", j.height, "tickers:", j["ticker"].n_unique(),
          "winrate:", round(float(j["win"].mean()), 4), flush=True)
    rows = []
    for band in BANDS:
        for window in WINDOWS:
            for margin in MARGINS:
                for mode in ("taker", "maker"):
                    m = eval_cfg(j, band[0], band[1], window[0], window[1], margin, mode)
                    if m and m["n"] > 0:
                        rows.append({"band": list(band), "window": list(window),
                                     "margin": margin, "mode": mode, **m})
    json.dump(rows, open("/opt/superbot/hf_gate_results.json", "w"), indent=1)
    for mode in ("taker", "maker"):
        sub = [r for r in rows if r["mode"] == mode and r["n"] >= 100]
        sub.sort(key=lambda r: -r["ev_c"])
        print("\n=== %s (gate) — top 15 by EV/trade (n>=100) ===" % mode.upper())
        print("%9s %7s %6s %6s %8s %7s %9s" % ("band", "win", "margin", "n", "ev_c", "win%", "tot$"))
        for r in sub[:15]:
            print("%9s %7s %6.3f %6d %8.3f %7.1f %9.1f" % (
                str(r["band"]), str(r["window"]), r["margin"], r["n"], r["ev_c"],
                100 * r["win_rate"], r["total_c"] / 100))
        pos = [r for r in sub if r["ev_c"] > 0]
        print("positive-EV %s configs (n>=100): %d / %d" % (mode, len(pos), len(sub)))


if __name__ == "__main__":
    main()
