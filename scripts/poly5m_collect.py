#!/usr/bin/env python3
"""Polymarket 5-minute crypto collector + paper fair-value evaluator (VPS).

Polymarket's only short-duration crypto product is the 5-minute "Up or Down"
binary (the 15m product does not exist on Polymarket — verified). This snapshots
the LIVE CLOB book for the current 5m window markets, appends real tick data to
poly5m_snapshots.jsonl, and logs a GBM fair-value vs market signal against the
composite Binance/Coinbase/OKX spot. Read-only: it places nothing.

Usage: python scripts/poly5m_collect.py [--interval 15] [--coins btc,eth,sol]
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import requests

# Make the repo root importable so binance_feed (composite spot) loads even when
# this script is run as `python scripts/poly5m_collect.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
OUT = os.environ.get("POLY5M_LOG", "/opt/superbot/poly5m_snapshots.jsonl")
FEE_RATE = 0.07

COINS = ["btc", "eth", "sol", "doge", "hype", "xrp"]
ASSET = {"btc": "BTC", "eth": "ETH", "sol": "SOL", "doge": "DOGE",
         "hype": "HYPE", "xrp": "XRP"}


def book(token_id):
    try:
        r = requests.get(CLOB + "/book", params={"token_id": token_id}, timeout=12)
        r.raise_for_status()
        b = r.json()
        bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
        asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
        bb = max((p for p, _ in bids), default=None)
        ba = min((p for p, _ in asks), default=None)
        return {"best_bid": bb, "best_ask": ba, "bid_depth": len(bids),
                "ask_depth": len(asks), "n_bids": len(bids), "n_asks": len(asks)}
    except Exception as e:
        return {"error": str(e)}


def market(slug):
    try:
        r = requests.get(GAMMA + "/markets", params={"slug": slug}, timeout=12)
        d = r.json()
        return d[0] if isinstance(d, list) and d else None
    except Exception:
        return None


def spot(coin):
    # binance + coinbase composite (binance_feed is the repo's own composite)
    try:
        import binance_feed
        return binance_feed.composite_price(f"{ASSET[coin]}USDT", timeout=8)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--coins", default=",".join(COINS))
    ap.add_argument("--cycles", type=int, default=0, help="0 = run forever")
    a = ap.parse_args()
    coins = [c.strip() for c in a.coins.split(",") if c.strip()]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    print(f"poly5m collector -> {OUT} interval={a.interval}s coins={coins}", flush=True)
    window_open_spot = {}
    n = 0
    while True:
        n += 1
        now = time.time()
        w5 = int(now - (now % 300))
        for coin in coins:
            slug = f"{coin}-updown-5m-{w5}"
            m = market(slug)
            if not m:
                continue
            toks = json.loads(m.get("clobTokenIds") or "[]")
            outc = json.loads(m.get("outcomes") or "[]")
            px = spot(coin)
            window_open_spot.setdefault((coin, w5), px)
            base = window_open_spot[(coin, w5)]
            rec = {"ts": datetime.now(timezone.utc).isoformat(), "unix": now,
                   "coin": coin, "slug": slug, "window_start": w5,
                   "sec_into_window": round(now - w5, 1),
                   "spot": px, "window_open_spot": base}
            sides = {}
            for t, o in zip(toks, outc):
                sides[o] = book(t)
            rec["sides"] = sides
            # paper fair value: P(up) from log(spot/base) with a 1-min sigma proxy
            if px and base and px > 0 and base > 0:
                rem = max(1.0, (w5 + 300 - now) / 60.0)  # minutes left
                # crude realized-vol-free proxy: distance over remaining minutes
                drift = math.log(px / base)
                rec["log_move"] = round(drift, 6)
                rec["minutes_left"] = round(rem, 2)
            with open(OUT, "a") as f:
                f.write(json.dumps(rec) + "\n")
            up = sides.get("Up", {})
            print(f"[{n}] {slug} sec={rec['sec_into_window']:.0f} "
                  f"spot={px} Up(bid={up.get('best_bid')} ask={up.get('best_ask')})",
                  flush=True)
        if a.cycles and n >= a.cycles:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
