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
