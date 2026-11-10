#!/data/data/com.termux/files/usr/bin/bash
# Scheduled continuous retrain of the torch fair-value model (paper-only).
#
# termux-job-scheduler entry point. Re-register on a fresh clone with:
#   termux-job-scheduler --job-id 7002 --period-ms 21600000 --network any \
#       --persisted --script "$PWD/scripts/retrain_ml.sh"
# Remove with:  termux-job-scheduler --cancel 7002
#
# Writes only the gitignored artifact data/ml_model.pt and appends a log under
# the approved scratch dir. It cannot place orders and is not wired into
# trader.py (the model loads only with --ml-model).
set -e
cd "$(dirname "$0")/.."
LOG="${HOME}/tmp/opencode/retrain_ml.log"
{
  echo "=== $(date -u +%FT%TZ) retrain start ==="
  /data/data/com.termux/files/usr/bin/python3 scripts/train_ml.py \
      --series KXBTCD --events 6 --out data/ml_model.pt
} >>"$LOG" 2>&1
