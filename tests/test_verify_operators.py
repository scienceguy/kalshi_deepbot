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


def test_dropped_and_conjunct_is_flagged():
    """x > 0 and limit != 0  ->  x > 0  silently drops a guard."""
    assert vo.findings(_diff("if x > 0 and limit != 0:", "if x > 0:"))


def test_dropped_or_conjunct_is_flagged():
    """Operator counts are identical here (Or -> Or); only the name disappears."""
    found = vo.findings(_diff("if a or b or cancel:", "if a or b:"))
    assert found, "identifier-only deletion must still be flagged"
    assert found[0][3] == found[0][4], "the two op multisets are expected to match"


def test_added_conjunct_is_not_flagged():
    """Narrowing a branch by adding a guard is a normal edit, not corruption."""
    assert not vo.findings(_diff("if x > 0:", "if x > 0 and limit != 0:"))


def test_paren_reformat_is_not_flagged():
    assert not vo.findings(_diff("if x >= limit:", "if (x) >= (limit):"))


def test_bulk_deletion_hunk_is_not_cross_paired():
    """Deleting a block (e.g. a retired module) must not pair every removed line
    with every remaining line: that produced 21 false findings on a real commit,
    because two unrelated lines can differ only an operator by coincidence."""
    diff = (
        "--- a/t.py\n"
        "+++ b/t.py.figure\n"
        "@@ -1,3 +1,1 @@\n"
        "-x =  subtypea + b\n"
        "-if y Dog >= 0 and z != 0:\n"
        "-flag = not q\n"
        "+ALLOW = set()\n"
    )
    assert vo.findings(diff) == []
