#!/usr/bin/env python3
"""Fail if a written Python file cannot be parsed as valid syntax.

Why this exists: scripts/verify_source.py catches the character classes we can
recognise -- non-ASCII letters spliced into identifiers, and a name fused onto
a number ("0.94laub"). It structurally cannot catch a splice made of ASCII only,
because there is no character for it to object to:

    file_env.get("K", "")   ->   file_env Ade("K", "")     (clean: 0 findings)
    os.environ.get(x)       ->   os.environ (x)            (same)

Both are corruption. The first is merely broken; the second compiles and returns
a different result, which is worse. The parser is the only oracle that is not
fooled by "the bytes still look like ASCII". Run this together with
verify_source, never instead of it: this check says nothing about a splice that
is legal Python, such as r[<han>] for r["series"].

    python3 scripts/verify_compiles.py FILES...
    python3 scripts/verify_compiles.py --all

Read-only. It compiles to memory and never writes a .pyc beside the source, and
it never imports the target module, so it cannot execute a trading entry point.
Exit 0 clean, 1 findings, 2 no input.
"""
import argparse
import pathlib
import subprocess
import sys


SKIP_DIRS = {".git", "__pycache__", "legacy", "venv", ".venv", "node_modules"}


def _tracked(pattern):
    out = subprocess.run(
        ["git", "ls-files", pattern],
        capture_output=True, text=True, check=True,
    ).stdout
    return [f for f in out.split()
            if not any(p in SKIP_DIRS for p in pathlib.Path(f).parts)]


def check_file(path):
    """Return a SyntaxError if the file does not parse, else None."""
    p = pathlib.Path(path)
    try:
        source = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        err = SyntaxError("not utf-8: %s" % e)
        err.lineno = 1
        err.offset = 0
        return err
    try:
        compile(source, str(p), "exec", dont_inherit=True)
    except SyntaxError as e:
        return e
    return None


def main():
    ap = argparse.ArgumentParser(description="Check Python files parse")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--all", action="store_true", help="check tracked python files")
    args = ap.parse_args()

    paths = _tracked("*.py") if args.all else args.files
    if not paths:
        print("no files given (pass paths or --all)")
        return 2

    bad = 0
    for p in paths:
        if not pathlib.Path(p).exists():
            continue
        err = check_file(p)
        if err is None:
            print("%s: parses" % p)
            continue
        bad += 1
        lineno = err.lineno or 0
        offset = err.offset or 0
        print("%s:%d:%d: SYNTAX %s" % (p, lineno, offset, err.msg))
        try:
            line = pathlib.Path(p).read_text(encoding="utf-8").splitlines()[lineno - 1]
            print("    %s" % line.strip())
        except (IndexError, OSError):
            pass

    print("")
    if bad:
        print("%d file(s) with findings" % bad)
        return 1
    print("all clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
