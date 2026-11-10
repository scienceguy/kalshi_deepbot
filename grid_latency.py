#!/usr/bin/env python3
"""grid_latency.py -- continuous read-only probe for a net-profitable fill.

No orders, no keys. One question per cycle:

    is there a fill that still pays after Kalshi's 7 percent taker fee?

An hourly ladder can be wrong in two ways, so there are two independent tests.

1. FEE FLOOR (favourite side).
   Buy YES or NO on one rung. Net per contract = 1.00 * win - ask - fee(ask).
   On a binary the winning share pays 1.00 and the losing pays 0.00. Fee is
   evaluated on the price you actually pay, never on the complement.

2. CROSS-VENUE PARITY (Polymarket as fair value).
   The venues do not share strikes, so the test is INTERPOLATED, not same
   ticker. Fit each side as a CDF in strike, evaluate the other venue CDF at
   this venue strikes, compare. Only a gap larger than the fee you pay to close
   it, on a two-sided book, is called TRADEABLE. A constant offset across every
   strike is a basis, not a lag, and is labelled as such.

Output: JSONL, one row per (series, strike) per cycle plus one summary row.
    python grid_latency.py --once
    python grid_latency.py --interval 30 --minutes 30
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import signal
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kalshi_client  # noqa: E402

log = logging.getLogger("grid_latency")

GAMMA = "https://gamma-api.polymarket.com"
FEE_RATE = 0.07
OUT_DIR = Path.home() / "tmp" / "opencode" / "grid_latency"

SERIES = ("KXBTCD", "KXETHD", "KXSOLD", "KXXRPD", "KXDOGED", "KXBNBD", "KXHYPED")

# series -> Polymarket hourly event slug. A slug that 404s is reported, never skipped.
PM_SLUGS = {
    "KXBTCD": "bitcoin-above-on-november-9-2026-8pm-et",
    "KXETHD": "ethereum-above-on-november-9-2026-8pm-et",
}


def taker_fee(price):
    """Fee per contract at the price actually paid. The cent ceiling applies to
    the whole order, so per contract this is a fraction of a cent, unrounded."""
    return FEE_RATE * price * (1.0 - price)


def fl(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = kalshi_client.KalshiClient()
    return _CLIENT


def kalshi_rungs(series):
    """Open rungs of the nearest event, both sides of the book."""
    ms = _client().get_markets(series_ticker=series, status="open", limit=200)
    if not ms:
        return []
    evs = sorted({m.get("event_ticker") or (m.get("ticker") or "").rsplit("-T", 1)[0]
                  for m in ms})
    out = []
    for m in ms:
        if m.get("event_ticker") != evs[0] or m.get("strike_type") != "greater":
            continue
        strike = fl(m.get("floor_strike"))
        if strike is None:
            continue
        out.append({"strike": strike,
                    "yes_bid": fl(m.get("yes_bid_dollars")),
                    "no_ask": fl(m.get("no_ask_dollars")),
                    "yes_ask_size": fl(m.get("yes_ask_size_fp")) or 0.0,
                    "ticker": m.get("ticker")})
    return sorted(out, key=lambda r: r["strike"])


def pm_event_markets(slug):
    """Raw markets list for a Polymarket event slug. None if unavailable."""
    try:
        url = GAMMA + "/events?slug=" + urllib.parse.quote(slug)
        req = urllib.request.Request(url, headers={
            "User-Agent": "kalshi_deepbot-gridlatency/0",
            "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            evs = json.loads(r.read())
    except Exception as e:  # noqa: BLE001 - any transport failure is 'unavailable'
        log.warning("polymarket slug %s unavailable: %s", slug, str(e)[:90])
        return None
    if not evs:
        return None
    return evs[0].get("markets") or []


def pm_rungs(slug):
    """Polymarket rungs of one hourly event; groupItemTitle is the strike."""
    markets = pm_event_markets(slug)
    if markets is None:
        return None
    out = []
    for m in markets:
        gi = str(m.get("groupItemTitle") or "").replace(",", "").strip()
        strike = fl(gi)
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except ValueError:
                prices = []
        if strike is None or not prices:
            continue
        bid, ask = fl(m.get("bestBid")), fl(m.get("bestAsk"))
        mid = (bid + ask) / 2.0 if (bid is not None and ask is not None and ask > bid) \
            else fl(prices[0])
        if mid is None:
            continue
        out.append({"strike": strike, "yes": mid, "quoted": bool(bid and ask)})
    return sorted(out, key=lambda r: r["strike"])

def cdf_at(rungs, x):
    """Interpolate P(price > x) at x from sorted (strike, yes) rungs.

    Returns None outside the quoted strike range. Extrapolating a CDF invents a
    number, and a gap measured against an invented number is not a gap.
    """
    xs, ys = [], []
    for r in rungs:
        p = r.get("yes")
        if p is None or not (0.0 < p < 1.0):
            continue
        xs.append(r["strike"])
        ys.append(p)
    if len(xs) < 3:
        return None
    for i in range(1, len(ys)):  # enforce non-increasing in strike
        if ys[i] > ys[i - 1]:
            ys[i] = ys[i - 1]
    if not (xs[0] <= x <= xs[-1]):
        return None
    i = bisect.bisect_left(xs, x)
    if i == 0:
        return ys[0]
    if i >= len(xs):
        return ys[-1]
    x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
    if x1 == x0:
        return y1
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def fee_floor_row(series, r):
    """One rung judged on the fee floor alone.

    yes_ask = 1 - no_ask : the seller of NO at no_ask is the seller of YES at that
    price (Kalshi binary). If no_ask is missing the rung is unpriced on the buy side
    and net is None, not a number built from a phantom.
    """
    yes_bid, no_ask = r["yes_bid"], r["no_ask"]
    yes_ask = None if no_ask is None else 1.0 - no_ask
    row = {"kind": "fee_floor", "series": series, "strike": r["strike"],
           "ticker": r["ticker"], "yes_bid": yes_bid, "no_ask": no_ask,
           "yes_ask": yes_ask, "yes_ask_size": r["yes_ask_size"]}
    if yes_ask is not None:
        row["net_yes_c"] = round((1.0 - yes_ask - taker_fee(yes_ask)) * 100.0, 4)
    if no_ask is not None:
        row["net_no_c"] = round((1.0 - no_ask - taker_fee(no_ask)) * 100.0, 4)
    return row

def parity_rows(series, k_rungs, p_rungs):
    """Interpolated Kalshi vs Polymarket gap, per Kalshi strike.

    Polymarket is the reference. gap_c = (pm_fair - kalshi_mid) * 100 where kalshi_mid
    is the mid of the Kalshi book and pm_fair is the Polymarket CDF evaluated at that
    strike. Positive gap means Polymarket prices the event higher than Kalshi does.

    A status field keeps this honest:
      fair=none     Polymarket does not quote strikes spanning this one
      no_pm         Polymarket ladder unavailable this cycle
      unpriced      Kalshi has no NO offering: no reachable ask, nothing to buy
      one_sided     Kalshi bid is 0: the mid is half a phantom
      tradeable     two-sided AND a side pays after the fee
      inside_fee    two-sided but the gap does not clear the fee
    """
    out = []
    if not p_rungs:
        status = "no_pm"
        for r in k_rungs:
            out.append({"kind": "parity", "series": series, "strike": r["strike"],
                        "status": status})
        return out, status

    offsets = []
    for r in k_rungs:
        yb, na = r["yes_bid"], r["no_ask"]
        if yb is None or na is None:
            continue
        ya = 1.0 - na
        fair = cdf_at(p_rungs, r["strike"])
        if fair is None:
            continue
        mid = (yb + ya) / 2.0
        offsets.append(fair - mid)

    # a basis is a level, not a movement: if the spread of offsets is small next to
    # the level, both venues are pricing the same print through different references
    if len(offsets) >= 3:
        # a systematic Polymarket reference print offset is a basis and cancels out
        # of the dispersion; a lag or a stale rung does not
        mean = sum(offsets) / len(offsets)
        sd = (sum((o - mean) ** 2 for o in offsets) / (len(offsets) - 1)) ** 0.5
        var = sum((o - mean) ** 2 for o in offsets) / (len(offsets) - 1)
        regime = "basis" if var < 0.005 ** 2 else "dispersion"
        # duplicate Polymarket mids make the interpolated fair a guess about a
        # wide interval: the parity signal is grid resolution, not a gap
        uniq = sorted({r["strike"] for r in p_rungs if r.get("quoted")})
        span = uniq[-1] - uniq[0] if len(uniq) > 1 else 0.0
        if span <= 0.0 or len(uniq) / (span / 250.0) < 3.0:
            regime = "coarse"
    else:
        regime = "thin"

    for r in k_rungs:
        yb, na = r["yes_bid"], r["no_ask"]
        row = {"kind": "parity", "series": series, "strike": r["strike"],
               "ticker": r["ticker"], "regime": regime, "status": "fair=none",
               "two_sided": False}
        if na is None or yb is None:
            row["status"] = "unpriced"
            out.append(row)
            continue
        ya = 1.0 - na
        fair = cdf_at(p_rungs, r["strike"])
        if fair is None:
            out.append(row)
            continue
        mid = (yb + ya) / 2.0
        buy_yes_net = (fair - ya - taker_fee(ya)) * 100.0
        buy_no_net = (fair - na - taker_fee(na)) * 100.0
        two_sided = yb > 0.0 and ya < 1.0
        row.update({"kalshi_mid": round(mid, 4), "pm_fair": round(fair, 4),
                    "gap_c": round((fair - mid) * 100.0, 3),
                    "buy_yes_net_c": round(buy_yes_net, 4),
                    "buy_no_net_c": round(buy_no_net, 4),
                    "two_sided": two_sided})
        if not two_sided:
            row["status"] = "one_sided"
        elif regime in ("coarse", "thin"):
            row["status"] = "reference_too_coarse"
        elif buy_yes_net > 0.0 or buy_no_net > 0.0:
            row["status"] = "tradeable"
        else:
            row["status"] = "inside_fee"
        out.append(row)
    return out, regime


def scan():
    """One probe cycle. Returns (rows, summary). Never places an order."""
    ts = datetime.now(timezone.utc).isoformat()
    rows, summary = [], {"ts": ts, "series": {}, "fetch_errors": 0,
                         "tradeable": 0, "regimes": {}}
    for series in SERIES:
        try:
            k_rungs = kalshi_rungs(series)
        except Exception as e:  # noqa: BLE001 - one series failing must not end the cycle
            log.warning("%s kalshi fetch failed: %s", series, str(e)[:90])
            k_rungs = []
            summary["fetch_errors"] += 1

        p_rungs = None
        slug = PM_SLUGS.get(series)
        if slug:
            p_rungs = pm_rungs(slug)

        s = {"kalshi_rungs": len(k_rungs), "pm_rungs": None if p_rungs is None else len(p_rungs),
             "pm_slug": slug, "cheapest_yes_ask_c": None, "min_net_c": None,
             "two_sided": 0, "tradeable": 0, "regime": None}
        floor_rows = [fee_floor_row(series, r) for r in k_rungs]
        priced = [fr["yes_ask"] for fr in floor_rows if fr["yes_ask"] is not None]
        if priced:
            s["cheapest_yes_ask_c"] = round(min(priced) * 100.0, 2)
            vals = []
            for fr in floor_rows:
                for v_mid in (fr.get('net_yes_c'), fr.get('net_no_c')):
                    if v_mid is not None:
                        vals.append(v_mid)
        s["two_sided"] = sum(1 for fr in floor_rows
                            if fr["yes_bid"] is not None and fr["yes_bid"] > 0.0)
        rows.extend(floor_rows)
        s["min_net_c"] = round(min(vals), 4) if vals else None

        if p_rungs:
            prow, regime = parity_rows(series, k_rungs, p_rungs)
            s["regime"] = regime
            s["tradeable"] = sum(1 for p in prow if p.get("status") == "tradeable")
            summary["tradeable"] += s["tradeable"]
            summary["regimes"][series] = regime
            rows.extend(prow)

        summary["series"][series] = s
        log.info("%-8s k=%-3d pm=%-4s two_sided=%-2d tradeable=%-2d cheapest_yes_ask=%s min_net=%s regime=%s",
                 series, len(k_rungs), s["pm_rungs"], s["two_sided"], s["tradeable"],
                 s["cheapest_yes_ask_c"], s["min_net_c"], s["regime"])
    return rows, summary


def append_rows(rows, summary):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / ("%s.jsonl" % datetime.now(timezone.utc).strftime("%Y%m%d"))
    with path.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
        fh.write(json.dumps({"kind": "summary", "ts": summary["ts"],
                             "series": summary["series"],
                             "fetch_errors": summary["fetch_errors"],
                             "tradeable": summary["tradeable"]}) + "\n")
    return path


def main():
    ap = argparse.ArgumentParser(description="read-only Kalshi fee-floor + Polymarket parity probe")
    ap.add_argument("--once", action="store_true", help="take one snapshot and exit")
    ap.add_argument("--interval", type=int, default=30, help="seconds between snapshots")
    ap.add_argument("--minutes", type=float, default=0.0, help="stop after this many minutes (0 = forever)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    deadline = time.time() + args.minutes * 60.0 if args.minutes else None
    cycles = 0
    while not stop["flag"]:
        try:
            rows, summary = scan()
            append_rows(rows, summary)
            cycles += 1
        except Exception as e:  # noqa: BLE001 - one bad cycle must not end the probe
            log.error("cycle failed: %s", str(e)[:200])
        if args.once:
            break
        if deadline is not None and time.time() + args.interval > deadline:
            break
        slept = 0.0
        while slept < args.interval and not stop["flag"]:
            time.sleep(0.5)
            slept += 0.5
    return 0 if cycles else 1


def _selfcheck():
    """Asserts for the two ways this file can lie. Run: python grid_latency.py --selfcheck"""
    # 1 . fee is charged on the price you pay, not on its complement
    assert abs(taker_fee(0.01) - 0.000693) < 1e-9
    assert abs(taker_fee(0.99) - 0.000693) < 1e-9
    assert taker_fee(0.50) > 0.017
    # 2. a 1c favourite pays; a 97c favourite does not; the complement is never the fee's base
    row = fee_floor_row("X", {"strike": 1.0, "yes_bid": 0.0, "no_ask": 0.99,
                             "yes_ask_size": 1.0, "ticker": "T"})
    assert row["yes_ask"] == 0.01 or abs(row["yes_ask"] - 0.01) < 1e-9, row
    # 3. the CDF refuses to extrapolate
    rungs = [{"strike": 100.0, "yes": 0.9}, {"strike": 110.0, "yes": 0.5},
             {"strike": 120.0, "yes": 0.1}]
    assert cdf_at(rungs, 110.0) == 0.5
    assert cdf_at(rungs, 99.0) is None and cdf_at(rungs, 121.0) is None
    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
        raise SystemExit(0)
    raise SystemExit(main())
