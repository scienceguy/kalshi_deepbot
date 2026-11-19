#!/usr/bin/env python3
"""J12: Large file guard for staged changes.

Fails when a staged file:
- Exceeds MAX_SIZE_KB (default 1024 KB = 1 MB), OR
- Matches a log/snapshot/data pattern that should be gitignored, unless
  explicitly allowlisted in scripts/large_file_allowlist.txt

Patterns (from .gitignore): *.log.*, *.jsonl.*, *.gz, *_snapshots.jsonl*,
*_paper_ledger.jsonl, maker_fill_test.jsonl, data/cfb/
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# Configurable limit
MAX_SIZE_KB = 1024  # 1 MB

# Patterns that should be gitignored (from .gitignore)
IGNORED_PATTERNS = [
    r"\.log\.\d+$",
    r"\.jsonl\.\d+$",
    r"\.gz$",
    r"_snapshots\.jsonl",
    r"_paper_ledger\.jsonl$",
    r"maker_fill_test\.jsonl$",
    r"^data/cfb/",
]

ALLOWLIST_PATH = Path(__file__).resolve().parent / "large_file_allowlist.txt"


def load_allowlist() -> set[str]:
    if not ALLOWLIST_PATH.exists():
        return set()
    with ALLOWLIST_PATH.open() as f:
        return {line.strip() for line in f if line.strip() and not line.startswith("#")}


def is_ignored_pattern(path: str) -> bool:
    for pat in IGNORED_PATTERNS:
        if re.search(pat, path):
            return True
    return False


def get_staged_files() -> list[tuple[str, int]]:
    """Return list of (path, size_kb) for staged files."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    names = [n for n in result.stdout.split("\0") if n]
    out = []
    for name in names:
        # Get size from working tree (staged file has same content)
        try:
            size = os.path.getsize(name)
            out.append((name, size // 1024))
        except OSError:
            pass
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true", help="Check only staged files")
    _ = ap.parse_args(argv)

    allowlist = load_allowlist()
    staged = get_staged_files()

    errors = []
    for path, size_kb in staged:
        if path in allowlist:
            continue
        if size_kb > MAX_SIZE_KB:
            errors.append(f"{path}: {size_kb} KB > {MAX_SIZE_KB} KB limit")
        elif is_ignored_pattern(path):
            errors.append(f"{path}: matches ignored pattern (should be in .gitignore)")

    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())