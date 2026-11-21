#!/usr/bin/env python3
"""Fetch CF Benchmarks settlement-index history through the Kalshi passthrough.

The 15-minute crypto contracts settle on the CF Benchmarks 60-second average,
and Kalshi's API passes those index values through with the same API key
(``/trade-api/v2/cfbenchmarks/*``). This script pulls tick-level history for
every supported series so a replay can price the model off what the contracts
actually settle on instead of a venue feed.

Measured context (2026-11-21): the venue feed prints +13.15bps above the
settlement index (n=45 settled windows), and cross-venue dispersion adds
17-20bps — both terms exist only because the bot never priced against the
index itself. With real index values the bias is zero by construction.

Usage:
  python scripts/fetch_brti_history.py --start 2026-11-21T09:30:00Z \
      --hours 3 --out /tmp/opencode/brti_history.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import kalshi_client  # noqa: E402

# CF Benchmarks index ids per Kalshi's coin/index table (BTC is the legacy
# short id; every other series is <ASSET>USD_RTI).
INDEX_IDS = {
    "BTC": "BRTI",
    "ETH": "ETHUSD_RTI",
    "SOL": "SOLUSD_RTI",
    "XRP": "XRPUSD_RTI",
    "DOGE": "DOGEUSD_RTI",
    "BNB": "BNBUSD_RTI",
    "HYPE": "HYPEUSD_RTI",
    "ZEC": "ZECUSD_RTI",
    "NEAR": "NEARUSD_RTI",
}


def fetch_span(client, index_id: str, start: datetime, hours: int) -> list:
    """Tick values for one index over ``hours`` starting at ``start``.

    HOUR windows are requested per hour of the span (the passthrough maps
    CF Benchmarks' own timespan codes). A failed hour is skipped, never
    silently coerced to zero — the caller sees fewer ticks, not fake ones.
    """
    rows = []
    for h in range(hours):
        # CF Benchmarks rejects a timestamp not truncated to the timespan
        # granularity ("Timestamp is not truncated to the expected
        # granularity: Hours"), so anchor each hour window on the hour.
        anchor = (start + timedelta(hours=h)).replace(
            minute=0, second=0, microsecond=0)
        try:
            resp = client.get_cfbenchmarks_history(
                index_id, span="HOUR", timestamp=anchor.isoformat())
        except Exception as e:
            print(f"  {index_id} hour {h} failed: {e}")
            continue
        payload = (resp.get("data") or {}).get("payload") or []
        for p in payload:
            try:
                rows.append({"time": int(p["time"]),
                             "value": float(p["value"])})
            except (KeyError, TypeError, ValueError):
                continue  # reviewed: skip malformed ticks; never synthesize
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True,
                    help="ISO-8601 anchor of the first hour window (UTC)")
    ap.add_argument("--hours", type=int, default=1)
    ap.add_argument("--assets", default="all",
                    help="comma list or 'all'")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    try:
        start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    except ValueError:
        print(f"bad --start: {args.start}")
        return 2
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    assets = list(INDEX_IDS) if args.assets == "all" else [
        a.strip().upper() for a in args.assets.split(",") if a.strip()]

    client = kalshi_client.KalshiClient()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with out.open("w") as f:
        for asset in assets:
            index_id = INDEX_IDS[asset]
            t0 = time.time()
            ticks = fetch_span(client, index_id, start, args.hours)
            for t in ticks:
                f.write(json.dumps({"asset": asset, **t}) + "\n")
            total += len(ticks)
            print(f"{asset:5s} {index_id:12s} {len(ticks):6d} ticks "
                  f"({time.time() - t0:.1f}s)")
    print(f"wrote {total} ticks -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())