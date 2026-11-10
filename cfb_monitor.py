"""cfb_monitor: read-only dry-run monitor of the Kalshi CF Benchmarks (BRTI) passthrough.

Measures settlement BRTI, ladder lock vs the current print, and fee-adjusted edge.
Read-only: no orders, no credentials of its own. Use --selfcheck for asserts."""

from __future__ import annotations

import argparse
import json
import logging
import math
import pathlib
import re
import time
from datetime import datetime, timezone


log = logging.getLogger("cfb_monitor")

CFB_BASE = "https://www.cfbenchmarks.com/api/v1"
CFB_INDEX = "BRTI"
KALSHI_PROD = "https://external-api.kalshi.com/trade-api/v2"
OUT_DIR = pathlib.Path(__file__).resolve().parent / "data" / "cfb"

# KXBTCD-26NOV0910 / KXETHD-26NOV0910 / KXBTC15M-26NOV090915 (Eastern wall time)
HOURLY_RE = re.compile(r"^KX(BTC|ETH)(?:D|15M)-(\d{2})([A-Z]{3})(\d{2})(\d{2})$")
SERIES = ("KXBTCD", "KXETHD")

FEE_RATE = 0.07
# The per-trade cap the EV arithmetic is sized to. config.PER_TRADE_PCT
# disagrees (default 0.05, "quarter-Kelly-ish"); 0.08 is the value measured
# for this bankroll. If sizes change, change this, not side_ev_c.
BANKROLL = 12.55
PER_TRADE_PCT = 0.08
MONTHS = dict((m, i + 1) for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]))


def round_fee_usd(model_fee):
    # Kalshi rounds the fee to the cent, UP, once. Do not pre-round
    # (0.0033 c -> 1 c -> $0.01) or a DERIVATIVE taker fee inflates 100x.
    return math.ceil(model_fee * 100.0 - 1e-9) / 100.0


def taker_fee(price):
    # dollars of taker fee per ONE contract, on the price actually paid
    p = min(max(float(price), 0.0), 1.0)
    return round_fee_usd(FEE_RATE * p * (1.0 - p))


def sigma_avg_factor(n=60):
    # Var(mean of the n equal 1s slices) / Var(the full minute's move)
    #   = (n+1)(2n+1) / (6 n^2);  n=60 -> 0.3402, sqrt 0.5832.
    # Degree of freedom in the printed minute-average, not a forecast of BRTI.
    # With no prints known yet, use this; with prints held, use p_above_held.
    r = (n + 1.0) * (2.0 * n + 1.0) / (6.0 * n * n)
    return math.sqrt(r)


