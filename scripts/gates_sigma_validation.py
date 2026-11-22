#!/usr/bin/env python3
"""Validate gates.sigma_from_atr V1 vs V2 against realized close-to-close sigma.

T15-03 / F-05: gates.py computes fractional per-sqrt-minute log-vol from a
dollar ATR. V1 multiplies by sqrt(pi/2) as if ATR were E|dS|; V2 divides by
1.6. ATR is a mean TRUE RANGE of high-low candles, so the right multiplier
depends on how much of the range is drift. This script measures the bias on
the SAME candles the gates see (Binance 1m klines, public market-data host):

    realized sigma = std of 1-minute log close-to-close returns
    ATR            = mean true range of the same candles (Wilder TR)
    V1            = (atr / spot) * sqrt(pi/2)      [current default]
    V2            = (atr / spot) / 1.6             [GATES_SIGMA_FROM_ATR_V2]

Output: n windows, median and mean ratio of each estimator to realized, the
implied overstatement multiple, and the Brier-relevant direction (overstating
sigma pushes fair values toward 0.5).

Read-only: no orders, no venue writes.

    python scripts/gates_sigma_validation.py --symbol BTCUSDT --hours 12
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import binance_feed  # noqa: E402


def true_range(candles) -> list:
    """Wilder true range per candle (first row uses high-low)."""
    trs = []
    prev_close = None
    for row in candles:
        _t, o, h, l, c = row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4])
        if prev_close is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    return trs


def collect(symbol: str, hours: int, atr_len: int) -> tuple:
    """(n_windows, v1_ratios, v2_ratios, realized_sigmas, spot_levels)."""
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - hours * 3600 * 1000
    candles = binance_feed._fetch_klines_paginated(
        f"{binance_feed.BINANCE_REST}/klines",
        lambda cursor, s=start_ms, e=end_ms: {"symbol": symbol, "interval": "1m",
                                              "startTime": s, "endTime": e,
                                              "limit": 1000},
        start_ms, end_ms, timeout=30)
    if not candles:
        return 0, [], [], [], []
    trs = true_range(candles)
    closes = [float(r[4]) for r in candles]
    v1, v2, realized, spots = [], [], [], []
    for i in range(atr_len + 1, len(candles) - 1):
        spot = closes[i]
        if spot <= 0:
            continue
        r = math.log(closes[i + 1] / closes[i])
        realized.append(abs(r))
        window = trs[i - atr_len + 1:i + 1]
        atr = sum(window) / len(window)
        a = atr / spot
        v1.append(a * math.sqrt(math.pi / 2))
        v2.append(a / 1.6)
        spots.append(spot)
    return len(v1), v1, v2, realized, spots


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--hours", type=int, default=12)
    ap.add_argument("--atr-len", type=int, default=14)
    A = ap.parse_args()

    n, v1, v2, realized, _spots = collect(A.symbol, A.hours, A.atr_len)
    if n == 0:
        print("no candles fetched")
        return 1

    def stats(v):
        m = sorted(v)
        med = m[len(m) // 2]
        mean = sum(v) / len(v)
        return med, mean

    # realized sigma is measured as E|log return|; scale to sigma via the
    # normal relation E|X| = sigma * sqrt(2/pi)
    realized_scaled = [r * math.sqrt(math.pi / 2) for r in realized]
    r_med, r_mean = stats(realized_scaled)
    v1_med, v1_mean = stats(v1)
    v2_med, v2_mean = stats(v2)

    def ratio_med(v):
        pairs = sorted((a / b) for a, b in zip(v, realized_scaled) if b > 0)
        return pairs[len(pairs) // 2] if pairs else float("nan")

    print(f"symbol={A.symbol} hours={A.hours} atr_len={A.atr_len} n_windows={n}")
    print(f"realized close-to-close sigma (fractional/min): median={r_med:.6f} mean={r_mean:.6f}")
    print(f"V1 (atr*sqrt(pi/2)) sigma: median={v1_med:.6f} mean={v1_mean:.6f} "
          f"overstatement_median={ratio_med(v1):.3f}x")
    print(f"V2 (atr/1.6)       sigma: median={v2_med:.6f} mean={v2_mean:.6f} "
          f"overstatement_median={ratio_med(v2):.3f}x")
    better = "V2" if abs(ratio_med(v2) - 1.0) < abs(ratio_med(v1) - 1.0) else "V1"
    print(f"closer to realized on the median: {better}")
    print("decision: enable GATES_SIGMA_FROM_ATR_V2 only if V2 is closer AND the"
          " calibration evidence (gates EV outcomes on the same candles) improves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())