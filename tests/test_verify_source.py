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
