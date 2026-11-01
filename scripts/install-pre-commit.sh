#!/bin/bash
# Install a pre-commit hook that blocks committing secrets.
# Two layers:
#   1. Filename block — refuses .env, *.pem, *.key, id_rsa, ...
#   2. Content scan  — refuses staged additions containing high-confidence
#                      secret strings (Supabase secret, AWS key, private key
#                      with body, JWT), even inside ordinary source files.
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK_DIR="$REPO_ROOT/.git/hooks"
HOOK_FILE="$HOOK_DIR/pre-commit"

mkdir -p "$HOOK_DIR"

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

exit 0
HOOK_EOF

chmod +x "$HOOK_FILE"
echo "Pre-commit hook installed at $HOOK_FILE"
