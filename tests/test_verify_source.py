"""verify_source: VS16 must not be a finding, real letters still must be."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import verify_source  # noqa: E402


def test_vs16_after_warning_sign_is_not_a_finding():
    # U+26A0 + U+FE0F, the emoji presentation the log strings use.
    assert not verify_source._is_letter("\ufe0f"), "VS16 is not a letter"
    assert not verify_source._is_letter("\u26a0"), "warning sign is a symbol"


def test_real_letters_still_flagged():
    # The class this tool exists for: CJK / Cyrillic spliced into source.
    assert verify_source._is_letter("\u7cfb")



def test_clean_file_passes_and_spliced_file_fails(tmp_path):
    clean = tmp_path / "clean.py"
    clean.write_text('log.warning("\u26a0\ufe0f  boom")\n', encoding="utf-8")
    assert verify_source.check_file(clean) == []

    spliced = tmp_path / "spliced.py"
    spliced.write_text('x = d["\u7cfb"]\n', encoding="utf-8")
    found = verify_source.check_file(spliced)
    assert len(found) == 1
    assert found[0][0] == 1


def test_deleted_path_is_not_a_finding(tmp_path, monkeypatch):
    """A file-deleting commit stages paths that no longer exist; that must not
    fail the corruption gate (it blocked every engine-deletion commit)."""
    monkeypatch.setattr(sys, "argv",
                        ["verify_source.py", str(tmp_path / "gone.py")])
    assert verify_source.main() == 0


def test_spliced_file_still_fails_the_gate(tmp_path, monkeypatch):
    """The fix must not weaken the gate: a real splice still exits 1."""
    bad = tmp_path / "bad.py"
    bad.write_text('x = d["\u7cfb"]\n', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["verify_source.py", str(bad)])
    assert verify_source.main() == 1


def test_market_types_is_ascii_clean():
    """market_types.py is new; catch a corrupted write at commit time, not in prod."""
    import pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "market_types.py"
    text = p.read_text(encoding="utf-8")
    bad = [(i, c) for i, c in enumerate(text) if ord(c) > 127]
    assert not bad, "non-ASCII in market_types.py: %r" % bad[:5]


def test_market_counts_separates_available_from_tradable():
    import market_types
    c = market_types.MarketCounts()
    c.add_series("KXBTCD", fetched=20, tradable=0)
    c.add_series("KXETHD", fetched=60, tradable=3)
    assert (c.available, c.tradable, c.fetch_errors) == (80, 3, 0)
    assert c.by_series["KXBTCD"] == (20, 0)
    assert not c.empty_book

    empty = market_types.MarketCounts()
    empty.add_series("KXBTCD", fetched=0, tradable=0)
    assert empty.empty_book

    broken = market_types.MarketCounts()
    broken.fail_series("KXBTCD")
    assert broken.fetch_errors == 1
    assert not broken.empty_book  # fetch failure is not an empty book
