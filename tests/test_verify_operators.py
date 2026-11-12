"""verify_operators: operator and `not` drift that every name-based gate misses."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import verify_operators as vo  # noqa: E402


def _diff(old, new):
    return (
        "--- a/t.py\n"
        "+++ b/t.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-%s\n"
        "+%s\n" % (old, new)
    )


def test_eq_to_ne_is_flagged():
    assert vo.findings(_diff("if x == THRESHOLD:", "if x != THRESHOLD:"))


def test_lt_to_le_is_flagged():
    assert vo.findings(_diff("if a < b:", "if a <= b:"))


def test_dropped_not_is_flagged():
    assert vo.findings(_diff("if not flag:", "if flag:"))


def test_doubled_not_is_flagged():
    assert vo.findings(_diff("if not x:", "if not not x:"))


def test_and_to_or_is_flagged():
    assert vo.findings(_diff("if a and b:", "if a or b:"))


def test_arith_operator_is_flagged():
    assert vo.findings(_diff("x = a + b", "x = a - b"))


def test_identifying_rename_the_same_operator_is_not_flagged():
    """A real rename changes no operator; the gate must not cry wolf on it."""
    assert not vo.findings(_diff("if edge_cents >= fee:", "if fee_edge >= fee:"))


def test_literal_change_is_not_flagged():
    assert not vo.findings(_diff("if x == 5:", "if x == 6:"))


def test_identical_lines_are_not_flagged():
    assert not vo.findings(_diff("if x == THRESHOLD:", "if x == THRESHOLD:"))
