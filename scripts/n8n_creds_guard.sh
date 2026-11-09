#!/data/data/com.termux/files/usr/bin/bash
# Periodic guard: recreate any n8n credentials an agent has deleted.
# Registered with termux-job-scheduler by scripts/install-n8n-cred-guard.sh.
# Safe to run anytime; it is idempotent.
LOG=/data/data/com.termux/files/home/tmp/opencode/n8n_creds_guard.log
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
{
  echo "--- $TS ---"
  /data/data/com.termux/files/usr/bin/python3 \
    /data/data/com.termux/files/home/kalshi_deepbot/scripts/n8n_creds_sync.py
} >> "$LOG" 2>&1
# keep the log small
tail -n 200 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
