#!/usr/bin/env python3
"""Polymarket 5-minute crypto PAPER trader (VPS, read-only venue access).

Trades the only short-duration Poly product: the 5-minute Up/Down binary. For
each current window it computes a GBM fair value P(Up) = Phi(ln(S/S0) /
(sigma*sqrt(minutes_left))) from the composite Binance/Coinbase spot and the
window-open spot, gates on (fair - ask) >= margin, and PAPER-executes at the
live CLOB ask (real book, no synthetic quote). Holds to resolution; resolution
is proxied by the composite spot at the window boundary (Poly settles on a
Chainlink feed). A per-trade fee models Poly's crypto taker fee (reality tax).

Places nothing. Appends to poly5m_paper_ledger.jsonl and prints a running P&L.

Usage: python scripts/poly5m_paper.py [--interval 15] [--margin 0.03]
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import binance_feed  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
TF = os.environ.get("POLY_TF", "5m")   # "5m" or "15m" (both exist; 15m matches Kalshi)
WIN_SEC = 300 if TF == "5m" else 900
SNAP = os.environ.get("POLY5M_LOG", f"/opt/superbot/poly{TF}_snapshots.jsonl")
LEDGER = os.environ.get("POLY5M_LEDGER", f"/opt/superbot/poly{TF}_paper_ledger.jsonl")
COINS = ["btc", "eth", "sol", "doge", "hype", "xrp"]
ASSET = {"btc": "BTC", "eth": "ETH", "sol": "SOL", "doge": "DOGE", "hype": "HYPE", "xrp": "XRP"}
FEE = 0.02  # Poly crypto taker fee ceiling (reality tax), applied on notional


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def spot(coin):
    try:
        return binance_feed.composite_price(f"{ASSET[coin]}USDT", timeout=8)
    except Exception:
        return None


def book(token_id):
    try:
        b = requests.get(CLOB + "/book", params={"token_id": token_id}, timeout=12).json()
        bids = [float(x["price"]) for x in b.get("bids", [])]
        asks = [float(x["price"]) for x in b.get("asks", [])]
        return (min(asks) if asks else None, max(bids) if bids else None)
    except Exception:
        return (None, None)


def market(slug):
    try:
        d = requests.get(GAMMA + "/markets", params={"slug": slug}, timeout=12).json()
        return d[0] if isinstance(d, list) and d else None
    except Exception:
        return None


def append(path, rec):
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--margin", type=float, default=0.03)
    ap.add_argument("--cycles", type=int, default=0)
    ap.add_argument("--coins", default=",".join(COINS))
    ap.add_argument("--min_elapsed", type=float, default=0,
                    help="seconds into the window before an entry is allowed "
                         "(avoids the drift~0 window-open coin flip)")
    a = ap.parse_args()
    coins = [x.strip() for x in a.coins.split(",") if x.strip()]
    open_spot = {}      # (coin, window) -> S0
    sigma = {}          # coin -> per-minute log-vol
    positions = {}      # (coin, window) -> {side, entry, S0, ts}
    realised = 0.0
    n = wins = 0
    cur_w = None
    print(f"poly5m PAPER margin={a.margin} fee={FEE} interval={a.interval}s", flush=True)
    c = 0
    while True:
        c += 1
        now = time.time()
        w5 = int(now - (now % WIN_SEC))
        left = max(0.0, w5 + WIN_SEC - now)
        mins_left = max(0.1, left / 60.0)
        # settle positions from ended windows using the boundary spot
        for key in list(positions):
            coin, w = key
            if w < w5:
                pos = positions.pop(key)
                sp = spot(coin)
                if sp is not None:
                    up_won = sp > pos["S0"]
                    payout = 1.0 if ((pos["side"] == "Up") == up_won) else 0.0
                    cost = pos["entry"] + FEE * pos["entry"]
                    pnl = payout - cost
                    realised += pnl
                    n += 1
                    wins += 1 if pnl > 0 else 0
                    append(LEDGER, {"ts": datetime.now(timezone.utc).isoformat(),
                                    "coin": coin, "window": w, "side": pos["side"],
                                    "entry": pos["entry"], "S0": pos["S0"],
                                    "S_end": sp, "payout": payout, "pnl": round(pnl, 5)})
                    print(f"SETTLE {coin} w{w} {pos['side']} entry={pos['entry']} "
                          f"pnl={pnl:+.4f} | cum={realised:+.4f} ({wins}/{n})", flush=True)
        for coin in coins:
            slug = f"{coin}-updown-{TF}-{w5}"
            m = market(slug)
            if not m:
                continue
            sp = spot(coin)
            if sp is None:
                continue
            key = (coin, w5)
            if key not in open_spot:
                # S0 must be the TRUE window-open price. A first observation
                # far into the window would make drift~0 and invent fake edges,
                # so only trust one taken near the open; otherwise skip this
                # window and wait for the next.
                if (now - w5) <= 60:
                    open_spot[key] = sp
                else:
                    continue
            S0 = open_spot[key]
            if coin not in sigma:
                try:
                    sigma[coin] = binance_feed.sigma_per_minute(f"{ASSET[coin]}USDT") or 0.0008
                except Exception:
                    sigma[coin] = 0.0008
            toks = json.loads(m.get("clobTokenIds") or "[]")
            outc = json.loads(m.get("outcomes") or "[]")
            px = {}
            for t, o in zip(toks, outc):
                ask, bid = book(t)
                px[o] = {"ask": ask, "bid": bid}
            up_ask = px.get("Up", {}).get("ask")
            dn_ask = px.get("Down", {}).get("ask")
            if not S0 or S0 <= 0 or sp <= 0:
                continue
            z = math.log(sp / S0) / max(1e-9, sigma[coin] * math.sqrt(mins_left))
            fair_up = norm_cdf(z)
            e_up = (fair_up - up_ask) if up_ask else None
            e_dn = ((1 - fair_up) - dn_ask) if dn_ask else None
            append(SNAP, {"ts": datetime.now(timezone.utc).isoformat(), "unix": now,
                          "coin": coin, "slug": slug, "window_start": w5,
                          "sec_into_window": round(now - w5, 1), "spot": sp,
                          "window_open_spot": S0, "sigma": sigma[coin],
                          "fair_up": round(fair_up, 4), "up_ask": up_ask,
                          "down_ask": dn_ask, "edge_up": e_up, "edge_down": e_dn})
            # enter the best side once per window if it clears the margin
            if key not in positions and (now - w5) >= a.min_elapsed:
                best = None
                if e_up is not None and e_up >= a.margin:
                    best = ("Up", up_ask)
                if e_dn is not None and e_dn >= a.margin and (best is None or e_dn > e_up):
                    best = ("Down", dn_ask)
                if best:
                    positions[key] = {"side": best[0], "entry": best[1], "S0": S0,
                                      "ts": now, "fair_up": fair_up}
                    print(f"ENTER {coin} w{w5} {best[0]} @ {best[1]} fair_up={fair_up:.2f} "
                          f"edge={max(e_up or 0, e_dn or 0):+.3f}", flush=True)
        if c % 4 == 0:
            print(f"[{c}] w={w5} left={left:.0f}s open={len(positions)} "
                  f"settled={n} cum={realised:+.4f}", flush=True)
        if a.cycles and c >= a.cycles:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
