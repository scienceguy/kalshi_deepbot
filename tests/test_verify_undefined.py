"""verify_undefined: the valid-syntax ASCII splice the other two gates bless."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import verify_undefined  # noqa: E402


def test_clean_file_has_no_findings(tmp_path):
    good = tmp_path / "good.py"
    good.write_text(
        "def f(items):\n"
        "    total = 0\n"
        "    for i in items:\n"
        "        total += i\n"
        "    return total\n",
        encoding="utf-8",
    )
    assert verify_undefined.find_undefined(good) == []


def test_undefined_name_is_a_finding(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def should_trade(edge_cents):\n"
        "    return oder\n",
        encoding="utf-8",
    )
    found = verify_undefined.find_undefined(bad)
    assert len(found) == 1
    assert found[0][2] == "UndefinedName"
    assert found[0][0] == 2


def test_unused_import_is_not_a_finding(tmp_path):
    """84 pre-existing style hits must not fail the gate, or it gets bypassed."""
    style = tmp_path / "style.py"
    style.write_text("import os\nx = 1\n", encoding="utf-8")
    assert verify_undefined.find_undefined(style) == []


def test_syntax_error_is_left_to_verify_compiles(tmp_path):
    """Two gates reporting one defect trains people to ignore one of them."""
    broken = tmp_path / "broken.py"
    broken.write_text("def f(\n", encoding="utf-8")
    assert verify_undefined.find_undefined(broken) == []


def test_deleted_path_is_not_a_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["verify_undefined.py", str(tmp_path / "gone.py")])
    assert verify_undefined.main() == 0


def test_undefined_name_fails_the_gate(tmp_path, monkeypatch):
    bad = tmp_path / "bad.py"
    bad.write_text("y = oder\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["verify_undefined.py", str(bad)])
    assert verify_undefined.main() == 1


def test_all_enumeration_includes_untracked_not_ignored(tmp_path, monkeypatch):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "untracked.py").write_text("y = 2\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (repo / "ignored.py").write_text("z = 3\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "tracked.py"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    assert verify_undefined._tracked("*.py") == ["tracked.py", "untracked.py"]
