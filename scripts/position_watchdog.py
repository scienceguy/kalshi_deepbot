#!/usr/bin/env python3
"""
scripts/position_watchdog.py — N3 unsettled-position watchdog.

Scheduled by the superbot-position-watchdog workflow (hourly). Alerts when a
local orders row is still not settled (no outcome booked) and older than the
threshold; catches rows the settlement loop could not reach.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/opt/superbot")  # when run by n8n from elsewhere

from local_db import get_local_db  # noqa: E402


def check(max_age_s: float = 7200.0, db=None) -> dict:
    """Alert on unsettled orders older than `max_age_s` (default 2h)."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_s)
    cutoff_s = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    conn = (db or get_local_db())._get_conn()
    try:
        rows = conn.execute(
            "SELECT order_id, ticker, status, created_at FROM orders "
            "WHERE settled_at IS NULL AND outcome_won IS NULL "
            "AND created_at < ? ORDER BY created_at ASC LIMIT 20",
            (cutoff_s,)).fetchall()
    except Exception as e:
        return {"alert": True, "error": f"local DB read failed: {e}"}
    return {
        "alert": bool(rows),
        "oldest_unsettled": [
            {"order_id": r[0], "ticker": r[1], "status": r[2], "created_at": r[3]}
            for r in rows[:5]],
        "unsettled_count": len(rows),
        "max_age_s": max_age_s,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-age-s", type=float, default=7200.0)
    a = ap.parse_args(argv)
    print(json.dumps(check(a.max_age_s), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