def _norm_sf(x):
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def iso_ms(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_hour_ms(ts_ms):
    dt = datetime.fromtimestamp(ts_ms / 1000.0, timezone.utc)
    return int(dt.replace(minute=0, second=0, microsecond=0).timestamp() * 1000)


def settlement_window(prints, close_ms):
    # The 60 one-second prints a Kalshi hourly contract averages: the minute
    # is [close - 60_000, close). Half-open; a print stamped exactly at close
    # belongs to the next contract, not this one.
    lo, hi = close_ms - 60_000, close_ms
    keep = [p for p in prints if lo <= int(p["time"]) < hi]
    keep.sort(key=lambda p: int(p["time"]))
    return keep


def brti_avg(prints):
    if not prints:
        return None
    return sum(float(p["value"]) for p in prints) / float(len(prints))


def _kalshi_client():
    try:
        import kalshi_client
        return kalshi_client.KalshiClient()
    except Exception as eDrop:
        log.warning("kalshi client unavailable: %s", str(eDrop)[:160])
        return None


def fetch_history_hour(client, hour_start_ms, timeout=20):
    # 5 Hz tick level prints for one hour through the CFB history passthrough.
    # IMPORTANT: this is only available to entitled accounts, picked up here
    # by reusing the existing signed client so no new credential is stored.
    if client is None:
        return None
    try:
        r = client._request("GET", "/cfbenchmarks/history/values",
                            params={"id": CFB_INDEX, "timespan": "HOUR",
                                    "timestamp": iso_ms(hour_start_ms)},
                            timeout=timeout)
    except Exception as e:
        log.warning("history hour %s: %s", iso_ms(hour_start_ms), str(e)[:160])
        return None
    pl = ((r or {}).get("data") or {}).get("payload")
    return pl if isinstance(pl, list) and pl else None


def fetch_rolling_hour(client, timeout=15):
    # last 3600 1-second fr prints, the whole settlement input for the
    # contract closing next
    if client is None:
        return None
    try:
        r = client._request("GET", "/cfbenchmarks/values",
                            params={"id": CFB_INDEX}, timeout=timeout)
    except Exception as e:
        log.warning("rolling hour: %s", str(e)[:160])
        return None
    pl = ((r or {}).get("data") or {}).get("payload")
    if isinstance(pl, dict):
        return [pl]
    return pl if isinstance(pl, list) and pl else None


def _mid_cents(market, book):
    def _p(side, key):
        v = side.get(key)
        if v is None:
            return None
        try:
            return float(v / 100.0)
        except Exception:
            return None

    yes = _p(book.get("yes") or {}, "bid")
    no_b = _p(book.get("no") or {}, "bid")
    if yes is not None and no_b is not None:
        return (yes + (1.0 - no_b)) / 2.0
    lv = market.get("last_price")
    if lv not in (None, ""):
        return float(lv / 100.0)
    return None


def fetch_ladder(client, series, target_close_ms, timeout=15):
    # target_close_ms is REQUIRED: several KXBTCD events are open at once
    # (hourly, next day, weekly). A strike repeated across expiries is not one
    # rung, and mixing them scores several markets as if they shared a result.
    ms = client.get_markets(series, status="open", limit=1000) or []
    rows = []
    for m in ms:
        try:
            strike = float(m["floor_strike"])
            yes_ask = float(m["yes_ask_dollars"])
            yes_bid = float(m["yes_bid_dollars"])
            no_ask = float(m["no_ask_dollars"])
            no_bid = float(m["no_bid_dollars"])
            cm = parse_close_ms(m.get("close_time") or "", 0)
            ticker = str(m.get("ticker"))
        except (KeyError, TypeError, ValueError):
            continue
        if cm is None or cm != target_close_ms:
            continue
        rows.append({
            "ticker": ticker,
            "src": "MARKET" if m.get("yes_sub_title") else "MODEL",
            "strike": strike,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "close_ms": cm,
        })
    return rows

def parse_close_ms(s, now_ms):
    # Kalshi close_time is ISO Z. Some series carry a far-dated placeholder;
    # caller decides whether the event ticks this hour.
    try:
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        return int(dt.replace(tzinfo=timezone.utc).timestamp()) * 1000
    except Exception:
        return None


def p_above_held(spot, strike, sigma, secs_held):
    """P(avg60 > strike) if the index holds at spot for secs_held seconds.

    The held seconds are known. The rest still move with the same sigma.
    Solve  spot*k + x*(60-k) > strike*60  for the mean x the rest needs.
    The remaining (60-k) seconds average has lognormal std sigma*sqrt((60-k)/180000).
    """
    held = float(secs_held)
    if held >= 60.0:
        return 1.0 if spot > strike else 0.0
    n_left = 60.0 - held
    need = (strike * 60.0 - spot * held) / n_left
    if need <= 0.0:
        return 1.0
    if spot <= 0.0 or sigma <= 0.0:
        return float("nan")
    sd = sigma * (min(n_left, 60.0) / 60.0) ** 0.5
    return _norm_sf(math.log(need / spot) / sd)




def lock_table(ladder, spot, sigma, secs_held):
    """Per rung: p if the rest of the minute prints flat, and the 45deg p.

    p_held is what the rung is worth when the unprinted slice adds nothing new.
    p_diff applies the full remaining dispersion. lock is 1 - |d - p| / room,
    where room is the probability space between the two neighbouring strikes,
    so a rung with no room left in the grid is skipped rather than reported 1.0.
    """
    pts = sorted(ladder, key=lambda r: r["strike"])
    if len(pts) < 3:
        return []
    mins = max(60.0 - float(secs_held), 1.0) / 60.0
    out = []
    for i in range(1, len(pts) - 1):
        lo, mid, hi = pts[i - 1], pts[i], pts[i + 1]
        if hi["strike"] <= lo["strike"]:
            continue
        p = p_above_held(spot, mid["strike"], sigma, secs_held)
        d = prob_avg_above(spot, mid["strike"], sigma, mins)
        room = abs(p_above_held(spot, hi["strike"], sigma, secs_held)
                   - p_above_held(spot, lo["strike"], sigma, secs_held))
        if room < 1e-9:
            continue
        lock = 1.0 - min(1.0, abs(d - p) / room)
        ask = mid.get("yes_ask")
        out.append({
            "strike": mid["strike"],
            "ask": ask,
            "bid": mid.get("yes_bid"),
            "p_held": round(p, 4),
            "p_diff": round(d, 4),
            "lock": round(lock, 4),
            "gap_c": None if ask is None else round(100.0 * (ask - p), 2),
        })
    return out
def sigma_avg(sigma_per_min, n=60):
    # sigma of the average of n equal 1s slices of one minute, vs the
    # sigma of the full minute: sqrt((n+1)(2n+1)/(6 n n)). n=60 -> 0.5832.
    # This is the degree of freedom in the printed average, not a forecast:
    # averaging n slices of one Brownian path removes 1/3 of the variance.
    return sigma_per_min * math.sqrt((n + 1.0) * (2.0 * n + 1.0) / (6.0 * n * n))


def prob_avg_above(spot, strike, sigma_per_min, minutes_left=1.0):
    """P(avg60 above strike), 45deg reference: the residual prints diffuse.

    minutes_left is a multiple of one minute (never use it to shorten the
    minute: the average is always over the same 60 seconds).
    """
    if spot <= 0 or strike <= 0 or minutes_left <= 0:
        return float("nan")
    sd = sigma_avg(sigma_per_min) * (float(minutes_left) ** 0.5)
    if sd <= 0:
        return 1.0 if spot > strike else 0.0
    return _norm_sf(math.log(spot / strike) / sd)

def _fee_usd(n, price):
    # Kalshi charges 0.07*n*p*(1-p) and rounds UP to the cent ONCE PER ORDER,
    # not per contract. An order is sized by BANKROLL*PER_TRADE, never by
    # "however many we feel like", so n is derived here and used everywhere.
    p = min(max(float(price), 0.0), 1.0)
    n = max(1, int(n))
    return round_fee_usd(FEE_RATE * n * p * (1.0 - p))


def order_contracts(price, bankroll=None, per_trade=None):
    price = min(max(float(price), 0.01), 0.99)
    budget = (BANKROLL if bankroll is None else float(bankroll)) * \
             (PER_TRADE_PCT if per_trade is None else float(per_trade))
    n = 1
    while n < 50:
        if n * price + _fee_usd(n, price) > budget + 1e-12:
            return n - 1
        n += 1
    return 50


def side_ev_c(win_p, ask, n=None):
    n = order_contracts(ask) if n is None else max(1, int(n))
    fee = _fee_usd(n, ask) / float(n)
    return 100.0 * (win_p * (1.0 - ask) - (1.0 - win_p) * ask - fee)

def score_row(p, yes_ask, no_ask):
    out = dict(ev_yes_c=None, ev_no_c=None, best=None, best_c=None, actionable=False)
    if p is None:
        return out
    if yes_ask is not None and 0.0 < yes_ask < 1.0:
        out["ev_yes_c"] = round(side_ev_c(p, yes_ask), 3)
    if no_ask is not None and 0.0 < no_ask < 1.0:
        out["ev_no_c"] = round(side_ev_c(1.0 - p, no_ask), 3)
    cand = [k for k in ("ev_yes_c", "ev_no_c") if out[k] is not None]
    if cand:
        best = max(cand, key=lambda k: out[k])
        out["best"] = "yes" if best == "ev_yes_c" else "no"
        out["best_c"] = out[best]
        out["actionable"] = out["best_c"] > 0.0
    return out


def rows_from(snapshot):
    rows = []
    secs_held = snapshot.get("secs_held")
    for series, data in (snapshot.get("series") or {}).items():
        for lr in data.get("ladder") or []:
            p = lr.get("p")
            if p is None:
                continue
            score = score_row(p, lr.get("yes_ask"), lr.get("no_ask"))
            rows.append(dict(
                ts=snapshot.get("ts"), series=series, ticker=lr.get("ticker"),
                strike=lr.get("strike"), yes_bid=lr.get("yes_bid"), yes_ask=lr.get("yes_ask"),
                no_bid=lr.get("no_bid"), no_ask=lr.get("no_ask"),
                p=round(p, 4), secs_held=secs_held, **score
            ))
    return rows
def append_rows(rows, summary, out_dir=None):
    d = pathlib.Path(out_dir) if out_dir else OUT_DIR
    d.mkdir(parents=True, exist_ok=True)
    day = datetime.fromtimestamp(time.time(), timezone.utc).strftime("%Y%m%d")
    path = d / ("%s.jsonl" % day)
    with path.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + chr(10))
        fh.write(json.dumps(dict(kind="summary", **summary)) + chr(10))
    return path


