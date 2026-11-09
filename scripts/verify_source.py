#!/usr/bin/env python3
"""Fail if a written file can no longer be trusted.

Why this exists: the write/heredoc path on this device intermittently splices
non-ASCII *letters* into source that was meant to be ASCII:

    r["series"]   -> r["<han characters>"]     (a valid subscript: returns None)
    end_lineno    -> end_linenoStar            (a different, undefined name)
    node.lineno   -> node.g<han characters>ojen

and also drops stray ASCII words into numbers (`0.94laub`).

py_compile does NOT catch the character-splice class. A spliced subscript is
legal Python; it compiles, runs, and silently reads the wrong key. A corrupt
file that compiles is more dangerous than one that does not, because it passes
review and then misbehaves in a money path. So this checks the text directly
rather than trusting the parser.

Rule: a non-ASCII *letter* is only allowed in a comment. Non-ASCII punctuation
(em dash, arrow, multiplication sign, ellipsis) is normal prose and is
allowed anywhere -- this repo deliberately writes it in comments, docstrings}
and in user-facing log strings.

The distinction is made with unicodedata.category(), not a hand-maintained
character list: typographic punctuation is in categories P* and S*, while every
script this device has spliced in (Han, Hangul, Cyrillic, Arabic, Hebrew,
Hiragana) is a letter in L*. A blocklist of ranges would silently miss new
ranges; a category test does not.

    python3 scripts/verify_source.py FILES...
    python3 scripts/verify_source.py --all

Exit 0 clean, 1 findings, 2 no input. No writes, and it never imports the target
module (so it cannot execute a trading entry point).
"""
import argparse
import pathlib
import subprocess
import sys
import unicodedata

SKIP_DIRS = {".git", "__pycache__", "legacy", "venv", ".venv", "node_modules"}

# Emoji presentation selectors: invisible, glide after a symbol (U+26A0 + VS16
# renders an emoji), and are Mn in every real emoji log string. They are Mn,
# so the combining-mark rule below would flag every "warning emoji" line and keep
# the gate red for a non-finding. Excluded by name, not by blanket category drop,
# so a genuine combining-mark splice (e.g. U+0301, category Mn) still fires.
_IGNORED_MARKS = {"VARIATION SELECTOR-15", "VARIATION SELECTOR-16"}


def _is_letter(ch):
    """True for a non-ASCII character that is a letter (a splice candidate)."""
    if ord(ch) <= 127:
        return False
    cat = unicodedata.category(ch)
    if cat.startswith("M") and unicodedata.name(ch, "") in _IGNORED_MARKS:
        return False
    return cat.startswith("L") or cat.startswith("M")



def _comment_only_lines(text):
    """1-based line numbers whose content is entirely a comment or blank."""
    out = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            out.add(lineno)
    return out


def check_file(path):
    """Return [(lineno, col, char, text)] for non-ASCII letters outside comments."""
    text = pathlib.Path(path).read_text(encoding="utf-8")
    comment_only = _comment_only_lines(text)
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if lineno in comment_only:
            continue
        for col, ch in enumerate(line, 1):
            if _is_letter(ch):
                findings.append((lineno, col, ch, line.strip()))
    return findings


def main():
    ap = argparse.ArgumentParser(description="Check source for corruption")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--all", action="store_true",
                    help="check tracked python files")
    args = ap.parse_args()

    if args.all:
        out = subprocess.run(["git", "ls-files", "*.py"], capture_output=True,
                             text=True, check=True).stdout
        paths = [f for f in out.split()
                 if not any(p in SKIP_DIRS for p in pathlib.Path(f).parts)]
    else:
        paths = args.files

    if not paths:
        print("no files given (pass paths or --all)")
        return 2

    bad = 0
    for p in paths:
        if not pathlib.Path(p).exists():
            print("%s: MISSING" % p)
            bad += 1
            continue
        try:
            found = check_file(p)
        except UnicodeDecodeError as e:
            print("%s: not utf-8 (%s)" % (p, e))
            bad += 1
            continue
        for lineno, col, ch, line in found:
            name = unicodedata.name(ch, "U+%04X" % ord(ch))
            print("%s:%d:%d: %s %s in %s"
                  % (p, lineno, col, name, repr(ch), line))
        if found:
            bad += 1
        else:
            print("%s: clean" % p)

    print("")
    if bad:
        print("%d file(s) with findings" % bad)
        return 1
    print("all clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
