#!/usr/bin/env python3
"""
scripts/sync_lag_alert.py — N3 Supabase-mirror lag watchdog.

Scheduled by the superbot-sync-lag workflow (every 10 min). Uses the same
thresholds as the in-trader sync.lag emission (emit.SYNC_LAG_MAX_*) so the
alert and the event agree.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # when run by n8n from elsewhere

from emit import SYNC_LAG_MAX_AGE_S, SYNC_LAG_MAX_ROWS  # noqa: E402
from local_db import get_local_db  # noqa: E402


def check(db=None) -> dict:
    """Alert when the local->Supabase mirror exceeds either lag threshold."""
    stats = (db or get_local_db()).get_sync_lag_stats()
    age_s = float(stats.get("oldest_unsynced_age_s", 0.0) or 0.0)
    rows = int(stats.get("unsynced_count", 0) or 0)
    return {
        "alert": age_s >= SYNC_LAG_MAX_AGE_S or rows >= SYNC_LAG_MAX_ROWS,
        "unsynced_count": rows,
        "oldest_unsynced_age_s": round(age_s, 1),
        "thresholds": {"age_s": SYNC_LAG_MAX_AGE_S, "rows": SYNC_LAG_MAX_ROWS},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", dest="as_json", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    print(json.dumps(check(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
