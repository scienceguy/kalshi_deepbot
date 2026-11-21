#!/usr/bin/env python3
"""Collect live Kalshi 15M orderbook snapshots for the dry-run growth sim.

The HF history (papylove/kalshi-15m-data) ends 2026-10-11, so a replay on it
is three weeks stale. This collector records what the venue quotes *now*:
per-minute top-of-book bids and depth for every open crypto 15M window, plus
the settlement result appended after close. The output schema matches the HF
parquet so scripts/dryrun_growth_sim.py --collected replays it through the
bot's own code paths.

Row schema (JSONL, one row per ticker per minute):
  series, ticker, minute, open_ts, close_ts, floor_strike,
  yes_bid, yes_ask, yes_vol, no_bid, no_ask, no_vol   # cents / contracts
  result, expiration_value                            # settlement only, minute 15

USAGE:
  TMPDIR=/tmp/opencode python scripts/live_book_collector.py \
      --out live_books.jsonl --minutes 90

Shortcut: settlement is fetched once per ticker, ~120s after close; run
longer than one 15-minute window or no ticker ever settles.
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from kalshi_client import CRYPTO_15M_SERIES, KalshiClient

SCRATCH = Path(os.environ.get("TMPDIR") or "/tmp/opencode")
STATE_PATH = SCRATCH / "live_book_state.json"
# A ticker settles within ~2 min of close; wait for the venue to post it.
SETTLE_DELAY_S = 120


def load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"pending": {}}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state))


def collect(client, out_path, minutes, poll_interval=60):
    """Loop over open windows, recording quotes each minute, then settlement."""
    state = load_state()
    pending = state.setdefault("pending", {})
    t_end = time.time() + minutes * 60
    rows = 0
    with open(out_path, "a") as fh:
        while time.time() < t_end:
            now = datetime.now(timezone.utc)
            written = 0
            for series in sorted(CRYPTO_15M_SERIES):
                try:
                    markets = client.get_markets(series_ticker=series,
                                                 status="open")
                except Exception as e:
                    print("get_markets(%s) failed: %s" % (series, e))
                    continue
                for mkt in markets:
                    ticker = mkt.get("ticker")
                    close = mkt.get("close_time") or ""
                    if not ticker or not close:
                        continue
                    close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
                    delta = (close_dt - now).total_seconds()
                    # Only quote windows closing in 0-15.5 min: older ones are
                    # already quoted by their own cycle, newer ones have no
                    # book yet, and the sim needs minutes 1..15.
                    if not (0 <= delta <= 15.5 * 60):
                        continue
                    open_ts = int(close_dt.timestamp() - 900)
                    close_ts = int(close_dt.timestamp())
                    minute = int((time.time() - open_ts) / 60) + 1
                    if not (1 <= minute <= 15):
                        continue
                    try:
                        ob = client.get_orderbook(ticker, depth=5) or {}
                    except Exception as e:
                        print("get_orderbook(%s) failed: %s" % (ticker, e))
                        continue
                    yes = ob.get("yes") or []
                    no = ob.get("no") or []
                    row = {
                        "series": series,
                        "ticker": ticker,
                        "minute": minute,
                        "open_ts": open_ts,
                        "close_ts": close_ts,
                        "floor_strike": mkt.get("floor_strike") or 0,
                        # Cents: Kalshi's dollar quotes come back as floats,
                        # and the sim's book is in cents (like the HF parquet).
                        "yes_bid": int(round(yes[0][0])) if yes else 0,
                        "yes_ask": (100 - int(round(no[0][0]))) if no else 0,
                        "yes_vol": int(yes[0][1]) if yes else 0,
                        "no_bid": int(round(no[0][0])) if no else 0,
                        "no_ask": (100 - int(round(yes[0][0]))) if yes else 0,
                        "no_vol": int(no[0][1]) if no else 0,
                    }
                    fh.write(json.dumps(row) + "\n")
                    written += 1
                    # Register for the settlement pass once this window closes.
                    pending[ticker] = {
                        "series": series, "open_ts": open_ts,
                        "close_ts": close_ts,
                        "floor_strike": row["floor_strike"],
                        "status": "quoted",
                    }
            # Settlement pass: quote rows only replay once the result is known.
            for ticker, info in list(pending.items()):
                if info["status"] == "settled":
                    continue
                if time.time() < info["close_ts"] + SETTLE_DELAY_S:
                    continue
                try:
                    mkt = client.get_market(ticker)
                    result = str(mkt.get("result") or "").lower()
                    if not result:
                        continue
                    srow = {
                        "series": info["series"],
                        "ticker": ticker,
                        "minute": 15,
                        "open_ts": info["open_ts"],
                        "close_ts": info["close_ts"],
                        "floor_strike": info.get("floor_strike") or 0,
                        "yes_bid": 0, "yes_ask": 0, "yes_vol": 0,
                        "no_bid": 0, "no_ask": 0, "no_vol": 0,
                        "result": result,
                        "expiration_value": mkt.get("expiration_value") or 0,
                    }
                    fh.write(json.dumps(srow) + "\n")
                    written += 1
                    info["status"] = "settled"
                except Exception as e:
                    print("get_market(%s) failed: %s" % (ticker, e))
                # Drop settled tickers so state stays bounded across days.
                if info["status"] == "settled":
                    pending.pop(ticker, None)
            fh.flush()
            rows += written
            state["pending"] = pending
            save_state(state)
            print("tick %s rows=%d (+%d) awaiting_settlement=%d" % (
                datetime.now(timezone.utc).strftime("%H:%M:%S"),
                rows, written, len(pending)), flush=True)
            if t_end - time.time() > poll_interval:
                time.sleep(poll_interval)
    print("collected %d rows -> %s" % (rows, out_path))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(SCRATCH / "live_books.jsonl"),
                    help="JSONL output path")
    ap.add_argument("--minutes", type=float, default=90.0,
                    help="Total collection duration in minutes")
    args = ap.parse_args()
    client = KalshiClient()
    collect(client, args.out, args.minutes)


if __name__ == "__main__":
    main()