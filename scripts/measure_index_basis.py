#!/usr/bin/env python3
"""Measure the settlement-index basis that fair value must price against.

The fair-value models compare a venue price against a probability computed
from a spot feed. Kalshi's 15M and daily threshold contracts settle on the CF
Benchmarks BRTI 60-second average, while the bot's feed is a median of
reachable venues. If the two disagree, the model reports an edge that is
really a data-source mismatch: buying a contract priced against the wrong
index is not edge, it is adverse selection with extra steps.

Measured on 2026-11-21 (45 settled 15M windows, 12:13-12:44Z): settlement vs
composite spot = -11.34 bps mean, sd 11.72, standard error 1.75 bps — a real
bias, not noise. Cross-venue dispersion at the same time was $117.50 (~20 bps)
between Coinbase $58,717 and Binance $58,811, which is the dominant term the
GBM fair never saw.

Subcommands:
  --collected FILE   settlement-vs-spot basis in bps from a live JSONL
  --ladder EVENT     live daily ladder vs the index-corrected fair value
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import crossvenue as cv  # noqa: E402
from binance_feed import (composite_price, get_klines, price_sources,  # noqa: E402
                          prob_above)
from datetime import datetime, timezone  # noqa: E402


def settlement_basis(rows: list[dict]) -> dict:
    """Settlement value vs the contemporaneous spot, in basis points.

    A settlement-only row carries the venue's expiration_value; the quote rows
    for the same ticker carry the spot recorded at that minute. The last
    minute with a recorded spot is the best available comparison (the
    settlement is the 60s average ending at close, so a point sample one
    minute earlier is the closest honest match).
    """
    settlement = {r["ticker"]: r for r in rows if r.get("result")}
    quote_by_minute: dict[str, dict[int, dict]] = {}
    for r in rows:
        if r.get("result"):
            continue
        quote_by_minute.setdefault(r["ticker"], {})[int(r["minute"])] = r

    bps = []
    per_series: dict[str, list[float]] = {}
    for ticker, s in settlement.items():
        quotes = quote_by_minute.get(ticker) or {}
        minutes = [m for m, q in quotes.items() if (q.get("spot") or 0) > 0]
        if not minutes:
            continue
        spot = float(quotes[max(minutes)]["spot"])
        settled = float(s.get("expiration_value") or 0)
        if spot <= 0 or settled <= 0:
            continue
        b = (settled / spot - 1) * 10_000.0
        bps.append(b)
        per_series.setdefault(ticker.split("-")[0], []).append(b)

    if not bps:
        return {"n": 0}
    return {
        "n": len(bps),
        "mean_bps": statistics.mean(bps),
        "sd_bps": statistics.pstdev(bps) if len(bps) > 1 else 0.0,
        "min_bps": min(bps),
        "max_bps": max(bps),
        "stderr_bps": (statistics.pstdev(bps) / math.sqrt(len(bps))
                       if len(bps) > 1 else 0.0),
        "per_series": {k: {"n": len(v), "mean_bps": statistics.mean(v)}
                       for k, v in sorted(per_series.items())},
    }


def corrected_fair(spot: float, strike: float, sigma_min: float,
                   minutes_left: float, bias_bps: float,
                   dispersion: float) -> float:
    """P(settlement > strike) against the index that settles the contract.

    Three terms the point-spot GBM fair leaves out:
    * bias_bps — the settlement index's level relative to the feed, so the
      spot is adjusted to the index level before the probability is computed.
    * dispersion — the disagreement between venue prices, which is the
      uncertainty about which level the index prints. Added in quadrature.
    * The settlement value is the 60-second average ending at close, so the
      terminal level keeps its full walk variance and only the final minute
      is smoothed (an average of a Brownian path over one minute has the
      variance of that minute divided by three, not by the number of prints).

    measured 2026-11-21: the raw point-spot fair reads +7c mean "Kalshi is
    cheap" on the daily ladder; with the index terms the gap does not survive,
    which is why the edge must never be taken from the raw number.
    """
    if spot <= 0 or strike <= 0 or minutes_left <= 0 or sigma_min <= 0:
        return float("nan")
    index_spot = spot * (1.0 + bias_bps / 10_000.0)
    terminal_var = (sigma_min ** 2) * minutes_left
    average_var = (sigma_min ** 2) / 3.0            # final-minute smoothing
    sigma_eff = math.sqrt(terminal_var + average_var
                          + (dispersion or 0.0) ** 2)
    denom = sigma_eff
    if denom <= 0:
        return 1.0 if index_spot > strike else 0.0
    z = math.log(strike / index_spot) / denom
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _report_collected(path: str) -> int:
    rows = [json.loads(l) for l in open(path) if l.strip()]
    rep = settlement_basis(rows)
    if not rep.get("n"):
        print("no windows with both a recorded spot and a settlement value")
        return 1
    print("settlement vs spot basis (n=%d): mean %+.2f bps  sd %.2f  "
          "stderr %.2f  range [%+.1f, %+.1f]" % (
              rep["n"], rep["mean_bps"], rep["sd_bps"], rep["stderr_bps"],
              rep["min_bps"], rep["max_bps"]))
    for series, r in rep["per_series"].items():
        print("  %-12s n=%3d mean %+7.2f bps" % (series, r["n"], r["mean_bps"]))
    return 0


def _report_ladder(event: str, bias_bps: float) -> int:
    ladder = cv.fetch_kalshi_ladder(event)
    if not ladder or not ladder.sorted_rungs():
        print("no usable ladder for %s" % event)
        return 1
    spot = composite_price("BTCUSDT")
    sources = price_sources("BTCUSDT")
    dispersion = (max(sources.values()) - min(sources.values())) / spot
    klines = get_klines("BTCUSDT", "1m", 60) or []
    closes = [float(k[4]) for k in klines]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) >= 5:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        sigma_min = math.sqrt(var)
    else:
        sigma_min = 0.0
    minutes_left = (cv.to_utc(ladder.expiry_et)
                    - datetime.now(timezone.utc)).total_seconds() / 60.0
    print("%s  spot %.2f  minutes_left %.1f  sigma_min %.6f" % (
        event, spot, minutes_left, sigma_min))
    print("measured dispersion %.1f bps; applying settlement bias %+.2f bps" % (
        dispersion * 10_000, bias_bps))
    print("%-10s %-6s %-6s %-9s %-9s %-9s %-9s" % (
        "strike", "bid", "ask", "raw_fair", "fair", "buy_edge", "mid_edge"))
    for r in ladder.sorted_rungs():
        if abs(r.strike - spot) > spot * 0.01:
            continue
        raw = prob_above(spot, r.strike, sigma_min, minutes_left)
        fair = corrected_fair(spot, r.strike, sigma_min, minutes_left,
                              bias_bps, dispersion)
        if r.ask and r.ask > 0:
            buy_edge = (fair - float(r.ask)) * 100.0
        else:
            buy_edge = float("nan")
        mid_edge = (fair - r.mid) * 100.0
        print("%-10.2f %-6s %-6s %-9.4f %-9.4f %-9s %-9s" % (
            r.strike, r.bid, r.ask, raw, fair,
            ("%+.2f" % buy_edge) if buy_edge == buy_edge else "-",
            "%+.2f" % mid_edge))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--collected", help="live JSONL to measure the basis from")
    ap.add_argument("--ladder", help="Kalshi daily event ticker to price")
    ap.add_argument("--bias-bps", type=float, default=-11.34,
                    help="settlement-vs-feed basis to apply (default: the "
                         "2026-11-21 measured mean, negative = the index "
                         "prints below the feed)")
    args = ap.parse_args()
    if args.collected:
        return _report_collected(args.collected)
    if args.ladder:
        return _report_ladder(args.ladder, args.bias_bps)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())