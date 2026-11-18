#!/usr/bin/env bash
# Periodic guard: recreate any n8n credential an agent has deleted.
# Idempotent and safe to run at any time. Scheduled by
# superbot-creds-guard.timer (deploy/); output goes to the journal.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
if [ ! -x "$PY" ]; then
    PY=python3
fi
exec "$PY" scripts/n8n_creds_sync.py
