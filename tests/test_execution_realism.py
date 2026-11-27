"""Arithmetic fixtures only; strategy evidence requires real timestamped books."""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import execution_realism
from execution_realism import (
    Book, Level, SizingCaps, bounded_fixed_fraction, clustered_lower_bound,
    depth_aware_size, evaluate_pair, summarize_validation, validate_candidate,
    walk_book,
)
import fees


def _book(venue="kalshi", ts=100.0, levels=None):
    return Book(venue=venue, side="yes", ts=ts,
                levels=[Level(*level) for level in levels or []])


def test_full_depth_walk_uses_real_levels_and_production_fees():
    book = _book(levels=[(0.40, 2.0), (0.41, 3.0)])
    fill = walk_book(book, 4.0, action="buy")
    assert fill.quantity == 4.0
    assert fill.levels_used == 2
    expected = (2 * 0.40 + fees.taker_fee_dollars(2, 0.40)
                + 2 * 0.41 + fees.taker_fee_dollars(2, 0.41))
    assert fill.total_cost == pytest.approx(expected)


def test_no_taker_buy_walks_lowest_asks_before_higher_levels():
    book = Book(venue="kalshi", side="no", ts=100.0,
                levels=[Level(0.40, 2.0), Level(0.30, 2.0)])

    fill = walk_book(book, 3.0, action="buy")

    expected = (2 * 0.30 + fees.taker_fee_dollars(2, 0.30)
                + 0.40 + fees.taker_fee_dollars(1, 0.40))
    assert fill.levels_used == 2
    assert fill.total_cost == pytest.approx(expected)


def test_yes_sell_walks_highest_bids_and_reports_net_proceeds():
    book = Book(venue="kalshi", side="yes", ts=100.0,
                levels=[Level(0.40, 2.0), Level(0.41, 2.0)])

    fill = walk_book(book, 3.0, action="sell")

    gross = 2 * 0.41 + 0.40
    expected_fee = (fees.taker_fee_dollars(2, 0.41)
                    + fees.taker_fee_dollars(1, 0.40))
    assert fill.levels_used == 2
    assert fill.proceeds == pytest.approx(gross - expected_fee)


def test_no_sell_walks_highest_bids_and_reports_net_proceeds():
    book = Book(venue="kalshi", side="no", ts=100.0,
                levels=[Level(0.30, 2.0), Level(0.40, 2.0)])

    fill = walk_book(book, 3.0, action="sell")

    gross = 2 * 0.40 + 0.30
    expected_fee = (fees.taker_fee_dollars(2, 0.40)
                    + fees.taker_fee_dollars(1, 0.30))
    assert fill.levels_used == 2
    assert fill.proceeds == pytest.approx(gross - expected_fee)


def test_polymarket_multi_fill_fee_adjustment_only_reduces_total():
    book = _book(venue="polymarket_us", levels=[(0.50, 1.0)] * 3)
    fill = walk_book(book, 3.0)
    # Each 1-lot rounds $0.017375 up to $0.02 ($0.06 total), but the
    # cumulative exact $0.052125 rounds to $0.05, so the adjustment is -$0.01.
    assert fill.fee == 0.05
    assert fill.total_cost == 1.55


def test_insufficient_depth_and_missing_leg_never_fill():
    assert walk_book(_book(levels=[(0.40, 1.0)]), 2.0) is None
    other = _book(venue="polymarket_us", levels=[(0.50, 10)])
    assert evaluate_pair(_book(levels=[]), other, 2.0) is None


def test_optimal_scale_depth_sensitivity_and_both_legs_required():
    left = _book(levels=[(0.40, 5.0), (0.41, 5.0)])
    right = _book(venue="polymarket_us", levels=[(0.50, 5.0), (0.51, 5.0)])
    result = evaluate_pair(left, right, 10.0, now=100.0)
    assert result is not None
    assert result.quantity > 0
    assert result.pnl > 0
    half = evaluate_pair(left, right, 10.0, depth_fraction=0.5, now=100.0)
    assert half is None or half.quantity <= result.quantity


