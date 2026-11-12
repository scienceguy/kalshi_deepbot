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
splice that swaps a defined name for another defined name, flips a comparison
operator, drops a `not`, or renames a parameter through every use, still parses
AND still resolves. Those need review. This gate removes the cheapest way for a
splice to survive unnoticed, nothing more.

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

# legacy/ is skipped by --all ON PURPOSE and the reason is recorded here, not
# left implicit: it is not imported by the trader (verified: no live importer),
# so none of it can reach a trade.
#
# Two hits were REAL and are now fixed (keep this note so nobody re-introduces
# them): legacy/market_data_aggregator.py:401,404 read 'kalshi_series' in a method
# whose parameter is 'kalshi_series_list'. They are gone from the baseline and the
# gate reports that file clean.
#
# One hit remains and is NOT a one-token fix:
#   legacy/polymarket_arb.py:139   self._get_polymarket_prices(pm)
#   - 'pm' is never bound (138 builds 'pm_token_id'); AND
#   - PolymarketArb has no '_get_polymarket_prices' method at all -- grep the tree.
# Renaming 'pm' to 'pm_token_id' would satisfy this gate and still raise
# AttributeError on the next line, so it is left as deliberately-dead code rather
# than papered over. If legacy/ is ever revived, that class must be finished or
# deleted first. Scanning legacy/ is opt-in: --all --include-legacy.
LEGACY_SKIP = "legacy"
LEGACY_KNOWN_UNDEFINED = {
    ("legacy/polymarket_arb.py", 139, "pm"),
}

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


def _tracked_files(pattern):
    out = subprocess.run(
        ["git", "ls-files", pattern],
        capture_output=True, text=True, check=True,
    ).stdout
    return out.split()


def cooked_name(text):
    """'undefined name 'oder'' -> 'oder' (the name pyflakes complained about)."""
    if "'" in text:
        return text.split("'")[1]
    return text


def main():
    ap = argparse.ArgumentParser(description="Check for undefined names")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--all", action="store_true",
                    help="check tracked python files (legacy/ excluded)")
    ap.add_argument("--include-legacy", action="store_true",
                    help="also scan legacy/, honouring LEGACY_KNOWN_UNDEFINED")
    args = ap.parse_args()

    if args.all:
        paths = _tracked("*.py")
        if args.include_legacy:
            paths = sorted(set(paths) | set(_tracked_files("legacy/*.py")))
        else:
            # --all covers the live tree unless legacy/ is asked for by name; the
            # legacy findings are then neither reported nor silently dropped, they
            # are listed in LEGACY_KNOWN_UNDEFINED and counted by --include-legacy.
            paths = [f for f in paths if not f.startswith("legacy/")]
    else:
        paths = args.files
    if not paths:
        print("no files given (pass paths or --all)")
        return 2

    try:
        import pyflakes  # noqa: F401
    except ImportError:
        print("pyflakes not installed; skipping undefined-name check "
              "(pip install pyflakes to enforce)")
        return 0

    # The recorded legacy hits are a baseline: expected present, so a fix or a
    # deletion has to update this file. Anything else in legacy is a finding.
    # Only a full-tree scan with --include-legacy can judge the baseline: a scan
    # of one legacy file would call every other recorded hit "stale".
    allow = set()
    if args.include_legacy:
        allow = LEGACY_KNOWN_UNDEFINED
    elif not args.all:
        # Explicit paths: still tolerate a recorded hit in a file named on the
        # command line, but never claim to adjudicate the ones not scanned.
        allow = {e for e in LEGACY_KNOWN_UNDEFINED
                 if any(e[0] == str(p) for p in paths)}

    bad = 0
    seen = set()
    for p in paths:
        if not pathlib.Path(p).exists():
            continue
        real = []
        for lineno, col, cls, text in find_undefined(p):
            if (p, lineno, cooked_name(text)) in allow:
                seen.add((p, lineno, cooked_name(text)))
                continue
            real.append((lineno, col, cls, text))
        if not real:
            print("%s: no undefined names" % p)
            continue
        bad += 1
        for lineno, col, cls, text in real:
            print("%s:%d:%d: %s %s" % (p, lineno, col, cls, text))

    # A baseline entry that no longer reproduces means the tree got fixed (or
    # the file went away). Fail so the entry is deleted, not left to mask a
    # future real hit at the same line.
    stale = allow - seen if args.include_legacy else set()
    if stale:
        print("")
        for p, lineno, name in sorted(stale):
            print("%s:%d: baseline does not reproduce undefined name %r"
                  % (p, lineno, name))
        print("       The tree is better than the baseline -- delete that entry from")
        print("       LEGACY_KNOWN_UNDEFINED in scripts/verify_undefined.py.")
        return 1

    print("")
    if bad:
        print("%d file(s) with findings" % bad)
        return 1
    if allow:
        print("all clean (%d legacy baseline finding(s) accounted for)" % len(seen))
        return 0
    print("all clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
