#!/bin/bash
# Install a pre-commit hook that blocks committing secrets and corrupted source.
# Three layers:
#   1. Filename block — refuses .env, *.pem, *.key, id_rsa, ...
#   2. Content scan  — refuses staged additions containing high-confidence
#                      secret strings (Supabase secret, AWS key, private key
#                      with body, JWT), even inside ordinary source files.
#   3. Token-corruption guard — refuses staged Python that fails
#                      scripts/verify_source.py (non-ASCII letter splices,
#                      number+fused-name splices).
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK_DIR="$REPO_ROOT/.git/hooks"
HOOK_FILE="$HOOK_DIR/pre-commit"
# The versioned source of truth. Hooks under .git/hooks are not cloned or
# reviewed and are overwritten by operations that rebuild the repo, so the
# authoritative copy lives in the tree and is copied into place below.
VERSIONED_HOOK="$REPO_ROOT/.githooks/pre-commit"

mkdir -p "$HOOK_DIR"
mkdir -p "$REPO_ROOT/.githooks"

cat > "$HOOK_FILE" << 'HOOK_EOF'
#!/bin/sh
# Pre-commit hook: block committing secrets.
# Install with: scripts/install-pre-commit.sh

STAGED=$(git diff --cached --name-only)

# --- 1. Filename block -------------------------------------------------------
for f in $STAGED; do
    case "$f" in
        .env|*/.env|*.env|*.pem|*.key|*.key.txt|id_rsa|id_ed25519|credentials.json|*_rsa)
            echo "ERROR: refusing to commit secret file: $f"
            echo "       Unstage it: git reset HEAD -- '$f'"
            exit 1
            ;;
    esac
done

# --- 2. Content scan (staged additions only) ---------------------------------
# High-confidence patterns; chosen to avoid matching code literals and
# placeholder values (e.g. the '-----BEGIN RSA PRIVATE KEY-----...' example
# in .env.example and kalshi_client.py).
PATTERN='sb_secret_[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----.*MII[A-Za-z0-9+/]{30,}|eyJhbGciOiJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'

HITS=$(git diff --cached -U0 | grep -E '^\+' | grep -E -- "$PATTERN" || true)
if [ -n "$HITS" ]; then
    echo "ERROR: staged changes appear to contain hardcoded secrets:"
    echo "$HITS" | cut -c1-100
    echo "       Move secrets to ~/.secrets/ and load them at runtime."
    exit 1
fi

# --- 3. Token-corruption guard on staged Python -------------------------------
# The generation channel that writes this tree intermittently splices foreign
# script into identifiers/strings and ASCII words into numbers ("0.94laub").
# Those files compile, so tests alone do not catch them. See AGENTS.md
# "TOKEN CORRUPTION". This gate is read-only; it never rewrites a file.
STAGED_PY=$(echo "$STAGED" | grep -E '\.py$' || true)
if [ -n "$STAGED_PY" ]; then
    # shellcheck disable=SC2086
    if ! python3 "$(git rev-parse --show-toplevel)/scripts/verify_source.py" $STAGED_PY; then
        echo "ERROR: staged Python files contain token corruption (see AGENTS.md)"
        echo "       Fix the flagged lines, do not bypass with --no-verify."
        exit 1
    fi
    # verify_source only sees non-ASCII letters and number+name splices. The
    # same channel also emits ASCII-only substitutions ("file_env.get" became
    # "file_env Ade(", "or" became a stray name). Those look like source to it.
    # The parser is the oracle for that class, and it is read-only.
    if ! python3 "$(git rev-parse --show-toplevel)/scripts/verify_compiles.py" $STAGED_PY; then
        echo "ERROR: staged Python files do not parse (ASCII-only token splice)"
        echo "       See AGENTS.md TOKEN CORRUPTION. Do not bypass with --no-verify."
        exit 1
    fi
    # A splice can be ASCII-only AND still parse, which defeats both gates above:
    # "return oder" is valid syntax. pyflakes reports the token that nothing
    # declared, which is what a substitution looks like to Python. Only undefined
    # names are enforced (see verify_undefined.py); the 84 unused-import style
    # findings in this tree stay advisory. Skipped cleanly if pyflakes is absent.
    if ! python3 "$(git rev-parse --show-toplevel)/scripts/verify_undefined.py" $STAGED_PY; then
        echo "ERROR: staged Python files reference undefined names (token splice)"
        echo "       See AGENTS.md TOKEN CORRUPTION. Do not bypass with --no-verify."
        exit 1
    fi
    # Operator drift: == -> !=, < -> <=, a dropped or doubled `not`, and -> or.
    # Every name survives those edits, so all three gates above report clean.
    if ! python3 "$(git rev-parse --show-toplevel)/scripts/verify_operators.py"; then
        echo "ERROR: staged diff changes a comparison/boolean operator or a not"
        echo "       See AGENTS.md TOKEN CORRUPTION. Do not bypass with --no-verify."
        exit 1
    fi
    # A PURELY ADDED condition in the money path has no removed counterpart, so the
    # gate above never compares it (measured: exit 0). Nothing mechanical can judge
    # whether the added branch is the intended one, so require the reviewer to say so
    # next to the line, as "<line>  # reviewed: why this guard belongs here".
    if ! python3 "$(git rev-parse --show-toplevel)/scripts/verify_review_marked.py"; then
        echo "ERROR: new condition in the money path without a review marker"
        echo "       See AGENTS.md TOKEN CORRUPTION. Do not bypass with --no-verify."
        exit 1
    fi
fi

# --- 4. /tmp-write guard on staged additions ---------------------------------
# /tmp is NOT writable on this termux/proot device. Temp files must go to
# $TMPDIR or /data/data/com.termux/files/usr/tmp/opencode. See AGENTS.md
# "System constraints (all agents)". This gate is read-only.
if ! python3 "$(git rev-parse --show-toplevel)/scripts/check_tmp_writes.py"; then
    echo "ERROR: staged changes write to /tmp (see AGENTS.md)"
    echo "       Use $TMPDIR or /data/data/com.termux/files/usr/tmp/opencode."
    echo "       Do not bypass with --no-verify."
    exit 1
fi

exit 0
HOOK_EOF

# The heredoc above already wrote $HOOK_FILE; mirror that exact content into the
# versioned path so the two can never drift.
cp "$HOOK_FILE" "$VERSIONED_HOOK"
chmod +x "$VERSIONED_HOOK" "$HOOK_FILE"

echo "Pre-commit hook installed at $HOOK_FILE"
echo "Versioned source of truth:            $VERSIONED_HOOK"
if ! diff -q "$VERSIONED_HOOK" "$HOOK_FILE" >/dev/null; then
    echo "WARNING: installed hook differs from the versioned copy" >&2
fi
echo "A fresh clone must run this once: sh scripts/install-pre-commit.sh"

# --- pre-push: same gates, but over the commits leaving this clone ------------
# The pre-commit hook only protects a writer who installed it. The pre-push hook
# runs against whatever is about to become public, which is the path that let the
# audit commits above reach main ungated.
VERSIONED_PUSH="$REPO_ROOT/.githooks/pre-push"
PUSH_SRC="$REPO_ROOT/scripts/pre-push"

if [ -f "$PUSH_SRC" ]; then
    cp "$PUSH_SRC" "$VERSIONED_PUSH"
    chmod +x "$VERSIONED_PUSH"
    if ! diff -q "$PUSH_SRC" "$VERSIONED_PUSH" >/dev/null; then
        echo "WARNING: versioned pre-push differs from scripts/pre-push" >&2
    fi
    echo "Pre-push hook versioned at:          $VERSIONED_PUSH"
fi