def test_one_second_delay_uses_only_subsequent_real_books():
    left = _book(levels=[(0.40, 5.0)])
    right = _book(venue="polymarket_us", levels=[(0.50, 5.0)])
    old = _book(ts=100.5, levels=[(0.41, 5.0)])
    later_right = _book(venue="polymarket_us", ts=101.0, levels=[(0.50, 5.0)])
    assert validate_candidate(left, right, 2.0, old, later_right, now=100.0).delay is None
    later_left = _book(ts=101.0, levels=[(0.41, 5.0)])
    stressed = validate_candidate(left, right, 2.0, later_left, later_right, now=100.0)
    assert stressed.delay is not None
    assert (stressed.delay.fill.left.total_cost + stressed.delay.fill.right.total_cost
            > stressed.base.fill.left.total_cost + stressed.base.fill.right.total_cost)


def test_stale_base_book_is_rejected():
    left = _book(ts=98.0, levels=[(0.40, 5.0)])
    right = _book(venue="polymarket_us", ts=100.0, levels=[(0.50, 5.0)])
    assert evaluate_pair(left, right, 2.0, now=100.0) is None


def test_mismatched_base_book_times_are_rejected_even_when_fresh():
    left = _book(ts=99.5, levels=[(0.40, 5.0)])
    right = _book(venue="polymarket_us", ts=100.0, levels=[(0.50, 5.0)])
    assert evaluate_pair(left, right, 2.0, now=100.0) is None


def test_unrelated_delayed_book_times_are_rejected():
    left = _book(levels=[(0.40, 5.0)])
    right = _book(venue="polymarket_us", levels=[(0.50, 5.0)])
    delayed_left = _book(ts=101.0, levels=[(0.41, 5.0)])
    unrelated_right = _book(
        venue="polymarket_us", ts=105.0, levels=[(0.50, 5.0)])
    result = validate_candidate(
        left, right, 2.0, delayed_left, unrelated_right, now=100.0)
    assert result.delay is None


def test_market_clustered_lower_bound_requires_30_markets():
    rows = [{"market": str(i), "pnl": 1.0} for i in range(29)]
    assert clustered_lower_bound(rows) is None
    rows += [{"market": "29", "pnl": 1.0}]
    assert clustered_lower_bound(rows, samples=100, seed=7) == 1.0


def _protocol_rows(count=300, days=14):
    start = date(2026, 1, 1)
    return [{
        "market": str(i),
        "settled_day": (start + timedelta(days=i % days)).isoformat(),
        "quantity": 1.0,
        "pnl": 0.01,
        "side": "no",
        "real_fill": True,
        "held_to_settlement": True,
    } for i in range(count)]


def test_protocol_bootstrap_exact_seed_and_market_weighted_reproducibility():
    rows = _protocol_rows()
    for i, row in enumerate(rows):
        row["quantity"] = float(1 + i % 3)
        row["pnl"] = (i % 5 - 2) * 0.013 * row["quantity"]
    first = execution_realism.protocol_clustered_lower_bound(rows, exact_m=200)
    second = execution_realism.protocol_clustered_lower_bound(rows, exact_m=200)
    assert first == second
    assert first == pytest.approx(-0.004006700167504188)


def test_protocol_bootstrap_requires_300_eligible_markets():
    assert execution_realism.protocol_clustered_lower_bound(
        _protocol_rows(299), exact_m=100) is None
    assert execution_realism.protocol_clustered_lower_bound(
        _protocol_rows(300), exact_m=100) == pytest.approx(0.01)


def test_protocol_bootstrap_requires_14_utc_settlement_days():
    assert execution_realism.protocol_clustered_lower_bound(
        _protocol_rows(300, days=13), exact_m=100) is None
    assert execution_realism.protocol_clustered_lower_bound(
        _protocol_rows(300, days=14), exact_m=100) == pytest.approx(0.01)


@pytest.mark.parametrize("field,value", [
    ("real_fill", False),
    ("held_to_settlement", False),
    ("side", "yes"),
    ("quantity", 0.0),
    ("quantity", float("nan")),
])
def test_protocol_bootstrap_rejects_missing_real_fill_eligibility(field, value):
    rows = _protocol_rows()
    rows[0][field] = value
    assert execution_realism.protocol_clustered_lower_bound(rows, exact_m=100) is None


