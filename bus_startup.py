"""bus_startup.py — durable bus inbox wiring for the coding agent (H3–H5).

Run once at coding-agent session start:

1. Drain the durable bus_inbox (SQLite): every pending message addressed to
   the coding agent is processed through ``handler`` and marked ``delivered``
   only after the handler accepts it. A handler that raises leaves the row
   pending — nothing is lost on a crash mid-drain.
2. Sweep the MemPalace logstream inbox (``to_agent=CODING_AGENT``) for events
   written while the agent was down. Best-effort: a missing/unreadable palace
   degrades to an empty sweep, never a startup failure.
3. Arm the logstream watcher (``task.request`` / ``task.reply`` /
   ``patch.ready``) and emit one status event naming the filter and the
   cursor it armed from.

Usage:
    python3 bus_startup.py             # drain + sweep + arm watcher
    python3 bus_startup.py --no-arm    # drain + sweep only
"""

import argparse
import json
import logging
import shutil
import subprocess
import sys
from typing import Any, Callable, Optional

import agent_identity
from mempalace_bridge import (
    _LOGSTREAM_DB,
    _MEMPALACE_AVAILABLE,
    get_default_bridge,
)

if _MEMPALACE_AVAILABLE:
    from mempalace.logstream import Logstream

log = logging.getLogger(__name__)

WATCHER_TYPES = ("task.request", "task.reply", "patch.ready")
SWEEP_LIMIT = 50
DRAIN_LIMIT = 100


def _default_handler(message: dict) -> None:
    """Surface a durable bus message to the session log. Replace/extend by
    passing ``handler=`` to process_pending_bus_inbox/startup."""
    log.info("BUS inbox id=%s from=%s type=%s correlation=%s body=%.500s",
             message.get("message_id"), message.get("from_agent"),
             message.get("type"), message.get("correlation_id"),
             str(message.get("body")))


def process_pending_bus_inbox(db=None,
                              handler: Optional[Callable[[dict], Any]] = None,
                              limit: int = DRAIN_LIMIT) -> list:
    """Drain pending bus_inbox rows through ``handler``; mark delivered only
    on success. Returns the rows that were delivered (failures stay pending)."""
    if db is None:
        from local_db import get_local_db
        db = get_local_db()
    handler = handler or _default_handler
    delivered = []
    for row in db.get_pending_bus_messages(limit=limit):
        try:
            handler(row)
        except Exception as e:
            log.warning("bus_inbox id=%s handler failed, left pending: %s",
                        row.get("id"), e)
            continue
        if db.mark_bus_message_delivered(row["id"]):
            delivered.append(row)
        else:
            log.warning("bus_inbox id=%s processed but mark delivered failed",
                        row.get("id"))
    return delivered


def sweep_logstream_inbox(since_event_id: Optional[str] = None,
                          limit: int = SWEEP_LIMIT) -> dict:
    """Best-effort sweep of logstream events addressed to the coding agent.

    Returns {"events": [...], "cursor": latest_event_id or None}. Empty on
    any failure (palace missing, unreadable, mempalace not installed)."""
    if not _MEMPALACE_AVAILABLE:
        return {"events": [], "cursor": None, "skipped": "mempalace unavailable"}
    try:
        ls = Logstream(db_path=_LOGSTREAM_DB)
        events = ls.list_events(to_agent=agent_identity.CODING_AGENT,
                                since_event_id=since_event_id,
                                limit=limit, order="asc")
        cursor = ls.latest_event_id()
        return {"events": events, "cursor": cursor}
    except Exception as e:
        log.warning("logstream sweep failed (continuing startup): %s", e)
        return {"events": [], "cursor": None, "skipped": str(e)}


def watcher_command(cursor: Optional[str] = None) -> list:
    """The mandated watcher invocation for the coding agent."""
    cmd = ["mempalace", "logstream", "watch",
           "--agent", agent_identity.CODING_AGENT]
    for t in WATCHER_TYPES:
        cmd += ["--type", t]
    if cursor:
        cmd += ["--since-event-id", str(cursor)]
    cmd += ["--json"]
    return cmd


def arm_watcher(cursor: Optional[str] = None, bridge=None,
                spawn: bool = True) -> dict:
    """Arm the logstream watcher and announce filter + cursor.

    Starts the default bridge (in-process subscriber) and emits one status
    event. With ``spawn`` also launches the CLI watcher detached; pass
    ``spawn=False`` to only compute the command (tests, dry setups)."""
    bridge = bridge or get_default_bridge()
    bridge.start()  # idempotent
    announcement = {"filter": list(WATCHER_TYPES), "cursor": cursor or ""}
    bridge.status("watcher-armed", announcement)
    pid = None
    if spawn:
        cmd = watcher_command(cursor)
        if shutil.which(cmd[0]):
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
                pid = proc.pid
            except Exception as e:
                log.warning("could not spawn logstream watcher: %s", e)
        else:
            log.warning("mempalace CLI not on PATH — watcher not spawned")
    return {"filter": list(WATCHER_TYPES), "cursor": cursor, "pid": pid}


def startup(db=None, handler: Optional[Callable[[dict], Any]] = None,
            bridge=None, spawn_watcher: bool = True,
            limit: int = DRAIN_LIMIT) -> dict:
    """Full startup wiring: drain bus_inbox, sweep logstream, arm watcher."""
    delivered = process_pending_bus_inbox(db=db, handler=handler, limit=limit)
    sweep = sweep_logstream_inbox(limit=SWEEP_LIMIT)
    cursor = sweep.get("cursor")
    watcher = arm_watcher(cursor=cursor, bridge=bridge, spawn=spawn_watcher)
    summary = {
        "delivered": [{"id": r.get("id"), "message_id": r.get("message_id"),
                       "type": r.get("type"),
                       "correlation_id": r.get("correlation_id")}
                      for r in delivered],
        "logstream_events": sweep.get("events", []),
        "cursor": cursor,
        "watcher": watcher,
    }
    if sweep.get("skipped"):
        summary["sweep_skipped"] = sweep["skipped"]
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-arm", action="store_true",
                    help="drain and sweep only; do not arm the watcher")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    summary = startup(spawn_watcher=not args.no_arm)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
