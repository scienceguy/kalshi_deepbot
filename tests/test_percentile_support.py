"""Tests for latency.percentile_with_support and latency.report_percentile."""

import latency
from latency import percentile_with_support, report_percentile


def test_percentile_with_support_nearest_rank():
    """Nearest-rank: index = ceil(p / 100 * n), 1-based."""
    samples = [float(i) for i in range(1, 101)]  # 1..100
    value, support = percentile_with_support(samples, 50)
    assert value == 50.0
    assert support == 50

    value, support = percentile_with_support(samples, 99)
    assert value == 99.0
    assert support == 99

    value, support = percentile_with_support(samples, 100)
    assert value == 100.0
    assert support == 100


def test_percentile_with_support_p0_is_minimum():
    value, support = percentile_with_support([3.0, 1.0, 2.0], 0)
    assert value == 1.0
    assert support == 1


def test_percentile_with_support_empty():
    assert percentile_with_support([], 50) == (None, 0)


def test_percentile_with_support_single():
    value, support = percentile_with_support([42.0], 99)
    assert value == 42.0
    assert support == 1


def test_support_count_counts_samples_at_or_below():
    """Support = number of samples at or below the percentile value."""
    samples = [1.0, 2.0, 2.0, 2.0, 3.0]
    # p50 -> rank = ceil(0.5 * 5) = 3 -> s[2] = 2.0
    # samples <= 2.0: 1.0, 2.0, 2.0, 2.0 = four of them
    value, support = percentile_with_support(samples, 50)
    assert value == 2.0
    assert support == 4


def test_percentile_with_support_clamps_p_out_of_range():
    samples = [1.0, 2.0, 3.0]
    value, _ = percentile_with_support(samples, -10)
    assert value == 1.0
    value, _ = percentile_with_support(samples, 200)
    assert value == 3.0


def test_report_percentile_grade_normal():
    """>= 10 support -> normal."""
    hist = {float(i): 1 for i in range(1, 21)}  # 20 samples
    r = report_percentile(hist, 50)
    assert r["grade"] == "normal"
    assert r["support"] >= 10
    assert r["value"] is not None


def test_report_percentile_grade_provisional():
    """3-9 support -> provisional (small samples marked provisional)."""
    hist = {float(i): 1 for i in range(1, 6)}  # 5 samples
    r = report_percentile(hist, 50)
    assert r["grade"] == "provisional"
    assert 3 <= r["support"] <= 9


def test_report_percentile_grade_insufficient():
    """1-2 support -> insufficient."""
    hist = {1.0: 2}
    r = report_percentile(hist, 50)
    assert r["grade"] == "insufficient"
    assert r["support"] == 2


def test_report_percentile_empty_histogram():
    r = report_percentile({}, 50)
    assert r["value"] is None
    assert r["support"] == 0
    assert r["grade"] == "no_data"
