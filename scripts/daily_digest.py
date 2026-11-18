#!/usr/bin/env python3
"""
scripts/daily_digest.py — N3 daily digest for the operator alert channel.

Scheduled by the superbot-daily-digest n8n workflow (daily 00:10 UTC).
Reads the last 24h from the local DB only (no network), prints one JSON
object; the workflow posts it to $SUPERBOT_ALERT_WEBHOOK_URL.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # when run by n8n from elsewhere

from local_db import get_local_db  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


def check(since_iso: str = "", db=None) -> dict:
    """Summarize rows created since `since_iso` (default: 24h ago)."""
    since = since_iso or (_now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    db = db or get_local_db()
    conn = db._get_conn()
    out = {"since_utc": since}
    try:
        out["signals"] = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE ts >= ?", (since,)).fetchone()[0]
        out["orders"] = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE created_at >= ?", (since,)).fetchone()[0]
        out["settlements"] = conn.execute(
            "SELECT COUNT(*) FROM pnl_ledger WHERE applied_at >= ?", (since,)).fetchone()[0]
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl_dollars), 0.0), "
            "COALESCE(SUM(fee_dollars), 0.0) FROM pnl_ledger WHERE applied_at >= ?",
            (since,)).fetchone()
        out["realized_pnl_dollars"] = round(float(row[0]), 4)
        out["fees_dollars"] = round(float(row[1]), 4)
        out["bot_state_keys"] = conn.execute(
            "SELECT COUNT(*) FROM bot_state").fetchone()[0]
    except Exception as e:
        return {"alert": True, "error": f"local DB read failed: {e}"}
    out["alert"] = True  # the digest itself is the notification
    out["digest_text"] = (
        f"SuperBot daily digest since {since}Z: {out['signals']} signals, "
        f"{out['orders']} orders, {out['settlements']} settlements, "
        f"P&L ${out['realized_pnl_dollars']:+.2f}, fees ${out['fees_dollars']:.2f}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="",
                    help="ISO timestamp 'YYYY-MM-DD HH:MM:SS' UTC (default: 24h ago)")
    a = ap.parse_args(argv)
    print(json.dumps(check(a.since), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