def nearest_close_ms(now_ms):
    # the next whole-hour close, at :00:00 on the minute boundary, in UTC ms
    return (int(now_ms) // 3600000 + 1) * 3600000


def secs_held(close_ms, now_ms):
    # seconds of the settlement minute already printed: 0 just after the
    # hour opens, 60 at the close; negative before the minute begins
    return int(round((int(now_ms) - (int(close_ms) - 60000)) / 1000.0))


def soonest_close(client, series=SERIES):
    # The settlement minute the monitor can observe: the soonest close_time
    # in the ladder whose minute has already STARTED (now >= close - 60s).
    #
    # Plain "next close after now" is wrong here. While the 22:00Z event is
    # also open, the soonest FUTURE close is 22:00Z, so the monitor would sit
    # in pre_window through every hourly close and never score one at all.
    now_ms = int(time.time() * 1000)
    if isinstance(series, str):
        series = (series,)
    seal = None
    for s in series:
        for m in client.get_markets(s, status="open", limit=1000) or []:
            cm = parse_close_ms(m.get("close_time") or "", 0)
            if cm is not None and now_ms >= cm - 60_000:
                if seal is None or cm < seal:
                    seal = cm
    return seal


def scan(client, sigma_per_min=0.0009):
    now_ms = int(time.time() * 1000)
    close_ms = soonest_close(client)
    snap = dict(ts=now_ms, close_ms=close_ms, secs_held=0, series={}, status="")
    if close_ms is None:
        snap["status"] = "no_closing_event"
        return snap, 0, len(SERIES)

    held = min(max(secs_held(close_ms, now_ms), 0), 59)
    snap["secs_held"] = held
    errs = 0

    prints = fetch_history_hour(client, parse_hour_ms(close_ms - 60_000))
    spot = None
    if prints:
        spot = brti_avg(settlement_window(prints, close_ms))
    if spot is None:
        roll = fetch_rolling_hour(client) or []
        if roll:
            spot = float(roll[-1]["value"])
    if spot is None:
        snap["status"] = "no_prints"
        return snap, held, errs + 1

    for series in SERIES:
        ladder = fetch_ladder(client, series, close_ms)
        if not ladder:
            continue
        for r in ladder:
            r["p"] = p_above_held(spot, r["strike"], sigma_per_min, held)
        nearest = min(abs(x["strike"] - spot) for x in ladder)
        snap["series"][series] = dict(spot=spot, sigma_per_min=sigma_per_min,
                                      secs_held=held, ladder=ladder,
                                      nearest_c=nearest / 100.0)
    snap["status"] = "scored" if snap["series"] else "no_ladder_at_close"
    return snap, held, errs


def summarize(rows, snap, errs):
    # The scored fields live at the TOP level of each row (rows_from splats
    # score_row's output). Reading them from r["edge"] reported actionable=0
    # best_c=None forever, because no row has that key.
    return dict(ts=snap["ts"], iso=iso_ms(snap["ts"]), status=snap.get("status"),
                close_iso=iso_ms(snap["close_ms"]) if snap.get("close_ms") else None,
                secs_held=snap["secs_held"],
                rungs=len(rows), fetch_errors=errs,
                actionable=sum(1 for r in rows if r.get("actionable")),
                best_c=max([r["best_c"] for r in rows if r.get("best_c") is not None] or [None]))


def run_once(client, out_dir=None):
    snap, held, errs = scan(client)
    rows = rows_from(snap)
    summ = summarize(rows, snap, errs)
    path = append_rows(rows, summ, out_dir)
    print(json.dumps(summ))
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--replay", default=None, help="ISO close time to replay from archived prints")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--minutes", type=float, default=0.0)
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.selfcheck:
        _selfcheck()
        return 0
    client = _kalshi_client()
    if client is None:
        print("no client")
        return 2
    if a.replay:
        cm = parse_close_ms(a.replay, 0)
        if not cm:
            print("bad --replay timestamp")
            return 2
        return replay(client, cm, fetch_ladder(client, SERIES[0], cm))
    if a.once:
        run_once(client, a.out)
        return 0
    end = time.time() + a.minutes * 60.0
    while time.time() < end:
        run_once(client, a.out)
        time.sleep(max(1.0, a.interval))
    return 0
