#!/usr/bin/env python3
"""scripts/prune_control_tables.py — H9-H11 nightly retention prune.

Scheduled by superbot-jobs.timer (see scripts/scheduled_jobs.py). Deletes
rows older than the configured retention windows (config.RETENTION_*_DAYS)
from the unbounded control tables: event_outbox, alert_history, bus_inbox,
pending_send, latency_measurements, sync_quarantine. Prints a JSON summary;
alerts only on failure.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from local_db import get_local_db  # noqa: E402


def check(db=None) -> dict:
    db = db or get_local_db()
    try:
        counts = db.prune_control_tables(
            latency_keep_days=config.RETENTION_LATENCY_MEASUREMENTS_DAYS,
            pending_keep_days=config.RETENTION_PENDING_SEND_DAYS,
            inbox_keep_days=config.RETENTION_BUS_INBOX_DAYS,
            outbox_keep_days=config.RETENTION_EVENT_OUTBOX_DAYS,
            alert_keep_days=config.RETENTION_ALERT_HISTORY_DAYS,
            quarantine_keep_days=config.RETENTION_SYNC_QUARANTINE_DAYS,
        )
    except Exception as e:
        return {"alert": True, "reason": f"prune failed: {e}"}
    return {"alert": False, **counts}


def main(argv=None) -> int:
    print(json.dumps(check(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
