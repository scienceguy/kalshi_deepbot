"""Replay the real trend maker over historical 1-minute bid/ask candles.

Drives trend_maker.TrendMakerEngine unchanged: same TrendTracker, same
fair_yes (gbm + momentum), same decide, same fill / stop / TP / flatten rules,
same taker fees from fees.py. Only network-backed inputs are substituted:

  * Market         - CandleMarket, carrying that minute's bid/ask
  * engine clock   - now_fn advanced one minute per step
  * gates.spot_for - BTC close at that minute
  * realized_sigma_frac - close-to-close sigma over the prior N 1m closes

No orders are possible: the engine holds no Kalshi client, and this script
never constructs one. It only reads a local corpus file.

Writes the same JSONL schema trend_maker writes live, so scripts/trend_report.py
and the Workstream A gate apply unchanged.

Usage:
  python scripts/replay_trend.py --corpus data/candles_kxbtc15m.jsonl \
      --out trend_maker_log.jsonl --start-hour 8 --end-hour 22
"""
import argparse
import json
import math
import sys

from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import gates  # noqa: E402
import trend_maker as tm  # noqa: E402


def load_corpus(path: Path) -> list:
    rows = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if len(rec.get("candles") or []) < 5:
                continue
            rows.append(rec)
    rows.sort(key=lambda r: r["open_ts"])
    return rows


def sigma_frac(closes: list):
    """Std-dev of 1m log returns, same formula as trend_maker."""
    if len(closes) < 5:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])
            if a > 0 and b > 0]
    if len(rets) < 4:
        return None
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def merge_spot(recs: list) -> list:
    """One global BTC series keyed by ts, sorted. Spot is not per-market."""
    by_ts = {}
    for rec in recs:
        for s in rec.get("spot") or []:
            ts = int(s["ts"])
            if ts not in by_ts:
                by_ts[ts] = float(s["close"])
    return sorted((ts, close) for ts, close in by_ts.items())


def closes_at(spot_series: list, ts: int, n: int) -> list:
    """The n most recent closes at or before ts, oldest first."""
    return [close for s_ts, close in spot_series if s_ts <= ts][-n:]


class CandleMarket:
    """One market at one minute: the attributes trend_maker reads."""

    __slots__ = ("ticker", "series", "strike", "yes_bid", "yes_ask",
                 "no_bid", "no_ask", "minutes_left", "mid_yes")

    def __init__(self, ticker, series, strike, yes_bid, yes_ask, minutes_left):
        self.ticker = ticker
        self.series = series
        self.strike = strike
        self.yes_bid = yes_bid
        self.yes_ask = yes_ask
        # Kalshi quotes YES; NO is the complement of the YES book.
        self.no_bid = None if yes_ask is None else 100 - yes_ask
        self.no_ask = None if yes_bid is None else 100 - yes_bid
        self.minutes_left = minutes_left
        if (yes_bid is not None and yes_ask is not None
                and yes_ask > yes_bid):
            self.mid_yes = (yes_bid + yes_ask) / 2.0
        else:
            self.mid_yes = None


def candle_for(rec: dict, minute_ts: int):
    """The candle whose close is at the end of this minute, or None."""
    for c in rec["candles"]:
        if int(c["end_period_ts"]) == minute_ts:
            return c
    return None


def valid_quote(cur: dict):
    """(yes_bid_cents, yes_ask_cents) or None for a settlement/insane print."""
    yb = round(float(cur["yes_bid"]["close"]) * 100)
    ya = round(float(cur["yes_ask"]["close"]) * 100)
    if yb >= 99 or ya >= 99:      # final settlement print, not a quote
        return None
    if yb <= 0 or ya <= 0:
        return None
    if not (yb < ya):
        return None
    return yb, ya


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="data/candles_kxbtc15m.jsonl")
    ap.add_argument("--out", default="trend_maker_log.jsonl")
    ap.add_argument("--start-hour", type=int, default=0)
    ap.add_argument("--end-hour", type=int, default=24)
    ap.add_argument("--balance", type=float, default=100.0)
    args = ap.parse_args(argv)

    corpus = Path(args.corpus)
    if not corpus.exists():
        print("missing corpus: %s" % corpus, file=sys.stderr)
        return 2
    recs = load_corpus(corpus)

    keep = []
    for rec in recs:
        hour = datetime.fromtimestamp(rec["open_ts"], timezone.utc).hour
        if args.start_hour <= hour < args.end_hour:
            keep.append(rec)
    recs = keep
    if not recs:
        print("no markets in the %02d-%02d UTC window"
              % (args.start_hour, args.end_hour), file=sys.stderr)
        return 2
    print("replaying %d markets, %02d-%02d UTC"
          % (len(recs), args.start_hour, args.end_hour), file=sys.stderr)

    out = Path(args.out)
    if out.exists():
        out.unlink()

    now = [0.0]
    engine = tm.TrendMakerEngine(log_path=out, now_fn=lambda: now[0])

    # sigma/spot caches are keyed by asset in trend_maker, so patch per step.
    span = config.TREND_MAKER_SIGMA_CANDLES + 1

    spot_series = merge_spot(recs)
    if not spot_series:
        print("corpus has no spot rows", file=sys.stderr)
        return 2

    first = recs[0]["open_ts"]
    last = max(r["close_ts"] for r in recs)
    t = first
    steps = 0
    while t <= last:
        now[0] = float(t)
        live = [r for r in recs if r["open_ts"] < t <= r["close_ts"]]
        frame = []
        for rec in live:
            cur = candle_for(rec, t)
            if cur is None:
                continue
            quote = valid_quote(cur)
            if quote is None:
                continue
            minutes_left = (rec["close_ts"] - t) / 60.0
            if minutes_left <= 0:
                continue
            frame.append(CandleMarket(
                rec["ticker"], rec.get("series", "KXBTC15M"), rec["strike"],
                quote[0], quote[1], minutes_left))

        if frame and recs:
            closes = closes_at(spot_series, t, span)
            spot = closes[-1] if closes else None
            sig = sigma_frac(closes)
            gates.spot_for = lambda asset, _s=spot: _s
            tm.realized_sigma_frac = lambda asset, _g=sig: _g
            engine.step(frame, balance=args.balance)
            steps += 1
        t += 60

    rows = out.read_text().splitlines() if out.exists() else []
    print("steps=%d log_rows=%d -> %s" % (steps, len(rows), out), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
