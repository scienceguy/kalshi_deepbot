#!/usr/bin/env bash
# Continuous retrain of the torch fair-value model (paper-only artifact).
#
# Scheduled by superbot-retrain.timer (deploy/), every 6 hours. Writes only the
# gitignored data/ml_model.pt and appends to local_data/retrain_ml.log. It
# cannot place orders: trader.py loads the model only with --ml-model.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
if [ ! -x "$PY" ]; then
    PY=python3
fi
LOG="${SUPERBOT_RETRAIN_LOG:-$PWD/local_data/retrain_ml.log}"
{
    echo "=== $(date -u +%FT%TZ) retrain start ==="
    "$PY" scripts/train_ml.py --series KXBTCD --events 6 --out data/ml_model.pt
} >>"$LOG" 2>&1
