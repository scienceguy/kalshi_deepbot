"""verify_review_marked: new money-path conditions must carry a review reason."""

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
