#!/usr/bin/env python3
"""Cross-coin lead-lag + spot-momentum backtest on Kalshi 15m (VPS, cached).

Two signals, both causal — the predictor uses minute t-1, the entry is at the
ask of minute t, held to settlement:

  leadlag : BTC's implied-probability move dP_btc(t-1) moves the ALT next (the
            cross-coin residual signal, IC up to +0.335 in the literature).
            dP_btc >= +thr -> alt up -> buy ALT YES at ask; <= -thr -> buy NO.
  spotmom : BTC's 1-minute spot-log-return over the minute ending at entry t
            (spot_history), i.e. "use spot to gauge momentum / lag arbitration".

Every variant pays the Kalshi taker fee (ceil to cent). Baseline = alt YES
favourite band with no cross signal, for comparison.

Input: /opt/superbot/hf_data/{quote_history,spot_history}/*.parquet
"""
import glob
import json

import polars as pl

ROOT = "/opt/superbot/hf_data"
FEE_RATE = 0.07
ALTS = ["ETH", "SOL", "DOGE", "XRP", "HYPE", "ZEC", "NEAR", "BNB"]
BANDS = [(95, 99), (90, 99), (85, 99), (0, 100)]
WINDOWS = [(1, 10), (4, 12), (6, 11)]
PROB_THRS = [0.5, 1.0, 2.0, 3.0, 5.0, 10.0]     # prob cents
SPOT_THRS = [0.0005, 0.001, 0.002, 0.005, 0.01]  # BTC 1-min log return


def load_quotes():
    q = pl.read_parquet(sorted(glob.glob(ROOT + "/quote_history/*.parquet")),
                        columns=["coin", "series", "ticker", "open_ts", "minute",
                                 "yes_bid", "yes_ask", "result"])
    q = q.with_columns([
        ((pl.col("yes_bid") + pl.col("yes_ask")) / 2.0 * 100.0).alias("mid"),
        (pl.col("yes_ask") * 100.0).alias("ask_c"),
        ((1.0 - pl.col("yes_bid")) * 100.0).alias("no_ask_c"),
    ]).sort(["coin", "open_ts", "minute"])
    btc = (q.filter(pl.col("coin") == "BTC")
             .select("open_ts", "minute", pl.col("mid").alias("btc_mid"))
             .with_columns((pl.col("btc_mid") - pl.col("btc_mid").shift(1)).over("open_ts").alias("d")).
             with_columns(pl.col("d").shift(1).over("open_ts").alias("value")).
             select("open_ts", "minute", "value"))
    return q, btc


def load_spot_btc():
    s = pl.read_parquet(sorted(glob.glob(ROOT + "/spot_history/*.parquet")),
                        columns=["coin", "ts", "close"])
    s = (s.filter(pl.col("coin") == "BTC").sort("ts")
           .with_columns((pl.col("close").log() - pl.col("close").log().shift(1)).alias("ret1")))
    return s.select("ts", pl.col("ret1").alias("dS_btc"))


def fee(px):
    return (FEE_RATE * px * (100.0 - px) / 100.0).ceil()


def pnl(e):
    if e is None or e.height == 0:
        return None
    win = pl.col("result") == pl.col("side")
    payout = pl.when(win).then(100.0).otherwise(0.0)
    net = payout - pl.col("entry_c") - fee(pl.col("entry_c"))
    e2 = e.with_columns([net.alias("net"), win.alias("_win")])
    return {"n": e2.height, "ev_c": float(e2["net"].mean()),
            "win_rate": float(e2["_win"].mean()), "total_c": float(e2["net"].sum())}


def entries(alts, sig, thr, w0, w1, band):
    """sig: DataFrame(open_ts, minute, value). One entry per (open_ts, coin)."""
    j = alts.join(sig, on=["open_ts", "minute"], how="inner")
    e = (j.filter((pl.col("minute") >= w0) & (pl.col("minute") <= w1)
                  & (pl.col("value").abs() >= thr))
           .with_columns(pl.when(pl.col("value") >= thr).then(pl.lit("yes"))
                           .otherwise(pl.lit("no")).alias("side"),
                         pl.when(pl.col("value") >= thr).then(pl.col("ask_c"))
                           .otherwise(pl.col("no_ask_c")).alias("entry_c"))
           .filter((pl.col("entry_c") >= band[0]) & (pl.col("entry_c") <= band[1]))
           .sort(["open_ts", "coin", "minute"]).group_by(["open_ts", "coin"]).first())
    return e


def main():
    q, btc = load_quotes()
    alts = q.filter(pl.col("coin").is_in(ALTS))
    print("quote rows:", q.height, "alt rows:", alts.height,
          "alt windows:", alts.select(["open_ts", "coin"]).unique().height, flush=True)

    base = (alts.filter((pl.col("ask_c") >= 90) & (pl.col("ask_c") <= 99)
                        & (pl.col("minute") >= 1) & (pl.col("minute") <= 10))
                .sort(["coin", "open_ts", "minute"]).group_by(["coin", "open_ts"]).first()
                .with_columns([pl.lit("yes").alias("side"), pl.col("ask_c").alias("entry_c")]))
    b = pnl(base)
    print("BASELINE alt YES 90-99 elapsed1-10: ev=%.3fc n=%d win=%.1f%% total=$%.2f"
          % (b["ev_c"], b["n"], 100 * b["win_rate"], b["total_c"] / 100))

    rows = []
    # lead-lag
    for band in BANDS:
        for w in WINDOWS:
            for thr in PROB_THRS:
                m = pnl(entries(alts, btc, thr, w[0], w[1], band))
                if m and m["n"] >= 50:
                    rows.append({"sig": "leadlag", "band": list(band), "window": list(w),
                                 "thr": thr, **m})
    # spot momentum: join spot by ts = open_ts + minute*60
    sbtc = load_spot_btc()
    alts_ts = alts.with_columns((pl.col("open_ts") + pl.col("minute") * 60).alias("ts"))
    sig_spot = (alts_ts.select("open_ts", "minute", "ts")
                       .join(sbtc, on="ts", how="inner")
                       .select("open_ts", "minute", pl.col("dS_btc").alias("value")))
    for band in BANDS:
        for w in WINDOWS:
            for thr in SPOT_THRS:
                m = pnl(entries(alts_ts, sig_spot, thr, w[0], w[1], band))
                if m and m["n"] >= 50:
                    rows.append({"sig": "spotmom", "band": list(band), "window": list(w),
                                 "thr": thr, **m})
    rows.sort(key=lambda r: -r["ev_c"])
    json.dump(rows, open("/opt/superbot/hf_lag_arb_results.json", "w"), indent=1)
    print("\n=== lead-lag / spot-momentum — top 22 by EV/trade (n>=50) ===")
    print("%8s %9s %7s %6s %6s %8s %7s %9s" %
          ("sig", "band", "win", "thr", "n", "ev_c", "win%", "tot$"))
    for r in rows[:22]:
        print("%8s %9s %7s %6.4f %6d %8.3f %7.1f %9.1f" % (
            r["sig"], str(r["band"]), str(r["window"]), r["thr"], r["n"], r["ev_c"],
            100 * r["win_rate"], r["total_c"] / 100))
    for s in ("leadlag", "spotmom"):
        sub = [r for r in rows if r["sig"] == s]
        pos = [r for r in sub if r["ev_c"] > 0]
        print("positive-EV %s (n>=50): %d / %d" % (s, len(pos), len(sub)))


if __name__ == "__main__":
    main()