def test_protocol_bootstrap_accepts_explicitly_simulated_dry_fills():
    rows = _protocol_rows()
    for row in rows:
        row["real_fill"] = False
        row["fill_provenance"] = "book_simulated"
    assert execution_realism.protocol_clustered_lower_bound(rows, exact_m=100) == pytest.approx(0.01)


def test_protocol_bootstrap_rejects_book_simulated_masquerading_as_real_fill():
    rows = _protocol_rows()
    for row in rows:
        row["fill_provenance"] = "book_simulated"
    assert execution_realism.protocol_clustered_lower_bound(rows, exact_m=100) is None


def test_protocol_bootstrap_requires_registered_positive_exact_m():
    assert execution_realism.protocol_clustered_lower_bound(
        _protocol_rows(), exact_m=0) is None


def test_fixed_fractional_is_bounded_and_not_martingale():
    caps = SizingCaps(available_cash=40.0, max_exposure=50.0,
                      current_exposure=20.0, current_drawdown=0.05,
                      max_drawdown=0.30)
    assert bounded_fixed_fraction(100.0, 0.50, caps) == 25.0
    assert bounded_fixed_fraction(80.0, 0.50, caps) == 20.0
    assert bounded_fixed_fraction(100.0, 0.50, caps) > 0


def test_depth_aware_size_requires_positive_edge_after_all_fees():
    left = _book(levels=[(0.40, 5.0)])
    right = _book(venue="polymarket_us", levels=[(0.50, 5.0)])
    caps = SizingCaps(available_cash=100.0, max_exposure=100.0,
                      current_exposure=0.0, current_drawdown=0.0,
                      max_drawdown=0.50)
    result = depth_aware_size(left, right, 100.0, 0.50, caps, now=100.0)
    assert result is not None
    assert result.fill.pnl > 0
    assert result.turnover <= 0.50
    losing = _book(venue="polymarket_us", levels=[(0.70, 5.0)])
    assert depth_aware_size(left, losing, 100.0, 0.50, caps, now=100.0) is None


def test_validation_summary_reports_tail_turnover_and_drawdown():
    rows = ([{"market": str(i), "pnl": -1.0 if i == 0 else 0.1,
              "cost": 2.0, "tail_exposure": 2.0} for i in range(30)])
    report = summarize_validation(rows, equity=100.0, samples=100, seed=7)
    assert report["clustered_lb"] is not None
    assert report["turnover"] == pytest.approx(0.60)
    assert report["tail_exposure"] == 2.0
    assert report["max_drawdown"] == pytest.approx(0.01)


def test_evaluate_pair_never_exceeds_requested_quantity():
    left = _book(levels=[(0.40, 5.0)])
    right = _book(venue="polymarket_us", levels=[(0.50, 5.0)])
    result = evaluate_pair(left, right, 1.0, now=100.0)
    assert result is not None
    assert result.quantity <= 1.0


def test_depth_aware_size_rejects_actual_drawdown_breach():
    left = _book(levels=[(0.40, 5.0)])
    right = _book(venue="polymarket_us", levels=[(0.50, 5.0)])
    caps = SizingCaps(available_cash=100.0, max_exposure=100.0,
                      current_exposure=0.0, current_drawdown=0.29,
                      max_drawdown=0.30)
    assert depth_aware_size(left, right, 100.0, 0.5, caps, now=100.0) is None


def test_first_real_kalshi_depth_walk():
    path = Path("/home/ubuntu/.kalshi-bot/depth_probe.jsonl")
    if not path.exists():
        pytest.skip("real KalshiEye depth probe unavailable")
    row = json.loads(path.read_text().splitlines()[0])
    book = _book(ts=float(row["t"]), levels=row["levels"]["no"])
    fill = walk_book(book, 100.0)
    assert fill is not None
    assert fill.levels_used >= 1
    assert fill.total_cost > 100 * 0.09  # sanity only, not strategy evidence