def _selfcheck():
    # one: the fee is quadratic in the price PAID and its one cent is rounded
    # UP exactly once: 5c -> 0.0335c -> 1c, not 0.1c and not 7c
    assert taker_fee(0.05) == 0.01, taker_fee(0.05)
    assert taker_fee(0.01) == 0.01, taker_fee(0.01)
    assert taker_fee(0.50) == 0.02, taker_fee(0.50)
    assert taker_fee(0.95) == 0.01, taker_fee(0.95)
    # two: half-open settlement window: a stamp AT close belongs to the next
    assert settlement_window([dict(time=60000, value=1.0)], 60000) == []
    assert settlement_window([dict(time=1000, value=1.0)], 60000) != []
    assert abs(brti_avg([dict(time=0, value=10.0), dict(time=1000, value=20.0)]) - 15.0) < 1e-9
    # three: a coin flip at 50c can never be actionable and a 99/1 call can
    assert score_row(0.5, 0.5, 0.5)["actionable"] is False
    assert score_row(0.99, 0.05, 0.95)["actionable"] is True
    assert score_row(0.99, 0.05, 0.95)["ev_yes_c"] > 0.0
    # four: held probability is monotone in strike and the boundary is right
    assert p_above_held(75000.0, 74500.0, 0.0015, 0) > 0.5
    assert p_above_held(75000.0, 75500.0, 0.0015, 0) < 0.5
    assert p_above_held(75000.0, 74000.0, 0.0009, 60) == 1.0
    assert p_above_held(75000.0, 76000.0, 0.0009, 60) == 0.0
    # five: the next close is the next whole hour, never the current one
    assert nearest_close_ms(3600000) == 7200000
    assert nearest_close_ms(3599999) == 3600000
    # six: with no prints held, a strike far away is a near-certainty both ways
    assert p_above_held(75000.0, 50000.0, 0.009, 0) > 0.999
    assert p_above_held(75000.0, 99000.0, 0.009, 0) < 0.001
    print("selfcheck OK")



