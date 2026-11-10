"""Tests for the cross-venue comparison primitives (no network)."""

import math
from datetime import datetime

import pytest

import crossvenue as cv
from binance_feed import prob_above


def test_parse_kalshi_event_rejects_other_series():
    assert cv.parse_kalshi_event("KXBTCD-26NOV0521") == ("BTC", datetime(2026, 11, 5, 21))
    assert cv.parse_kalshi_event("KXETHD-26NOV0617") == ("ETH", datetime(2026, 11, 6, 17))
    # 15M series are now supported: HHMM time format
    assert cv.parse_kalshi_event("KXBTC15M-26NOV052245") == ("BTC", datetime(2026, 11, 5, 22, 45))
    assert cv.parse_kalshi_event("KXETH15M-26NOV052100") == ("ETH", datetime(2026, 11, 5, 21, 0))
    assert cv.parse_kalshi_event("") is None


def test_parse_pm_slug_hourly_and_daily():
    assert cv.parse_pm_slug("bitcoin-above-on-november-5-2026-1pm-et") == (
        "BTC", datetime(2026, 11, 5, 13, 0))
    assert cv.parse_pm_slug("ethereum-above-on-november-5-2026-12am-et") == (
        "ETH", datetime(2026, 11, 5, 0, 0))
    # no hour means the daily ladder, resolving at 23:59
    assert cv.parse_pm_slug("bitcoin-above-on-november-5-2026") == (
        "BTC", datetime(2026, 11, 5, 23, 59))
    assert cv.parse_pm_slug("who-wins-the-election") is None


def _ladder(venue, rungs, expiry=datetime(2026, 11, 5, 21)):
    return cv.Ladder(venue, "BTC", expiry,
                     [cv.Rung(s, m, bid=m - 0.005, ask=m + 0.005) for s, m in rungs])


def test_wide_spread_rungs_are_dropped():
    lad = cv.Ladder("polymarket", "BTC", datetime(2026, 11, 5, 21), [
        cv.Rung(100.0, 0.90, bid=0.80, ask=0.99),   # 0.19 spread -> unusable
        cv.Rung(110.0, 0.60, bid=0.59, ask=0.61),
        cv.Rung(120.0, 0.30, bid=0.29, ask=0.31),
        cv.Rung(130.0, 0.10, bid=0.09, ask=0.11),
    ])
    kept = lad.sorted_rungs()
    assert [r.strike for r in kept] == [110.0, 120.0, 130.0]
    assert lad.ok


def test_cdf_is_monotone_even_when_quotes_are_not():
    # 120 is quoted higher than 110: stale rung, must be flattened.
    lad = _ladder("kalshi", [(110.0, 0.50), (120.0, 0.70), (130.0, 0.20)])
    cdf = cv.to_cdf(lad)
    probs = [p for _, p in cdf]
    assert all(b <= a + 1e-12 for a, b in zip(probs, probs[1:]))
    assert cdf[0][1] == pytest.approx(0.50)
    assert cdf[-1][1] == pytest.approx(0.20)


def test_to_cdf_needs_three_rungs():
    assert cv.to_cdf(_ladder("kalshi", [(100.0, 0.5), (110.0, 0.4)])) is None


def test_compare_only_interpolates_inside_poly_range():
    k = _ladder("kalshi", [(100.0, 0.9), (110.0, 0.6), (120.0, 0.3), (200.0, 0.01)])
    p = _ladder("polymarket", [(100.0, 0.8), (110.0, 0.5), (120.0, 0.2)])
    rows = cv.compare(k, p)
    strikes = [r.strike for r in rows]
    assert 200.0 not in strikes          # would require extrapolating a CDF
    assert strikes == [100.0, 110.0, 120.0]
    mid = next(r for r in rows if r.strike == 110.0)
    assert mid.cents == pytest.approx((0.5 - 0.6) * 100)


def test_prob_above_at_the_money_is_near_half():
    assert prob_above(100.0, 100.0, 0.001, 30) == pytest.approx(0.5, abs=0.01)


def test_prob_above_deep_in_the_money_and_out():
    assert prob_above(100.0, 90.0, 0.001, 30) > 0.99
    assert prob_above(100.0, 110.0, 0.001, 30) < 0.01


def test_prob_above_returns_nan_on_bad_input():
    assert math.isnan(prob_above(0.0, 100.0, 0.001, 30))
    assert math.isnan(prob_above(100.0, 100.0, 0.0, 30))
    assert math.isnan(prob_above(100.0, 100.0, 0.001, 0))


def test_median_strike_interpolates_the_crossing():
    rows = [{"strike": 100.0, "kalshi": 0.8}, {"strike": 110.0, "kalshi": 0.2}]
    # 0.8 -> 0.2 crosses 0.5 exactly halfway
    assert cv._median_strike(rows, "kalshi") == pytest.approx(105.0)
