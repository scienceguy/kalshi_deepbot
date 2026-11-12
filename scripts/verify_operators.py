#!/usr/bin/env python3
"""Fail if a staged edit changes a comparison/boolean operator or a `not`.

Why this exists: the other three gates reason about *names*. A splice that swaps
an operator keeps every name intact, so they all report clean and it commits:

    if x == THRESHOLD:   ->  if x != THRESHOLD:
    if not flag:         ->  if flag significantly:
    if a < b:            ->  if a <= b:
    if a and b:          ->  if a or b:

This gate asks a different question. For each removed/added pair of lines that
parse, it compares (a) the syntax tree with all operator nodes blanked out and
(b) the multiset of operator node types. It reports a pair only when (a) is
IDENTICAL and (b) DIFFERS: the rest of the line is untouched and only the
operator moved. Anything else -- a renamed variable, a changed literal, a
reflowed expression -- is an ordinary edit and is left alone, so the gate flags
the minimal operator edit rather than all edits.

It reads `git diff --cached`, so it sees exactly what would be committed.

    python3 scripts/verify_operators.py
    python3 scripts/verify_operators.py --diff-file some.diff

Exit 0 clean, 1 findings. Read-only: it never writes a file, and it parses the
unified diff text only; no file is imported and no staged module is executed.
"""
import argparse
import ast
import collections
import subprocess
import sys


# AST nodes whose *presence* is what an operator splice moves. ast.cmpop /
# ast.boolop / ast.operator are the symbols themselves (Eq, And, Add...);
# UnaryOp is where `not` lives, so `not x` -> `x` changes this multiset.
OP_NODES = (ast.cmpop, ast.boolop, ast.operator, ast.unaryop)


def _identifiers(tree):
    """Every Name/Attribute/Constant the line mentions.

    This is the "kept the wording" half of the test: an operator-only edit does
    not rename a variable or change a literal. Comparing these makes a rename
    in the same hunk an ordinary edit, while `not flag` -> `flag` still matches.
    """
    parts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            parts.append(("name", node.id))
        elif isinstance(node, ast.Attribute):
            parts.append(("attr", node.attr))
        elif isinstance(node, ast.Constant):
            parts.append(("const", repr(node.value)))
    return sorted(parts)


def _blank_ops(tree):
    """Return the tree with every operator/boolean symbol erased.

    After this, two expressions that differ only by operators have the same dump.
    `not not x` and `not x` both yield UnaryOp(operand=Name('x')) because the op
    field was cleared -- which is the point: the mask is blind to *how many*
    operators there are, the count below is what tells them apart.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            node.ops = []
        if isinstance(node, ast.BoolOp):
            node.op = None
        if isinstance(node, ast.UnaryOp):
            node.op = None
        if isinstance(node, ast.BinOp):
            node.op = None
        if isinstance(node, ast.AugAssign):
            node.op = None
        if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.AST):
            node.op = None
    return ast.dump(tree, annotate_fields=False, include_attributes=False)


def _count_ops(tree):
    """Multiset of operator node type names in the tree (symmetry-free)."""
    c = collections.Counter()
    for node in ast.walk(tree):
        if isinstance(node, OP_NODES):
            for base in (ast.cmpop, ast.boolop, ast.operator, ast.unaryop):
                if isinstance(node, base):
                    c[type(node).__name__] += 1
                    break
    return c


def _sig(src):
    """(masked dump, operator multiset) for one line, or None if it has no expression.

    A line like `if x == THRESHOLD:` is not a statement on its own (the body is
    the next line), so wrapping the line in `if __probe__:` and parsing the whole
    module would drag in the body. Instead take the expression AFTER the leading
    keyword and parse just that. Lines that are not an expression we care about
    return None and are skipped.
    """
    src = src.strip()
    for kw in ("elif ", "if ", "while ", "assert ", "return "):
        if src.startswith(kw):
            src = src[len(kw):]
            break
    # strip a trailing colon (suite header) only when it is the final character
    if src.endswith(":"):
        src = src[:-1]
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError:
        try:
            tree = ast.parse(src, mode="exec")
        except SyntaxError:
            return None
    # Count FIRST: _blank_ops mutates the tree in place and would empty the
    # operator lists before they could be counted.
    counts = _count_ops(tree)
    ids = _identifiers(tree)
    masked = _blank_ops(tree)
    return ((masked, ids), counts)


def pairs(diff_text):
    """Yield (path, removed_line, added_line) for lines changed within one file."""
    path = None
    removed = []
    added = []

    def flush():
        for old in removed:
            for new in added:
                if old.strip() != new.strip():
                    yield path, old, new

    for line in diff_text.splitlines():
        if line.startswith("--- "):
            if path and (removed or added):
                yield from flush()
            removed, added = [], []
            path = None
        elif line.startswith("+++ "):
            p = line[4:].strip()
            path = p[2:] if p.startswith("b/") else p
        elif line.startswith("@@"):
            if removed or added:
                yield from flush()
            removed, added = [], []
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    if removed or added:
        yield from flush()


def findings(diff_text):
    """[(path, old, new, old_ops, new_ops)] for operator-only edits."""
    out = []
    for path, old, new in pairs(diff_text):
        a, b = _sig(old), _sig(new)
        if a is None or b is None:
            continue
        (mask_a, ops_a), (mask_b, ops_b) = a, b
        # Operator-only edit: the identifiers and literals are identical, but the
        # operator multiset changed. The masked SHAPE is not required to match,
        # because dropping a `not` legitimately changes the shape (UnaryOp -> Name)
        # while changing no identifier -- that is exactly the case to catch.
        if mask_a[1] != mask_b[1]:
            continue  # something other than an operator changed
        if ops_a != ops_b:
            out.append((path, old, new, ops_a, ops_b))
    return out


def main():
    ap = argparse.ArgumentParser(description="Flag operator/not drift in a staged diff")
    ap.add_argument("--diff-file", help="read a unified diff from this file instead of git")
    args = ap.parse_args()

    if args.diff_file:
        with open(args.diff_file, encoding="utf-8") as fh:
            diff_text = fh.read()
    else:
        diff_text = subprocess.run(
            ["git", "diff", "--cached", "-U0"],
            capture_output=True, text=True, check=True,
        ).stdout

    hits = findings(diff_text)
    for path, old, new, ops_a, ops_b in hits:
        print("%s: operator/not change" % path)
        print("   -  %s   %s" % (old.strip(), dict(ops_a)))
        print("   +  %s   %s" % (new.strip(), dict(ops_b)))

    print("")
    if hits:
        print("%d line(s) with operator/not drift" % len(hits))
        return 1
    print("all clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
