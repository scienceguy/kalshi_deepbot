#!/usr/bin/env python3
"""measure_edge_poly.py — X1: Measure Polymarket US favorite win rate by price band.

Analogous to scripts/measure_edge.py but for Polymarket US markets.
Requires a SQLite DB with markets and candles tables populated by data_collector.py
or poly_us_stream.py.

Tables needed:
    poly_markets(slug, series, open_ts, close_ts, result)   result: yes|no
    poly_candles(slug, end_ts, yes_bid, yes_ask)            prices in dollars

Output: for each price band and entry window, the number of settled quotes,
win rate, average price paid, net EV per contract after fees, and whether
the 95% lower bound clears breakeven.
"""

import argparse
import math
import sqlite3
import sys
from pathlib import Path

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

# Polymarket US fee: 0% for maker, taker fee varies
# Using a conservative estimate for fee calculation
FEE_MULTIPLIER = 0.02  # ~2% estimate for taker (Polymarket US has no protocol fee for maker)

BANDS = (
    (0.02, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40),
    (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80),
    (0.80, 0.90), (0.90, 0.95), (0.95, 0.99), (0.99, 1.00),
)


def taker_fee(p: float) -> float:
    """Fee per contract at price p, in dollars."""
    return FEE_MULTIPLIER * p * (1.0 - p)


def load_poly_quotes(db_path: str):
    """Return (markets_by_slug, quotes) for every quote with a settled outcome."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        markets = {}
        for r in conn.execute(
                "select slug, series, open_ts, close_ts, result from poly_markets"):
            res = (r["result"] or "").strip().lower()
            if res in ("yes", "no") and r["open_ts"] and r["close_ts"]:
                markets[r["slug"]] = {
                    "series": r["series"],
                    "open_ts": r["open_ts"],
                    "close_ts": r["close_ts"],
                    "won": res == "yes",
                }

        quotes = []
        for r in conn.execute(
                "select slug, end_ts, yes_bid, yes_ask from poly_candles "
                "where yes_bid is not null and yes_ask is not null"):
            m = markets.get(r["slug"])
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
                "slug": r["slug"],
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
            by_market.setdefault(q["slug"], []).append(q)
        units = []
        for slug, qs in by_market.items():
            units.append({"slug": slug,
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
    parser = argparse.ArgumentParser(description="Measure Polymarket US favorite edge")
    parser.add_argument("--db", default="local_data/poly_us.sqlite",
                        help="Path to SQLite DB with poly_markets and poly_candles tables")
    parser.add_argument("--minutes", nargs=2, type=float, default=[6.0, 11.0],
                        metavar=("MIN", "MAX"),
                        help="Entry window in minutes before close")
    parser.add_argument("--cluster", choices=["quote", "market"], default="quote",
                        help="quote (default) or market: one observation per settled market")
    parser.add_argument("--band-min", type=float, default=0.95,
                        help="Minimum price band to analyze (default 0.95)")
    parser.add_argument("--band-max", type=float, default=0.99,
                        help="Maximum price band to analyze (default 0.99)")
    ap = parser.parse_args()

    if not Path(ap.db).exists():
        print(f"Database not found: {ap.db}")
        print("Run data_collector.py or poly_us_stream.py to populate the database first.")
        return 1

    markets, quotes = load_poly_quotes(ap.db)
    print("resolved markets: %d   usable quotes: %d" % (len(markets), len(quotes)))
    print()

    lo_m, hi_m = ap.minutes
    window = [q for q in quotes if lo_m <= q["minutes_left"] < hi_m]
    print("entry window %d-%d min to close: %d quotes"
          % (lo_m, hi_m, len(window)))

    # Custom bands focused on the favorite region
    custom_bands = []
    for lo, hi in BANDS:
        if lo >= ap.band_min and hi <= ap.band_max:
            custom_bands.append((lo, hi))

    if not custom_bands:
        custom_bands = [(ap.band_min, ap.band_max)]

    for lo, hi in custom_bands:
        band = [q for q in window if lo <= q["mid"] < hi]
        print("%3.0fc-%-3.0fc" % (100 * lo, 100 * hi), end=" ")
        summarize(band, ap.cluster == "market")
    print()

    # Also print summary for the main band (95-99c)
    main_band = [q for q in window if 0.95 <= q["mid"] < 0.99]
    if main_band:
        print("=== MAIN BAND 95c-99c ===")
        summarize(main_band, False)
        summarize(main_band, True)

    return 0


if __name__ == "__main__":
    sys.exit(main())