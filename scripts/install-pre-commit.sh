#!/bin/bash
# Install pre-commit hooks to prevent accidental secret commits
set -e

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)/.git/hooks"
HOOK_FILE="$HOOK_DIR/pre-commit"

mkdir -p "$HOOK_DIR"

cat > "$HOOK_FILE" << 'HOOK_EOF'
#!/bin/sh
# Pre-commit hook: block staging of secret files
# Prevents accidental commits of .env and *.pem files containing real credentials

FILES_TO_BLOCK=".env *.pem *.key *.key.txt"

STAGED=$(git diff --cached --name-only)

for pattern in $FILES_TO_BLOCK; do
    for file in $STAGED; do
        if echo "$file" | grep -qE "$pattern"; then
            echo "ERROR: Attempting to commit secret file: $file"
            echo "       This file contains real credentials and must not be committed."
            echo "       Remove it from staging: git reset HEAD $file"
            exit 1
        fi
    done
done

exit 0
HOOK_EOF

chmod +x "$HOOK_FILE"
echo "Pre-commit hook installed at $HOOK_FILE"
