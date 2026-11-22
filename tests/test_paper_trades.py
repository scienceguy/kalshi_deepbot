"""Tests for the dry-run paper-trade lane (scripts/paper_trades.py).

Money-path code: fill prices, fee usage, cash accounting, settlement and the
drawdown halt are all asserted against fees.py's own venue model.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import paper_trades as pt  # noqa: E402

import fees  # noqa: E402


def cfg(**over):
    base = dict(mode="taker", venue="kalshi", balance=100.0, pct=0.08,
                band_lo=85.0, band_hi=99.0, min_m=3, max_m=5, gate=3.0,
                maker_offset=0.01, dd_halt=0.25)
    base.update(over)
    return argparse.Namespace(**base)


def row(event, ts, bid, ask, fair, strike, minutes_left):
    return {"ts": ts, "asset": "BTC", "event": event, "slug": "",
            "minutes_left": minutes_left, "strike": strike, "fair": fair,
            "kalshi": round((bid + ask) / 2.0, 4), "kalshi_bid": bid,
            "kalshi_ask": ask, "poly": 0.5, "poly_bid": 0.49, "poly_ask": 0.51}


T0 = datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc)
EV = "KXBTC15M-26NOV220000"


def window(ts=T0, bid=0.89, ask=0.90, fair=0.94, minutes_left=11.0, event=EV):
    # minute = round(15 - 11.0) = 4 -> inside [3, 5]; ask 90 in [85, 99];
    # fair - ask = 4 >= gate 3.0
    return row(event, ts.isoformat(), bid, ask, fair, 60000.0, minutes_left)


def test_taker_win_pays_out_minus_cash():
    out = pt.simulate([window()], cfg(), lambda ev: {"result": "yes"})
    t = out["trades"][0]
    contracts = int(0.08 * 100.0 / 0.90)
    assert t["contracts"] == contracts == 8
    assert t["price"] == 0.90
    assert t["settled"] and t["result"] == "yes"
    assert t["cash"] == fees.buy_cash_out(8, 0.90, taker=True)
    assert t["realized"] == round(8 * 1.0 - t["cash"], 4)
    assert out["summary"]["balance"] == round(100.0 + t["realized"], 4)
    assert out["summary"]["unrealized_pnl"] == 0.0


def test_taker_loss_loses_the_cash():
    out = pt.simulate([window()], cfg(), lambda ev: {"result": "no"})
    t = out["trades"][0]
    assert t["realized"] == round(-t["cash"], 4)
    assert out["summary"]["wins"] == 0
    assert out["summary"]["win_rate"] == 0.0


def test_unsettled_window_is_unrealized_only():
    out = pt.simulate([window()], cfg(), lambda ev: {"result": None})
    t = out["trades"][0]
    assert not t["settled"]
    assert t["realized"] == 0.0
    assert out["summary"]["balance"] == 100.0
    assert t["unrealized"] == round(8 * 0.895 - t["cash"], 4)
    assert out["summary"]["unrealized_pnl"] == t["unrealized"]


def test_maker_fills_only_when_the_ask_comes_down():
    entry = window(ts=T0, ask=0.90, fair=0.94)
    later_hit = window(ts=T0 + timedelta(seconds=90), ask=0.925, fair=0.94)
    later_miss = window(ts=T0 + timedelta(seconds=90), ask=0.95, fair=0.94)
    hit = pt.simulate([entry, later_hit], cfg(mode="maker"),
                      lambda ev: {"result": "yes"})
    miss = pt.simulate([entry, later_miss], cfg(mode="maker"),
                       lambda ev: {"result": "yes"})
    assert hit["trades"][0]["price"] == 0.93       # fair - maker_offset
    assert hit["trades"][0]["cash"] == fees.buy_cash_out(8, 0.93, taker=False)
    assert miss["trades"] == []                     # no fill, no trade


def test_rule_filters_exclude_nonqualifying_rows():
    out = pt.simulate([
        window(bid=0.79, ask=0.80, fair=0.94),      # ask below the band
        window(bid=0.89, ask=0.90, fair=0.915),     # fair - ask < gate
        window(minutes_left=13.0),                  # minute 2, too early
        window(minutes_left=8.0),                   # minute 7, too late
        window(bid=0.70, ask=0.90, fair=0.94),      # spread 0.20 > MAX_SPREAD
    ], cfg(), lambda ev: {"result": "yes"})
    assert out["trades"] == [] and out["summary"]["entries"] == 0


def test_unaffordable_and_halted_entries_are_dropped():
    poor = pt.simulate([window()], cfg(balance=1.0), lambda ev: {"result": "yes"})
    assert poor["trades"] == [] and poor["summary"]["dropped_unaffordable_or_halted"] == 1

    lose = {"result": "no"}
    # four DISTINCT windows: the engine stops opening once the halt trips
    rows = [window(ts=T0 + timedelta(minutes=15 * i),
                   event=f"KXBTC15M-26NOV2200{i:02d}") for i in range(4)]
    halted = pt.simulate(rows, cfg(dd_halt=0.10), lambda ev: lose)
    assert halted["summary"]["halted"]
    assert len(halted["trades"]) < 4
    assert all(t["halted_after"] for t in halted["trades"][1:])


def test_minutes_elapsed_matches_the_sweeps_minute_column():
    assert pt.minutes_elapsed(14.9) == 0
    assert pt.minutes_elapsed(11.0) == 4
    assert pt.minutes_elapsed(9.9) == 5
    assert pt.minutes_elapsed(0.2) == 15