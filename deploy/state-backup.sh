#!/bin/sh
# Daily snapshot of the paper trading state + ledger on the VPS, keeping 7 days.
# Install (on the VPS): crontab -e  ->  15 3 * * * /opt/superbot/deploy/state-backup.sh
set -eu
cd /opt/superbot
d="$HOME/backups/state"
mkdir -p "$d"
stamp=$(date +%Y%m%d)
cp local_data/poly_us_state.json "$d/poly_us_state-$stamp.json"
cp local_data/poly_us_paper_ledger.jsonl "$d/poly_us_paper_ledger-$stamp.jsonl"
ls -1t "$d"/poly_us_state-*.json 2>/dev/null | tail -n +8 | xargs -r rm -f
ls -1t "$d"/poly_us_paper_ledger-*.jsonl 2>/dev/null | tail -n +8 | xargs -r rm -f