def replay(client, close_ms, live_ladder, sigma_per_min=0.0009 ):
    # Dry run of one settlement minute from archived prints.
    # live_ladder must have been captured while that minute was running; the
    # caller is responsible for saying so. Prints come from the archive.
    prints = fetch_history_hour(client, parse_hour_ms(close_ms - 60000))
    if not prints:
        print("replay: no prints for %s" % iso_ms(close_ms))
        return 1
    win = settlement_window(prints, close_ms)
    if len(win) < 55:
        print("replay: window has %d prints (5 Hz expects near 300)" % len(win))
    if not live_ladder:
        print("replay: empty ladder")
        return 1

    rungs = sorted(live_ladder, key=lambda r: r["strike"])
    strikes = [r["strike"] for r in rungs]
    ask_by = {}
    for r in rungs:
        ask_by.setdefault(r["strike"], r.get("yes_ask"))

    print("replay close %s  prints %d  rungs %d  strikes %d"
          % (iso_ms(close_ms), len(win) , len(rungs), len(strikes)))

    for secs in (0, 5, 15, 30, 45, 50, 55, 59):
        vals = [float(p["value"]) for p in win
                if int(p["time"]) - (close_ms - 60000) < secs * 1000]
        if not vals:
            continue
        spot = sum(vals) / len(vals)
        nearest = min(strikes, key=lambda k: abs(k - spot))
        p = p_above_held(spot, nearest, sigma_per_min, secs)
        ask = ask_by.get(nearest)
        gap = None if ask is None else 100.0 * (ask - p)
        print("  k=%02d spot=%9.2f prints=%3d near_K=%8.2f p=%.4f gap=%s"
              % (secs, spot, len(vals), nearest, p,
                 "n/a" if gap is None else "%+.1fc" % gap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
