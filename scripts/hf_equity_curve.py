#!/usr/bin/env python3
"""Compounding equity curve for the gated Kalshi-15m config on the HF data.

Applies the live-winning gate (band 85-99c, elapsed window 1-10, model edge
>= margin) to fair_value_log joined to settlement, then replays the trades
CHRONOLOGICALLY with the bot's real sizing rules (cash floor 30%, per-trade just
under the floor, $5 hard cap, >=1 contract) so the compounding is visible.

Input: /opt/superbot/hf_data/{fair_value_log,quote_history}/*.parquet
"""
import glob
import json
import math

import polars as pl

ROOT = "/opt/superbot/hf_data"
FEE_RATE = 0.07
START = 100.0
CASH_FLOOR_PCT = 0.30
MAX_TRADE_DOLLARS = 5.0
BAND = (85, 99)
WINDOW = (1, 10)
MARGIN = 0.02  # model P(up)*100 - ask_cents


def fee_cents(px):
    return math.ceil(FEE_RATE * (px / 100.0) * (1 - px / 100.0) * 100 - 1e-9)


def main():
    fv = pl.read_parquet(sorted(glob.glob(ROOT + "/fair_value_log/*.parquet")))
    q = (pl.read_parquet(sorted(glob.glob(ROOT + "/quote_history/*.parquet")),
                         columns=["ticker", "minute", "yes_ask", "result"])
           .with_columns((pl.col("yes_ask") * 100.0).alias("ask_c")))
    res = q.group_by("ticker").agg(pl.col("result").first()).with_columns(
        pl.col("result").eq("yes").alias("win"))
    fv = fv.with_columns([
        (pl.col("kalshi_yes_ask") * 100.0).alias("ask_c"),
        (pl.col("fair_value_prob_up") * 100.0).alias("fv_c"),
        (15.0 - pl.col("minutes_remaining")).round().cast(pl.Int64).alias("minute"),
    ])
    j = (fv.join(res, on="ticker", how="inner")
           .filter((pl.col("minute") >= WINDOW[0]) & (pl.col("minute") <= WINDOW[1])
                   & (pl.col("ask_c") >= BAND[0]) & (pl.col("ask_c") <= BAND[1])
                   & ((pl.col("fv_c") - pl.col("ask_c")) >= MARGIN * 100.0))
           .sort(["ticker", "minute"]).group_by("ticker").first()
           .sort(["ts"]))

    bal = START
    peak = START
    curve = []
    n = 0
    for row in j.iter_rows(named=True):
        px = row["ask_c"]
        stake = min(bal * (1 - CASH_FLOOR_PCT), MAX_TRADE_DOLLARS)
        contracts = max(1, int(stake / (px / 100.0)))
        cost = contracts * px / 100.0
        fee = contracts * fee_cents(px) / 100.0
        if row["win"]:
            pnl = contracts * (1 - px / 100.0) - fee
        else:
            pnl = -cost - fee
        bal += pnl
        peak = max(peak, bal)
        n += 1
        if n % 50 == 0 or n <= 5:
            curve.append((n, round(bal, 2)))
    print("gated config band=%s window=%s margin=%.2f" % (BAND, WINDOW, MARGIN))
    print("trades=%d  start=$%.2f  final=$%.2f  (%.1fx)  peak=$%.2f" %
          (n, START, bal, bal / START, peak))
    print("curve (trade#, balance):", curve[-25:])
    if n:
        print("per-trade compounding rate: %.4f%%" % (100.0 * ((bal / START) ** (1.0 / n) - 1)))


if __name__ == "__main__":
    main()
