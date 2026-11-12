#!/usr/bin/env python3
"""Require an explicit review marker on NEW condition lines in the money path.

Why this exists: scripts/verify_operators.py compares a removed line against an
added one. A condition that is only ADDED has no removed counterpart, so there is
no pair to compare and it passes every gate (measured: exit 0). That hole cannot
be closed by comparing harder, because nothing was replaced -- the new branch is
legitimate feature work by construction, and only a human can say whether it is
the branch that was intended.

So this gate does not guess intent. It refuses to let a new guard in a
money/order/risk file enter history unreviewed, and makes the reviewer say so in
the source, next to the line:

    if balance < cash_floor:  # reviewed: no-trade when below floor
        return

The marker is `# reviewed: <why>`, at least eight characters of reason, on the
added line or the line immediately above it. A test that wants to plant such a
line without the marker can pass --allow-unreviewed to get the finding list back
instead of an exit code, which is how the gate is itself tested.

    python3 scripts/verify_review_marked.py               # staged diff
    python3 scripts/verify_review_marked.py --diff-file f # a unified diff
    python3 scripts/verify_review_marked.py --allow-unreviewed

Exit 0 clean, 1 findings (or unreviewed conditions, without --allow-unreviewed).
Read-only: parses diff text, never imports the module, never writes.

Deliberate limit: the marker is a convention, not proof. It records that someone
looked; it cannot record that they were right. A corrupted line carrying a
plausible reason still passes. Do not describe this as validation of intent.
"""
import argparse
import ast
import pathlib
import re
import subprocess
import sys


# Files where a new condition can change whether, and how much, the bot trades.
# Keep this list explicit and short: a directory glob would quietly widen it.
MONEY_PATH = (
    "trader.py",
    "risk.py",
    "signals.py",
    "gates.py",
    "fees.py",
    "kalshi_client.py",
    "market_finder.py",
)

COND_FORMS = ("if ", "elif ", "while ", "assert ")

MARKER = re.compile(r"#\s*reviewed:\s*(\S.{7,})")
MIN_REASON = 8


def is_condition(src):
    s = src.strip()
    return any(s.startswith(f) for f in COND_FORMS)


def parse_added(diff_text):
    """[(path, lineno_in_new_file, source)] for added lines in money-path files."""
    out = []
    path = None
    new_no = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            spec = line[4:].strip()
            path = spec[2:] if spec.startswith("b/") else spec
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                new_no = int(m.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((path, new_no, line[1:]))
            new_no += 1
        elif line.startswith("-") and not line.startswith("---"):
            pass  # removed line: does not advance the new-file counter
        elif path is not None:
            new_no += 1  # context line
    return out


def unreviewed(diff_text):
    """[(path, lineno, source)] for added conditions with no review marker.

    The marker is looked for in the DIFF, not on disk: the staged diff is what
    would be committed, and the working tree may already differ from it. A marker
    counts if it is on the added line itself, or on the immediately preceding added
    line of the same file (so a comment above the guard also works).
    """
    out = []
    prev_added = None
    prev_path = None

    for path, lineno, src in parse_added(diff_text):
        above = prev_added if prev_path == path else None

        hit = None
        for cand in (src, above):
            if cand is None:
                continue
            m = MARKER.search(cand)
            if m and len(m.group(1).strip()) >= MIN_REASON:
                hit = cand
                break

        if path in MONEY_PATH and is_condition(src) and hit is None:
            out.append((path, lineno, src))

        prev_added = src
        prev_path = path
    return out


def main():
    ap = argparse.ArgumentParser(description="Require review markers on new money-path conditions")
    ap.add_argument("--diff-file", help="read a unified diff from this file instead of git")
    ap.add_argument("--allow-unreviewed", action="store_true",
                    help="list unreviewed conditions but exit 0 (for tests)")
    args = ap.parse_args()

    if args.diff_file:
        with open(args.diff_file, encoding="utf-8") as fh:
            diff_text = fh.read()
    else:
        diff_text = subprocess.run(
            ["git", "diff", "--cached", "-U3"],
            capture_output=True, text=True, check=True,
        ).stdout

    hits = unreviewed(diff_text)
    for path, lineno, src in hits:
        print("%s:%d: new condition without '# reviewed: <why>'" % (path, lineno))
        print("    %s" % src.strip())

    print("")
    if hits:
        print("%d unreviewed condition(s) in the money path" % len(hits))
        print("Add a reason on the line or the one above:")
        print("    <line>  # reviewed: why this guard is intended")
        return 0 if args.allow_unreviewed else 1
    print("all clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
