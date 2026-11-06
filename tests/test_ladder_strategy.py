"""Tests for the ladder order planner and the ET->UTC conversion."""

from datetime import datetime, timezone

import pytest

import crossvenue as cv
import ladder_strategy as ls


def _row(strike, kalshi, fair, minutes=30.0):
    return {"strike": strike, "kalshi": kalshi, "fair": fair,
            "minutes_left": minutes, "poly": kalshi,
            "kalshi_edge_c": (fair - kalshi) * 100,
            "poly_edge_c": (fair - kalshi) * 100}


# --- taker fee is the adverse-selection floor -------------------------------

def test_taker_fee_peaks_at_fifty_cents():
    assert ls.taker_fee_cents(50) == pytest.approx(1.75)
    assert ls.taker_fee_cents(50) > ls.taker_fee_cents(20)
    assert ls.taker_fee_cents(50) > ls.taker_fee_cents(80)


def test_taker_fee_vanishes_at_the_boundaries():
    assert ls.taker_fee_cents(0) == 0.0
    assert ls.taker_fee_cents(100) == 0.0


def test_required_edge_scales_with_volatility():
    calm = ls.required_edge_cents(50, vol_mult=1.0)
    hot = ls.required_edge_cents(50, vol_mult=3.0)
    assert hot == pytest.approx(calm * 3.0)


def test_vol_multiple_is_clamped():
    assert ls.vol_multiple(0.001, 0.001) == pytest.approx(1.0)
    assert ls.vol_multiple(0.010, 0.001) == pytest.approx(ls.MAX_VOL_MULTIPLE)
    assert ls.vol_multiple(0.0001, 0.001) == pytest.approx(1.0)  # never below 1


# --- the entry rule ---------------------------------------------------------

def test_edge_below_the_fee_bar_is_not_tradeable():
    # 1c of edge at 50c cannot clear a 1.75c fee barrier.
    plans = ls.plan_orders([_row(100, 0.50, 0.51)])
    assert plans[0].tradeable is False
    assert "adverse" in plans[0].reason


def test_edge_above_the_fee_bar_is_tradeable():
    plans = ls.plan_orders([_row(100, 0.50, 0.56)])
    assert plans[0].tradeable is True
    assert plans[0].side == "YES"
    assert plans[0].net_edge_c == pytest.approx(6.0 - 1.75)


def test_overpriced_rung_flips_to_the_no_side():
    # fair 44c against a 50c price means buy NO, and the edge is symmetric.
    plans = ls.plan_orders([_row(100, 0.50, 0.44)])
    assert plans[0].side == "NO"
    assert plans[0].gross_edge_c == pytest.approx(6.0)


def test_time_gate_blocks_the_settlement_race():
    plans = ls.plan_orders([_row(100, 0.50, 0.60, minutes=1.0)])
    assert plans[0].tradeable is False
    assert "hold window" in plans[0].reason


def test_time_gate_blocks_distant_expiries():
    plans = ls.plan_orders([_row(100, 0.50, 0.60, minutes=600)])
    assert plans[0].tradeable is False


def test_boundary_prices_are_never_tradeable():
    plans = ls.plan_orders([_row(100, 0.0, 0.5), _row(110, 1.0, 0.5)])
    assert all(p.tradeable is False for p in plans)


# --- layering ---------------------------------------------------------------

def test_layering_takes_the_best_and_caps_the_count():
    rows = [_row(100, 0.50, 0.60), _row(110, 0.50, 0.58), _row(120, 0.50, 0.56),
            _row(130, 0.50, 0.54), _row(140, 0.50, 0.51)]
    picked = ls.layered(ls.plan_orders(rows), layers=3)
    assert len(picked) == 3
    assert picked[0].net_edge_c >= picked[-1].net_edge_c
    assert [p.strike for p in picked] == [100, 110, 120]


def test_layering_ignores_untradeable_rungs():
    rows = [_row(100, 0.50, 0.505), _row(110, 0.50, 0.70)]
    picked = ls.layered(ls.plan_orders(rows), layers=3)
    assert [p.strike for p in picked] == [110]


def test_summarise_reports_no_tradeables_cleanly():
    s = ls.summarise(ls.plan_orders([_row(100, 0.50, 0.505)]))
    assert s == {"rungs": 1, "tradeable": 0, "best_net_edge_c": 0.0,
                 "median_required_c": pytest.approx(1.75)}


# --- implied spot -----------------------------------------------------------

def test_implied_spot_recovers_the_reference_the_ladder_was_built_from():
    from binance_feed import prob_above

    true_spot, sigma, minutes = 50000.0, 0.0008, 60.0
    rows = [{"strike": k, "kalshi": prob_above(true_spot, k, sigma, minutes)}
            for k in range(49000, 51001, 100)]
    got = ls.implied_spot(rows, sigma, minutes)
    assert got == pytest.approx(true_spot, abs=60)


def test_implied_spot_handles_degenerate_input():
    assert ls.implied_spot([], 0.001, 30) is None
    assert ls.implied_spot([{"strike": 100, "kalshi": 0.5}], 0.001, 30) is None


# --- ET -> UTC --------------------------------------------------------------

def test_et_offset_handles_dst_boundaries():
    # Nov 5 2026 is after the first Sunday in November -> EST (-5).
    assert cv._et_offset_hours(datetime(2026, 11, 5, 21)) == -5
    # July is EDT (-4); a fixed -5 would put summer expiries an hour early.
    assert cv._et_offset_hours(datetime(2026, 7, 15, 21)) == -4
    # DST starts 2026-03-08 02:00 and ends 2026-11-01 02:00.
    assert cv._et_offset_hours(datetime(2026, 3, 8, 1)) == -5
    assert cv._et_offset_hours(datetime(2026, 3, 8, 3)) == -4
    assert cv._et_offset_hours(datetime(2026, 11, 1, 3)) == -5


def test_to_utc_matches_the_known_kalshi_expiry():
    # KXBTCD-26NOV0521 expires 21:00 ET and Kalshi reports 2026-11-06T02:00Z.
    assert cv.to_utc(datetime(2026, 11, 5, 21)) == datetime(2026, 11, 6, 2, tzinfo=timezone.utc)
