#!/bin/sh
# Daily snapshot of the paper trading state + ledger on the VPS, keeping 7 days,
# plus an append-only equity history line for milestone tracking.
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

# Equity history: one JSON line per run, appended (never truncated).
.venv/bin/python - "$d/equity-history.jsonl" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path

state = json.loads(Path("local_data/poly_us_state.json").read_text())
mtm = sum(p.get("mtm_value", p.get("cost", 0.0)) for p in state.get("positions", []))
row = {
    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "equity": round(state.get("cash", 0.0) + mtm, 4),
    "cash": round(state.get("cash", 0.0), 4),
    "mtm": round(mtm, 4),
    "realized_pnl": state.get("realized_pnl", 0.0),
    "lifetime_realized_pnl": state.get("lifetime_realized_pnl", 0.0),
    "trades_count": state.get("trades_count", 0),
    "open_positions": len(state.get("positions", [])),
    "peak_equity": state.get("peak_equity"),
    "max_drawdown": state.get("max_drawdown"),
}
with open(sys.argv[1], "a") as fh:
    fh.write(json.dumps(row) + "\n")
print(json.dumps(row))
PY

# SQLite online backups (D4): Connection.backup is WAL-safe; cp of the main
# file can capture a torn image. Keeps 7 generations per DB. Restore drill:
# docs/vps-runbook.md ("Restore drill (SQLite DBs)").
.venv/bin/python - "$d/sqlite" <<'PY'
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

dest = Path(sys.argv[1])
dest.mkdir(parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
KEEP = 7
for db in sorted(Path("local_data").glob("superbot_local*.db")):
    target = dest / f"{db.name}.{stamp}.bak"
    src = sqlite3.connect(str(db), timeout=30)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)  # page-by-page, consistent under concurrent writers
        finally:
            dst.close()
    finally:
        src.close()
    print(f"backed up {db} -> {target}")
    for old in sorted(dest.glob(f"{db.name}.*.bak"), reverse=True)[KEEP:]:
        old.unlink()
PY
