#!/usr/bin/env python3
"""Fail if a written Python file references a name that is never defined.

Why this exists: scripts/verify_source.py catches non-ASCII and number+name
splices. scripts/verify_compiles.py catches splices that break parsing. Neither
sees a splice that is pure ASCII *and* still valid Python, which is the class
that reaches production:

    return oder            ->  return oder        (pyflakes: undefined name)
    return "yes" if x      ->  return Fill         (pyflakes: undefined name)
    if not flag:           ->  if flag:            (NOT caught -- see below)

pyflakes reports the first two as undefined names, which is the closest a
mechanical tool gets to "this token was not produced by the author". Only that
message class is treated as a finding. Imported-but-unused, unused locals and
redefinitions are ignored on purpose: this repo has 84 of them, they are style,
and a gate that fails on 84 pre-existing lines is a gate everyone passes with
--no-verify.

Honest limit, do not overclaim it: this does NOT bound the whole class. A
splice that swaps one DEFINED name for another still parses AND still resolves,
and nothing here sees it. A comparison operator swap or a dropped or
doubled `not` are covered by scripts/verify_operators.py instead. What all three
gates provably miss is listed under "Not caught" in AGENTS.md -- read it before
treating a clean run as review. This gate removes the cheapest way for a splice
to survive unnoticed, nothing more.

    python3 scripts/verify_undefined.py FILES...
    python3 scripts/verify_undefined.py --all

Optional: if pyflakes is not installed the check is SKIPPED with exit 0 and a
notice, so a fresh clone without dev extras is never blocked by its own gate.

Read-only and never imports the target module (pyflakes compiles to an AST), so
it cannot execute a trading entry point. Exit 0 clean, 1 findings, 2 no input.
"""
import argparse
import pathlib
import subprocess
import sys


SKIP_DIRS = {".git", "__pycache__", "venv", ".venv", "node_modules"}

# No directory exemption and no baseline. legacy/ is scanned like everything
# else. It briefly held a recorded exemption for one unfixed name; that file
# (legacy/polymarket_arb.py) called a method defined nowhere in the tree, so it
# was deleted rather than papered over (AUDIT_REPORT.md:236 lists it under
# "unused exchange clients, delete"). An exemption set whose only correct state
# is empty is a hole waiting to be refilled, so the mechanism is gone too.

# Only these message classes mean "a token that nothing declared". Everything
# else pyflakes emits (unused import, unused variable, redefinition scenarios,
# f-string without placeholders) is style and is deliberately not a finding.
FINDING_CLASSES = ("UndefinedName", "UndefinedLocal")


def _tracked(pattern):
    out = subprocess.run(
        ["git", "ls-files", pattern],
        capture_output=True, text=True, check=True,
    ).stdout
    return [f for f in out.split()
            if not any(p in SKIP_DIRS for p in pathlib.Path(f).parts)]


def find_undefined(path):
    """Return [(lineno, col, cls, text)] for undefined-name findings in one file."""
    import ast

    import pyflakes.messages as msgs
    from pyflakes import checker

    source = pathlib.Path(path).read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, path)
    except SyntaxError:
        # Not this gate's job: verify_compiles.py already owns "does not parse",
        # and reporting it twice trains people to ignore one of the two.
        return []

    wanted = tuple(getattr(msgs, name) for name in FINDING_CLASSES)
    out = []
    for msg in checker.Checker(tree, path, source).messages:
        if isinstance(msg, wanted):
            flat = msg.message % msg.message_args if msg.message_args else msg.message
            out.append((msg.lineno, msg.col, type(msg).__name__, flat))
    return out


def main():
    ap = argparse.ArgumentParser(description="Check for undefined names")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--all", action="store_true", help="check tracked python files")
    args = ap.parse_args()

    paths = _tracked("*.py") if args.all else args.files
    if not paths:
        print("no files given (pass paths or --all)")
        return 2

    try:
        import pyflakes  # noqa: F401
    except ImportError:
        print("pyflakes not installed; skipping undefined-name check "
              "(pip install pyflakes to enforce)")
        return 0

    bad = 0
    for p in paths:
        if not pathlib.Path(p).exists():
            continue
        real = find_undefined(p)
        if not real:
            print("%s: no undefined names" % p)
            continue
        bad += 1
        for lineno, col, cls, text in real:
            print("%s:%d:%d: %s %s" % (p, lineno, col, cls, text))

    print("")
    if bad:
        print("%d file(s) with findings" % bad)
        return 1
    print("all clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
