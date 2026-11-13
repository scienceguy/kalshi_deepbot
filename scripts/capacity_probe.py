#!/usr/bin/env python3
"""Capacity probe: which Kalshi crypto series exist at 5M / 15M / 1H / 1D?

Reads the public /trade-api/v2/series endpoint (and /markets for volume) and
buckets crypto series by cadence suffix. Read-only, no auth, no orders.
"""
import json
import re
import sys
import urllib.request

BASE = "https://external-api.kalshi.com/trade-api/v2"


def get(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "superbot-capacity/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main():
    # Pull open markets across the crypto series we know, plus probe common
    # suffix patterns to see which cadences the venue actually lists.
    patterns = ["5M", "15M", "1H", "1D", "HOURLY", "DAILY", "1M"]
    series_seen = {}
    # 1) enumerate via series endpoint (public)
    try:
        s = get("/series?category=Crypto")
        for row in s.get("series", []):
            t = row.get("ticker", "")
            series_seen[t] = row.get("title", "")
    except Exception as e:
        print("series endpoint failed:", e, file=sys.stderr)
    print("crypto series returned:", len(series_seen))

    def cadence(t):
        if re.search(r"(\d+)(M|H|D)$", t):
            m = re.search(r"(\d+)(M|H|D)$", t)
            return m.group(1) + m.group(2)
        return "other"

    buckets = {}
    for t, title in series_seen.items():
        buckets.setdefault(cadence(t), []).append((t, title))
    for cad in sorted(buckets):
        items = buckets[cad]
        print(f"\n=== cadence {cad}: {len(items)} series ===")
        for t, title in items[:12]:
            print(f"  {t:28s} {title[:60]}")

    # 2) direct probe: do open markets exist for canonical tickers?
    print("\n=== open-market probe (limit=1 each) ===")
    for series in ["KXBTC5M", "KXBTCD", "KXBTC", "KXBTC15M", "KXBTCH", "KXBTCHOURLY",
                   "KXBTC1H", "KXBTC1D", "KXETH15M", "KXETH1H", "KXETH1D"]:
        try:
            m = get(f"/markets?status=open&limit=1&series_ticker={series}")
            mk = m.get("markets", [])
            print(f"  {series:14s} open_markets={'YES' if mk else 'no '} "
                  f"{mk[0]['ticker'] if mk else ''}")
        except Exception as e:
            print(f"  {series:14s} ERR {e}")


if __name__ == "__main__":
    main()
