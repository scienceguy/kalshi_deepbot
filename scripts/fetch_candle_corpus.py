"""Fetch historical KXBTC15M 1-minute bid/ask candles into a local corpus.

Run once, resumable. Afterwards scripts/replay_trend.py drives the real
trend_maker.TrendMakerEngine over this corpus with no network access.

Read-only: KalshiClient only GETs /historical/*. No order is ever placed.

The market window is derived from the candles themselves (open_ts..close_ts
from end_period_ts), not from the market's open_time/close_time strings, so it
cannot drift with a timezone misreading.
"""
import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import binance_feed  # noqa: E402
from kalshi_client import KalshiClient  # noqa: E402

# realised_sigma_frac wants TREND_MAKER_SIGMA_CANDLES+1 closes ending now, plus
# slack for the longest lookback window. One hour of 1m klines covers both.
SPOT_BUFFER_S = 3600


def _done_tickers(path: Path) -> set:
    done = set()
    if not path.exists():
        return done
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line)["ticker"])
            except Exception:
                continue
    return done


def fetch_one(client: KalshiClient, market: dict) -> dict | None:
    ticker = market.get("ticker")
    if not ticker:
        return None
    # Wide probe window: the API clips to the market's real lifetime.
    start = int(datetime.fromisoformat(
        market["open_time"].replace("Z", "+00:00")).timestamp())
    end = int(datetime.fromisoformat(
        market["close_time"].replace("Z", "+00:00")).timestamp())
    raw = client.get_market_candlesticks(
        ticker, start, end, period_interval=1, limit=100)
    candles = raw.get("candlesticks") or []
    if len(candles) < 3:
        return None
    open_ts = int(candles[0]["end_period_ts"])
    close_ts = int(candles[-1]["end_period_ts"])
    spot = binance_feed.get_klines_range(
        "BTCUSDT",
        (open_ts - SPOT_BUFFER_S) * 1000,
        close_ts * 1000,
        interval="1m",
        timeout=30,
    ) or []
    return {
        "ticker": ticker,
        "series": market.get("series_ticker") or "KXBTC15M",
        "strike": market.get("floor_strike"),
        "expiration_value": market.get("expiration_value"),
        "result": market.get("result"),
        "open_ts": open_ts,
        "close_ts": close_ts,
        "candles": candles,
        "spot": [
            {"ts": int(k[0]) // 1000, "open": float(k[1]),
             "high": float(k[2]), "low": float(k[3]),
             "close": float(k[4])}
            for k in spot
        ],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXBTC15M")
    ap.add_argument("--markets", type=int, default=400)
    ap.add_argument("--out", default="data/candles_kxbtc15m.jsonl")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = _done_tickers(out) if args.resume else set()

    client = KalshiClient()
    markets = client.fetch_all_historical_markets(args.series, limit=args.markets)
    print("listed %d markets" % len(markets), file=sys.stderr, flush=True)

    kept, skipped, no_spot = 0, 0, 0
    with out.open("a" if args.resume else "w") as fh:
        for i, market in enumerate(markets, 1):
            ticker = market.get("ticker")
            if not ticker or ticker in done:
                continue
            try:
                rec = fetch_one(client, market)
            except Exception:
                traceback.print_exc()
                time.sleep(2)
                continue
            if rec is None:
                skipped += 1
                continue
            if len(rec["spot"]) < 31:
                no_spot += 1
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            kept += 1
            if kept % 25 == 0:
                print("kept %d / seen %d / skipped %d / thin-spot %d"
                      % (kept, i, skipped, no_spot), file=sys.stderr, flush=True)

    print("wrote %d records to %s (skipped %d, thin-spot %d)"
          % (kept, out, skipped, no_spot), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
