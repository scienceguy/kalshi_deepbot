#!/usr/bin/env python3
"""Reality-tax sweep over Kalshi 15m quote_history.

For every (band, window, side) it reports two honest entry models:

  taker : cross the ask now, pay the Kalshi quadratic taker fee (ceil to cent).
  maker : rest one cent inside the ask, pay NO fee, but only fill when the ask
          subsequently trades down to our resting price (adverse selection is
          therefore priced in — a favourite that never dips, never fills).

Input:  /opt/superbot/hf_data/quote_history/*.parquet  (prices in DOLLARS 0-1)
Output: ranked JSON + table on stdout.
"""
import glob
import json

import polars as pl

HF = "/opt/superbot/hf_data/quote_history"
FEE_RATE = 0.07
BANDS = [(98, 99), (97, 99), (96, 99), (95, 99), (93, 99), (90, 99), (85, 99),
         (80, 99), (75, 99), (70, 99), (70, 95), (60, 95), (50, 95), (15, 55)]
WINDOWS = [(3, 9), (6, 11), (1, 15), (4, 12), (10, 15), (11, 14), (13, 15), (1, 14)]


def load():
    df = pl.read_parquet(sorted(glob.glob(HF + "/*.parquet")),
                         columns=["ticker", "minute", "yes_bid", "yes_ask", "result"])
    df = df.with_columns([
        (pl.col("yes_bid") * 100.0).alias("yes_bid"),
        (pl.col("yes_ask") * 100.0).alias("yes_ask"),
    ]).with_columns((100.0 - pl.col("yes_bid")).alias("no_ask"))
    # Future asks per ticker: next minute, and min over the next two minutes,
    # so a maker fill can be tested against "informed flow within 1-2 min".
    df = df.sort(["ticker", "minute"]).with_columns([
        pl.col("yes_ask").shift(-1).over("ticker").alias("n1_yes"),
        pl.col("yes_ask").shift(-1).rolling_min(2).over("ticker").alias("n2_yes"),
        pl.col("no_ask").shift(-1).over("ticker").alias("n1_no"),
        pl.col("no_ask").shift(-1).rolling_min(2).over("ticker").alias("n2_no"),
    ])
    return df


def first_entry(df, b0, b1, w0, w1, leg):
    ask = "yes_ask" if leg == "yes" else "no_ask"
    win_val = "yes" if leg == "yes" else "no"
    e = (df.filter((pl.col("minute") >= w0) & (pl.col("minute") <= w1)
                   & (pl.col(ask) >= b0) & (pl.col(ask) <= b1))
           .sort(["ticker", "minute"]).group_by("ticker").first()
           .select("ticker", "result", pl.col(ask).alias("ask"),
                   pl.col("n1_yes").alias("n1y"), pl.col("n2_yes").alias("n2y"),
                   pl.col("n1_no").alias("n1n"), pl.col("n2_no").alias("n2n"))
           .with_columns(pl.col("result").eq(win_val).alias("win")))
    return e


def taker_metrics(e):
    if e is None or e.height == 0:
        return None
    fee = (FEE_RATE * pl.col("ask") * (100.0 - pl.col("ask")) / 100.0).ceil()
    net = pl.when(pl.col("win")).then(100.0).otherwise(0.0) - pl.col("ask") - fee
    e2 = e.with_columns(net.alias("net"))
    return {"n": e2.height, "ev_c": float(e2["net"].mean()),
            "total_c": float(e2["net"].sum()), "win_rate": float(e2["win"].mean())}


def maker_metrics(e, horizon=1, leg="yes"):
    """Rest at ask-1, no fee; fill if the ask reaches our price within `horizon`
    minutes (1 = next minute only, strict informed-flow; 2 = next two minutes)."""
    if e is None or e.height == 0:
        return None
    col = ("n1y" if horizon == 1 else "n2y") if leg == "yes" else ("n1n" if horizon == 1 else "n2n")
    price = pl.max_horizontal(pl.col("ask") - 1.0, pl.lit(1.0))
    filled = (e.with_columns(price.alias("px"))
                .with_columns((pl.col(col).is_not_null() & (pl.col(col) <= pl.col("px"))).alias("fill")))
    n_all = filled.height
    f = filled.filter(pl.col("fill"))
    if f.height == 0:
        return {"n_fill": 0, "fill_rate": 0.0, "ev_c": 0.0, "win_rate_fill": 0.0, "total_c": 0.0}
    net = pl.when(pl.col("win")).then(100.0).otherwise(0.0) - pl.col("px")
    f2 = f.with_columns(net.alias("net"))
    return {"n_fill": f2.height, "fill_rate": f2.height / n_all,
            "ev_c": float(f2["net"].mean()), "win_rate_fill": float(f2["win"].mean()),
            "total_c": float(f2["net"].sum())}


def main():
    df = load()
    print("rows:", df.height, "tickers:", df["ticker"].n_unique(), flush=True)
    rows = []
    for band in BANDS:
        for window in WINDOWS:
            for leg in ("yes", "no"):
                e = first_entry(df, band[0], band[1], window[0], window[1], leg)
                t = taker_metrics(e)
                m1 = maker_metrics(e, horizon=1, leg=leg)
                m2 = maker_metrics(e, horizon=2, leg=leg)
                rows.append({"band": list(band), "window": list(window), "leg": leg,
                             "taker": t, "maker": m1, "maker2": m2})
    json.dump(rows, open("/opt/superbot/hf_reality_results.json", "w"), indent=1)

    def top(key, filt, n=15):
        cand = [r for r in rows if filt(r) and r[key]]
        cand.sort(key=lambda r: -r[key]["ev_c"])
        return cand[:n]

    print("\n=== TAKER (fee paid) — top 15 by EV/trade ===")
    print("%9s %7s %4s %6s %8s %7s %9s" % ("band", "win", "leg", "n", "ev_c", "win%", "tot$"))
    for r in top("taker", lambda r: r["taker"]["n"] > 200):
        t = r["taker"]
        print("%9s %7s %4s %6d %8.3f %7.1f %9.1f" % (
            str(r["band"]), str(r["window"]), r["leg"], t["n"], t["ev_c"],
            100 * t["win_rate"], t["total_c"] / 100))
    for key, label in (("maker", "MAKER fill within 1 min (strict, informed flow)"),
                       ("maker2", "MAKER fill within 2 min")):
        print("\n=== %s — top 12 by EV/trade ===" % label)
        print("%9s %7s %4s %6s %8s %7s %8s" % ("band", "win", "leg", "nfil", "ev_c", "win%", "fill%"))
        for r in top(key, lambda r, k=key: r[k]["n_fill"] > 200):
            m = r[key]
            print("%9s %7s %4s %6d %8.3f %7.1f %8.1f" % (
                str(r["band"]), str(r["window"]), r["leg"], m["n_fill"], m["ev_c"],
                100 * m["win_rate_fill"], 100 * m["fill_rate"]))
    pos_t = [r for r in rows if r["taker"] and r["taker"]["n"] > 200 and r["taker"]["ev_c"] > 0]
    pos_m1 = [r for r in rows if r["maker"] and r["maker"]["n_fill"] > 200 and r["maker"]["ev_c"] > 0]
    pos_m2 = [r for r in rows if r["maker2"] and r["maker2"]["n_fill"] > 200 and r["maker2"]["ev_c"] > 0]
    print("\npositive-EV (n>200): taker %d/%d | maker1min %d/%d | maker2min %d/%d" %
          (len(pos_t), len(rows), len(pos_m1), len(rows), len(pos_m2), len(rows)))


if __name__ == "__main__":
    main()
