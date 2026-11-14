#!/usr/bin/env python3
"""Live cross-venue deviation: Kalshi 15m vs Polymarket 15m, same coin/window.

Both venues quote P(up over the 15m window) for the same underlying but settle
on different feeds (Kalshi: CF Benchmarks BRTI 60s average; Poly: Chainlink), so
this is a deviation study, not a riskless arb. Read-only.

Prints, per coin: Kalshi YES mid, Poly Up mid, and the deviation (Kalshi-Poly).
"""
import json
import time
import urllib.request

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
COINS = {"BTC": "btc", "ETH": "eth", "SOL": "sol", "DOGE": "doge",
         "XRP": "xrp", "HYPE": "hype", "BNB": "bnb"}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "xvenue/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def kalshi_mid(series, w15):
    """Kalshi YES mid for the 15m market whose window matches w15."""
    try:
        def num(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None

        d = get(f"{KALSHI}/markets?status=open&limit=100&series_ticker={series}")
        for m in d.get("markets", []):
            # Kalshi's public API mixes *_dollars (float or str) with legacy cents.
            yb = num(m.get("yes_bid_dollars"))
            ya = num(m.get("yes_ask_dollars"))
            if yb is None:
                c = num(m.get("yes_bid"))
                yb = c / 100.0 if c is not None else None
            if ya is None:
                c = num(m.get("yes_ask"))
                ya = c / 100.0 if c is not None else None
            if yb is None or ya is None:
                continue
            return (yb + ya) / 2.0
        return None
    except Exception as e:
        return f"ERR {e}"


def poly_mid(coin, w15):
    try:
        d = get(f"{GAMMA}/markets?slug={coin}-updown-15m-{w15}")
        if not (isinstance(d, list) and d):
            return None
        m = d[0]
        toks = json.loads(m["clobTokenIds"])
        # Up token is index 0 for outcomes ["Up","Down"]
        clob = "https://clob.polymarket.com/book?token_id=" + toks[0]
        b = get(clob)
        bids = [float(x["price"]) for x in b.get("bids", [])]
        asks = [float(x["price"]) for x in b.get("asks", [])]
        if not bids or not asks:
            return None
        return (max(bids) + min(asks)) / 2.0
    except Exception as e:
        return f"ERR {e}"


def main():
    now = int(time.time())
    w15 = now - (now % 900)
    left = w15 + 900 - now
    print("15m window %d (%.0fs left)" % (w15, left))
    print("%-6s %12s %12s %12s" % ("coin", "kalshi_mid", "poly_mid", "10k*dev"))
    for kc, pc in COINS.items():
        km = kalshi_mid(f"KX{kc}15M", w15)
        pm = poly_mid(pc, w15)
        dev = None
        if isinstance(km, (int, float)) and isinstance(pm, (int, float)):
            dev = km - pm
        print("%-6s %12s %12s %12s" % (kc, km, pm,
                                       ("%+.3f" % dev) if dev is not None else "n/a"))


if __name__ == "__main__":
    main()
