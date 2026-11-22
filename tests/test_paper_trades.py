"""Tests for the dry-run paper-trade lane (scripts/paper_trades.py).

Money-path code: fill prices, fee usage, cash accounting, settlement and the
drawdown halt are all asserted against fees.py's own venue model.
"""

import argparse
import json
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
            "sigma_min": 0.0004, "spot": strike,
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
    # the live path's own sizing rule: fractional contracts to the 0.01 lot,
    # not whole contracts -- 8% of $100 at a 90c ask is 8.88 contracts
    contracts = 8.88
    assert t["contracts"] == contracts
    assert t["price"] == 0.90
    assert t["settled"] and t["result"] == "yes"
    assert t["cash"] == fees.buy_cash_out(contracts, 0.90, taker=True)
    assert t["realized"] == round(contracts * 1.0 - t["cash"], 4)
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
    assert t["unrealized"] == round(8.88 * 0.895 - t["cash"], 4)
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
    # 8% of $100 at the 93c resting bid is 8.60 contracts (0.01 lot floor)
    assert hit["trades"][0]["cash"] == fees.buy_cash_out(8.60, 0.93, taker=False)
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
    # below the venue's 0.01 minimum lot: 8% of $0.10 at a 90c ask is 0.0089
    # contracts, which floor_contracts rounds to zero -- no trade, dropped
    poor = pt.simulate([window()], cfg(balance=0.10), lambda ev: {"result": "yes"})
    assert poor["trades"] == [] and poor["summary"]["dropped_unaffordable_or_halted"] == 1

    lose = {"result": "no"}
    # four DISTINCT windows: the engine stops opening once the halt trips
    rows = [window(ts=T0 + timedelta(minutes=15 * i),
                   event=f"KXBTC15M-26NOV2200{i:02d}") for i in range(4)]
    halted = pt.simulate(rows, cfg(dd_halt=0.10), lambda ev: lose)
    assert halted["summary"]["halted"]
    assert len(halted["trades"]) < 4
    assert all(t["halted_after"] for t in halted["trades"][1:])


def test_daily_ladder_events_are_never_booked():
    # a ladder row that passes every other filter must still be ignored: the
    # collector's daily "above" ladders are a different market family
    ladder = row("KXBTCD-26NOV2120", (T0 + timedelta(minutes=5)).isoformat(),
                 0.889, 0.90, 0.94, 59899.99, 11.0)
    out = pt.simulate([window(ts=T0 + timedelta(minutes=5)), ladder], cfg(),
                      lambda ev: {"result": "yes"})
    assert [t["event"] for t in out["trades"]] == [EV]


def test_sizing_mirrors_the_live_fractional_rule():
    # the first qualifying dry-run entry on the live balance: 8% of $10.34 at
    # an 88c ask is 0.94 contracts -- the whole-contract floor would have
    # dropped it, but the live path trades fractional lots to 0.01
    out = pt.simulate([window(ts=T0, bid=0.87, ask=0.88, fair=0.999,
                              minutes_left=10.45)],
                      cfg(balance=10.34), lambda ev: {"result": "yes"})
    t = out["trades"][0]
    assert t["contracts"] == 0.94
    assert t["cash"] == fees.buy_cash_out(0.94, 0.88, taker=True)
    assert t["realized"] == round(0.94 * 1.0 - t["cash"], 4)
    assert out["summary"]["dropped_unaffordable_or_halted"] == 0


def test_minutes_elapsed_matches_the_sweeps_minute_column():
    assert pt.minutes_elapsed(14.9) == 0
    assert pt.minutes_elapsed(11.0) == 4
    assert pt.minutes_elapsed(9.9) == 5
    assert pt.minutes_elapsed(0.2) == 15


def _index(tmp_path, points, asset="BTC"):
    """A BRTI tick file in fetch_brti_history.py's own output format."""
    p = tmp_path / "brti.jsonl"
    with open(p, "w") as fh:
        for offset_s, px in points:
            d = {"time": int((T0 + timedelta(seconds=offset_s)).timestamp() * 1000),
                 "value": px}
            if asset is not None:
                d = {"asset": asset, **d}
            fh.write(json.dumps(d) + "\n")
    return pt.load_index_ticks(str(p))


def test_index_priced_fair_governs_the_entry_not_the_feed(tmp_path):
    # the collector's feed fair (0.94) clears the 3c gate, but the settlement
    # index sits AT the strike -> index fair 0.4997 -> gap -40c: the feed's
    # measured ~22bps basis must not book a paper trade (2026-11-22 audit:
    # the feed printed an 11.9c gap where the index priced 7.5c)
    index = _index(tmp_path, [(0, 60000.0)])
    out = pt.simulate([window()], cfg(), lambda ev: {"result": "yes"}, index)
    assert out["trades"] == [] and out["summary"]["entries"] == 0
    # the artifact path stays available when no index is loaded
    feed = pt.simulate([window()], cfg(), lambda ev: {"result": "yes"})
    assert feed["trades"][0]["fair_source"] == "feed"
    assert feed["trades"][0]["fair"] == 0.94


def test_index_priced_entry_records_the_index_fair(tmp_path):
    from binance_feed import prob_above
    px = 60200.0
    index = _index(tmp_path, [(0, px)])
    out = pt.simulate([window()], cfg(), lambda ev: {"result": "yes"}, index)
    t = out["trades"][0]
    assert t["fair_source"] == "index"
    assert t["fair"] == round(prob_above(px, 60000.0, 0.0004, 11.0), 4)
    assert t["price"] == 0.90  # taker fills at the recorded ask either way


def test_rows_without_index_coverage_are_not_replayed(tmp_path):
    # a tick 10 minutes from the snapshot: no honest price at the instant,
    # so the row is not replayed at all rather than repriced from the feed
    index = _index(tmp_path, [(600, 60200.0)])
    out = pt.simulate([window()], cfg(), lambda ev: {"result": "yes"}, index)
    assert out["trades"] == [] and out["summary"]["entries"] == 0


def test_index_ticks_without_an_asset_column_still_price(tmp_path):
    # a single-asset BRTI dump (no "asset" column) falls in the default bucket
    index = _index(tmp_path, [(0, 60200.0)], asset=None)
    out = pt.simulate([window()], cfg(), lambda ev: {"result": "yes"}, index)
    assert out["trades"][0]["fair_source"] == "index"