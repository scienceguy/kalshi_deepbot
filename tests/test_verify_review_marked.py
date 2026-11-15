"""verify_review_marked: B10 — money-path diffs must ship a referencing test.

Markers are advisory notes; the test-ship rule is the gate: a diff touching
a money-path file (MONEY_PATH) must also add or modify a tests/*.py file,
and when it adds a def in the money path, at least one added test line must
reference it by name.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import verify_review_marked as vr  # noqa: E402


def _ins(marker=""):
    return (
        "--- a/trader.py\n"
        "+++ b/trader.py\n"
        "@@ -10,2 +10,4 @@\n"
        " def cycle(client, balance):\n"
        "     balance = float(balance) / 100.0\n"
        "+    if balance < CASH_FLOOR:%s\n"
        "+        return\n"
        "     positions = client.get_positions()\n" % marker
    )


def test_unreviewed_condition_is_a_finding():
    hits = vr.unreviewed(_ins())
    assert len(hits) == 1
    assert hits[0][0] == "trader.py"
    assert hits[0][2].strip().startswith("if balance < CASH_FLOOR:")


def test_condition_with_marker_is_clean():
    assert vr.unreviewed(_ins("  # reviewed: halt entries below cash floor")) == []


def test_short_marker_reason_does_not_count():
    assert len(vr.unreviewed(_ins("  # reviewed: ok"))) == 1


def test_marker_on_line_above_is_clean():
    diff = (
        "--- a/risk.py\n"
        "+++ b/risk.py\n"
        "@@ -1,1 +1,2 @@\n"
        " total = self.mark_to_market()\n"
        "+        # reviewed: negative exposure means flat\n"
        "+        if total <= 0:\n"
        "+            return False\n"
    )
    assert vr.unreviewed(diff) == []


def test_marker_does_not_leak_across_files():
    diff = (
        "--- a/risk.py\n"
        "+++ b/risk.py\n"
        "@@ -1,1 +1,2 @@\n"
        "+        # reviewed: negative exposure means flat\n"
        " x = 1\n"
        "--- a/trader.py\n"
        "+++ b/trader.py\n"
        "@@ -1,1 +1,2 @@\n"
        "+    if balance < CASH_FLOOR:\n"
        " y = 2\n"
    )
    hits = vr.unreviewed(diff)
    assert len(hits) == 1
    assert hits[0][0] == "trader.py"


def test_condition_in_non_money_file_is_clean():
    diff = (
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,1 +1,2 @@\n"
        "+if balance < CASH_FLOOR:\n"
        " text\n"
    )
    assert vr.unreviewed(diff) == []


def test_removed_condition_is_not_a_finding():
    diff = (
        "--- a/trader.py\n"
        "+++ b/trader.py\n"
        "@@ -1,2 +1,1 @@\n"
        "-    if balance < CASH_FLOOR:\n"
        "-        return\n"
        " def cycle():\n"
    )
    assert vr.unreviewed(diff) == []


def test_hunk_offset_is_applied_to_reported_lineno():
    diff = (
        "--- a/risk.py\n"
        "+++ b/risk.py\n"
        "@@ -240,3 +240,4 @@\n"
        "     def check(self):\n"
        "         total = self.mark_to_market()\n"
        "+        if total <= 0:\n"
        "+            return False\n"
        "         return total < self.ceiling\n"
    )
    hits = vr.unreviewed(diff)
    assert len(hits) == 1
    assert hits[0][1] == 242


# ── B10: money-path diffs must ship a test referencing the change ────────

def _risk_diff(with_test=False, new_def=False):
    guard = "+    def new_guard(self):\n" if new_def else ""
    diff = (
        "--- a/risk.py\n"
        "+++ b/risk.py\n"
        "@@ -1,2 +1,4 @@\n"
        " total = self.mark_to_market()\n"
        + guard +
        "+        if total <= 0:\n"
        "+            return False\n"
        "         return total < self.ceiling\n"
    )
    if with_test:
        diff += (
            "--- a/tests/test_risk.py\n"
            "+++ b/tests/test_risk.py\n"
            "@@ -1,1 +1,2 @@\n"
            "+def test_ceiling():\n"
            "+    assert True\n"
        )
    return diff


def test_money_path_diff_without_test_fails():
    """Acceptance: a risk.py diff with no tests/ change fails the gate."""
    assert vr.test_coverage_ok(_risk_diff()) is False


def test_money_path_diff_with_added_test_passes():
    """Acceptance: the same diff plus a test passes."""
    assert vr.test_coverage_ok(_risk_diff(with_test=True)) is True


def test_added_test_must_reference_changed_function():
    """When the diff adds a def, the test must reference it by name."""
    assert vr.test_coverage_ok(_risk_diff(with_test=True, new_def=True)) is False
    referencing = _risk_diff(with_test=True, new_def=True).replace(
        "+def test_ceiling():", "+def test_new_guard():")
    assert vr.test_coverage_ok(referencing) is True


def test_non_money_path_diff_needs_no_test():
    diff = (
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,1 +1,2 @@\n"
        "+some doc line\n"
        " text\n"
    )
    assert vr.test_coverage_ok(diff) is True


def test_main_exit_codes_follow_the_test_rule(tmp_path):
    """main() exits 1 without a test, 0 with one (acceptance, end to end)."""
    no_test = tmp_path / "no_test.diff"
    no_test.write_text(_risk_diff())
    rc = vr.main(["--diff-file", str(no_test)])
    assert rc == 1
    with_test = tmp_path / "with_test.diff"
    with_test.write_text(_risk_diff(with_test=True))
    rc = vr.main(["--diff-file", str(with_test)])
    assert rc == 0
