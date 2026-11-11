"""verify_compiles: the ASCII-only splice class verify_source cannot see."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import verify_compiles  # noqa: E402


def test_good_file_parses(tmp_path):
    good = tmp_path / "good.py"
    good.write_text("x = 1\n", encoding="utf-8")
    assert verify_compiles.check_file(good) is None


def test_ascii_only_splice_is_caught(tmp_path):
    # verify_source calls this clean: every character is ASCII.
    bad = tmp_path / "bad.py"
    bad.write_text('sig = os.environ.get("X") or file_env Ade("X", "")\n',
                   encoding="utf-8")
    err = verify_compiles.check_file(bad)
    assert err is not None
    assert err.lineno == 1


def test_legal_python_splice_is_not_claimed(tmp_path):
    """This gate only promises to see what does not parse. A splice that is
    legal Python must be caught by review, not believed to be caught here."""
    legal = tmp_path / "legal.py"
    legal.write_text('r = d["\\u7cfb"]\n', encoding="utf-8")
    assert verify_compiles.check_file(legal) is None


def test_deleted_path_is_not_a_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv",
                        ["verify_compiles.py", str(tmp_path / "gone.py")])
    assert verify_compiles.main() == 0


def test_spliced_file_fails_the_gate(tmp_path, monkeypatch):
    bad = tmp_path / "bad.py"
    bad.write_text("def f(\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["verify_compiles.py", str(bad)])
    assert verify_compiles.main() == 1


def test_every_tracked_python_file_parses():
    """The whole tree, not just the file being committed."""
    root = pathlib.Path(__file__).resolve().parents[1]
    bad = []
    for p in sorted(root.rglob("*.py")):
        if any(part in {"legacy", "venv", ".venv", "node_modules", "__pycache__"}
               for part in p.parts):
            continue
        if verify_compiles.check_file(p) is not None:
            bad.append(str(p.relative_to(root)))
    assert not bad, "files that do not parse: %r" % bad
