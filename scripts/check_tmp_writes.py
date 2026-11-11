#!/usr/bin/env python3
"""Block staged additions that write to /tmp.

/tmp is NOT writable on this termux/proot device (tmpfs owned by shell:shell
0771; the app user gets only ``--x``). Temp files must go to $TMPDIR
(/data/data/com.termux/files/home/tmp) or
/data/data/com.termux/files/usr/tmp/opencode instead.

See AGENTS.md "System constraints (all agents)".

This gate is read-only; it never rewrites a file. Install via layer 4 of
scripts/install-pre-commit.sh.
"""
import re
import subprocess
import sys

# Files allowed to reference /tmp (docs, the constraint test, this checker).
ALLOWED_BASENAMES = {
    "AGENTS.md",
    "check_tmp_writes.py",
    "test_tmp_constraint.py",
}

# Patterns indicating a WRITE path targeting /tmp (not just a mention).
WRITE_PATTERNS = [
    r'open\s*\(\s*["\'].*/tmp',           # open("/tmp...)
    r"open\s*\(\s*['\"].*/tmp",           # open('/tmp...)
    r'Path\s*\(\s*["\'].*/tmp',           # Path("/tmp...)
    r"Path\s*\(\s*['\"].*/tmp",           # Path('/tmp...)
    r'os\.path\.join\s*\(.*["\'].*/tmp',  # os.path.join(..., "/tmp...)
    r"os\.path\.join\s*\(.*['\"].*/tmp",  # os.path.join(..., '/tmp...)
    r'tempfile.*["\'].*/tmp',             # tempfile with /tmp path
    r"tempfile.*['\"].*/tmp",
    r'>\s*/tmp\b',                         # shell redirect > /tmp
    r'>>\s*/tmp\b',                        # shell append >> /tmp
    r'tee\s+/tmp\b',                       # tee /tmp
    r'cat\s+>\s*/tmp\b',                   # cat > /tmp
]

COMPILED = [re.compile(p) for p in WRITE_PATTERNS]
FULL_LINE_COMMENT = re.compile(r'^\s*#')


def staged_additions(files):
    """Return {filename: [added line contents]} for staged additions."""
    result = {}
    for f in files:
        out = subprocess.run(
            ["git", "diff", "--cached", "-U0", "--", f],
            capture_output=True, text=True, check=True,
        ).stdout
        added = []
        for line in out.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                content = line[1:]
                if FULL_LINE_COMMENT.match(content):
                    continue
                added.append(content)
        if added:
            result[f] = added
    return result


def check_file(filename, lines):
    """Return list of (lineno, content) violations."""
    violations = []
    for i, content in enumerate(lines, 1):
        for rx in COMPILED:
            if rx.search(content):
                violations.append((i, content))
                break
    return violations


def main():
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True, text=True, check=True,
    ).stdout.split()

    if not staged:
        print("OK: no staged files")
        return 0

    additions = staged_additions(staged)
    any_violation = False
    for filename, lines in additions.items():
        if filename.split("/")[-1] in ALLOWED_BASENAMES:
            continue
        for lineno, content in check_file(filename, lines):
            print(f"ERROR: {filename}:{lineno}: writes to /tmp")
            print(f"       {content.strip()[:120]}")
            any_violation = True

    if any_violation:
        print()
        print("/tmp is NOT writable on this device.")
        print("Use $TMPDIR or /data/data/com.termux/files/usr/tmp/opencode.")
        print("See AGENTS.md 'System constraints (all agents)'.")
        return 1

    print("OK: no /tmp writes in staged additions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
