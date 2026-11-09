#!/usr/bin/env python3
"""Measure the realized win rate of Kalshi 15M favourites by price band.

Why this exists: gates.py gate 3 scores a trade with a driftless Brownian
model, so inside its own band it returns q of roughly the venue price and
gate 2's net EV lands near zero by construction. Nothing in that model can
say whether a favourite at 97c is worth buying, because the model has no
opinion about the venue being right or wrong. The only way to answer that is
to look at what favourites actually did: hold the quote to settlement.

Input: a sqlite file with two tables (any collector that writes them):

    markets(ticker, series, open_ts, close_ts, result)   result: yes|no
    candles(ticker, end_ts, yes_bid, yes_ask)    prices in dollars

Output: for each price band and entry window, the number of settled quotes at
that price, the win rate, the average price paid, the net EV per contract
after the taker fee, and whether the 95% lower bound on the win rate clears
breakeven. Only then does a band have a measured edge.

    python3 scripts/measure_edge.py
    python3 scripts/measure_edge.py --db path/to/candles.sqlite --minutes 6 11

The numbers this prints are the ones quoted in the config.py comment above
GATES_MEASURED_Q. If the input data changes, re-run it and update that
comment; do not edit the constant by hand.
"""
import argparse
import math
import sqlite3

FEE_MULTIPLIER = 0.07  # Kalshi taker: 0.07 * p * (1 - p) per contract

BANDS = (
    (0.02, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40),
    (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80),
    (0.80, 0.90), (0.90, 0.95), (0.95, 0.99), (0.99, 1.00),
)


def taker_fee(p):
    """Fee per contract at price p, in dollars."""
    return FEE_MULTIPLIER * p * (1.0 - p)


def load_quotes(db):
    """Return (markets_by_ticker, quotes) for every quote with a settled outcome."""
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        markets = {}
        for r in conn.execute(
                "select ticker, series, open_ts, close_ts, result from markets"):
            res = (r["result"] or "").strip().lower()
            if res in ("yes", "no") and r["open_ts"] and r["close_ts"]:
                markets[r["ticker"]] = {
                    "series": r["series"],
                    "open_ts": r["open_ts"],
                    "close_ts": r["close_ts"],
                    "won": res == "yes",
                }

        quotes = []
        for r in conn.execute(
                "select ticker, end_ts, yes_bid, yes_ask from candles "
                "where yes_bid is not null and yes_ask is not null"):
            m = markets.get(r["ticker"])
            if m is None:
                continue
            bid, ask = r["yes_bid"], r["yes_ask"]
            if not (0.0 < bid < 1.0 and 0.0 < ask < 1.0):
                continue
            mid = (bid + ask) / 2.0
            if not 0.0 < mid < 1.0:
                continue
            minutes_left = (m["close_ts"] - r["end_ts"]) / 60.0
            quotes.append({
                "ticker": r["ticker"],
                "series": m["series"],
                "mid": mid,
                "minutes_left": minutes_left,
                "won": m["won"],
            })
    finally:
        conn.close()
    return markets, quotes


def summarize(quotes, cluster=False):
    """Win rate vs price for quotes in one band.

    cluster=True collapses to one observation per market. A market settles
    once, so every quote taken inside it shares that outcome and is not an
    independent draw; the per-quote interval is too narrow and must not be
    the number quoted in config.
    """
    units = quotes
    if cluster:
        by_market = {}
        for q in quotes:
            by_market.setdefault(q["ticker"], []).append(q)
        units = []
        for ticker, qs in by_market.items():
            units.append({"ticker": ticker,
                          "mid": sum(q["mid"] for q in qs) / len(qs),
                          "won": qs[0]["won"]})
    if len(units) < 30:
        print("   n=%d (too few to judge)" % len(units))
        return
    wins = sum(1 for q in units if q["won"])
    f = wins / len(units)
    avg_p = sum(q["mid"] for q in units) / len(units)
    avg_fee = sum(taker_fee(q["mid"]) for q in units) / len(units)
    ev = f * (1 - avg_p) - (1 - f) * avg_p - avg_fee
    se = math.sqrt(f * (1 - f) / len(units))
    lower = f - 1.96 * se
    clears = lower > avg_p + avg_fee
    print("   n=%-5d win=%.4f price=%.4f fee=%.4f netEV=%+.4f "
          "lo95=%.4f %s" % (len(units), f, avg_p, avg_fee, ev,
                            lower, "CLEARS" if clears else "no"))


def main():
    parser = argparse.ArgumentParser(description="Measure favourite edge")
    parser.add_argument("--db", default="../kalshi_1230/data/candles.sqlite")
    parser.add_argument("--minutes", nargs=2, type=float, default=[6.0, 11.0],
                        metavar=("MIN", "MAX"))
    parser.add_argument("--cluster", choices=["quote", "market"], default="quote",
                        help="quote (default) or market: one observation per settled market")
    ap = parser.parse_args()

    markets, quotes = load_quotes(ap.db)
    print("resolved markets: %d   usable quotes: %d" % (len(markets), len(quotes)))
    print()

    lo_m, hi_m = ap.minutes
    window = [q for q in quotes if lo_m <= q["minutes_left"] < hi_m]
    print("entry window %d-%d min to close: %d quotes"
          % (lo_m, hi_m, len(window)))
    for lo, hi in BANDS:
        band = [q for q in window if lo <= q["mid"] < hi]
        print("%3.0fc-%-3.0fc" % (100 * lo, 100 * hi), end=" ")
        summarize(band, ap.cluster == "market")
    print()


if __name__ == "__main__":
    main()
