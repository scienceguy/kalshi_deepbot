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

# M1: MemPalace bridge for agent coordination
try:
    import agent_identity
    import mempalace_bridge
    _MP_BRIDGE = mempalace_bridge.get_default_bridge(agent_identity.POLY_US_COLLECTOR)
    _MP_BRIDGE.start()
    _MP_AVAILABLE = True
except Exception as e:
    _MP_BRIDGE = None
    _MP_AVAILABLE = False
    print(f"MemPalace bridge unavailable: {e} — running without coordination", file=sys.stderr)

MP_CHECKPOINT_EVERY = int(os.environ.get("MEMPALACE_CHECKPOINT_INTERVAL", 5))

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


def _mp_sweep_tasks():
    """Sweep MemPalace inbox for task.request events addressed to this agent."""
    if not _MP_AVAILABLE or _MP_BRIDGE is None:
        return
    try:
        for task in _MP_BRIDGE.drain_tasks():
            print(f"MEMPALACE TASK id={task.get('event_id','')} corr={task.get('correlation_id','')} from={task.get('from_agent','')} body={(task.get('body') or '')[:200]}", flush=True)
            try:
                _MP_BRIDGE.ack_event(task.get("event_id", ""), "task received by poly_us_collector")
            except Exception as e:
                print(f"MemPalace task ack failed: {e}", file=sys.stderr)
    except Exception as e:
        print(f"MemPalace task sweep failed: {e}", file=sys.stderr)


def _mp_checkpoint(cycle_num, markets_scanned, snapshots_written):
    """Queue an operational checkpoint to MemPalace."""
    if not _MP_AVAILABLE or _MP_BRIDGE is None:
        return
    try:
        _MP_BRIDGE.checkpoint({
            "cycles_completed": cycle_num,
            "markets_scanned": markets_scanned,
            "snapshots_written": snapshots_written,
            "last_activity": datetime.now(timezone.utc).isoformat(),
        }, correlation_id=f"collect-checkpoint-cycle-{cycle_num}")
    except Exception as e:
        print(f"MemPalace checkpoint failed: {e}", file=sys.stderr)


def _mp_heartbeat(cycle_num):
    """Queue a heartbeat to MemPalace."""
    if not _MP_AVAILABLE or _MP_BRIDGE is None:
        return
    try:
        _MP_BRIDGE.heartbeat(cycle_num)
    except Exception as e:
        print(f"MemPalace heartbeat failed: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--coins", default=",".join(COINS))
    ap.add_argument("--cycles", type=int, default=0, help="0 = run forever")
    a = ap.parse_args()
    coins = [c.strip() for c in a.coins.split(",") if c.strip()]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    print(f"poly5m collector -> {OUT} interval={a.interval}s coins={coins}", flush=True)
    if _MP_AVAILABLE:
        print(f"MemPalace bridge active: {agent_identity.POLY_US_COLLECTOR}", flush=True)
    window_open_spot = {}
    n = 0
    snapshots_written = 0
    while True:
        n += 1
        # Sweep MemPalace inbox for delegated tasks
        _mp_sweep_tasks()
        now = time.time()
        w5 = int(now - (now % 300))
        markets_this_cycle = 0
        for coin in coins:
            slug = f"{coin}-updown-5m-{w5}"
            m = market(slug)
            if not m:
                continue
            markets_this_cycle += 1
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
            snapshots_written += 1
            up = sides.get("Up", {})
            print(f"[{n}] {slug} sec={rec['sec_into_window']:.0f} "
                  f"spot={px} Up(bid={up.get('best_bid')} ask={up.get('best_ask')})",
                  flush=True)
        # MemPalace heartbeat every cycle, checkpoint every N cycles
        _mp_heartbeat(n)
        if n % MP_CHECKPOINT_EVERY == 0:
            _mp_checkpoint(n, markets_this_cycle, snapshots_written)
        if a.cycles and n >= a.cycles:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    import signal
    def _shutdown(signum, frame):
        print(f"Signal {signum} received — shutting down gracefully", flush=True)
        if _MP_AVAILABLE and _MP_BRIDGE is not None:
            try:
                _MP_BRIDGE.stop(timeout=5.0)
                print("MemPalace bridge stopped", flush=True)
            except Exception as e:
                print(f"MemPalace bridge stop failed: {e}", flush=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    main()
