#!/usr/bin/env python3
"""H11 — durable inbox for the coding agent.

Messages sent to the coding agent over the localhost bus are lost when the
agent is not running. bus_inbox is the durable fallback: messages are enqueued
in the local SQLite DB and drained here (list / drain) when the agent next
runs.

Usage (from the repo root):
    python scripts/bus_inbox.py list          # pending messages as JSON
    python scripts/bus_inbox.py list --table # pending messages as a table
    python scripts/bus_inbox.py ack <id>     # mark one message delivered
    python scripts/bus_inbox.py drain        # print all pending, then mark delivered
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # when run from elsewhere

from local_db import get_local_db  # noqa: E402


def _json(rows) -> None:
    print(json.dumps(rows, indent=2, ensure_ascii=False))


def _table(rows) -> None:
    if not rows:
        print("(inbox empty)")
        return
    cols = ("id", "message_id", "type", "from_agent", "correlation_id", "created_at")
    print("\t".join(cols) + "\tbody")
    for r in rows:
        line = "\t".join(str(r.get(c) or "") for c in cols)
        body = str(r.get("body") or "").replace("\n", " ")
        print(f"{line}\t{body[:80]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="print pending messages")
    p_list.add_argument("--table", action="store_true",
                        help="print a table instead of JSON")

    p_ack = sub.add_parser("ack", help="mark one message delivered")
    p_ack.add_argument("id", type=int, help="bus_inbox row id (from list/drain)")

    sub.add_parser("drain", help="print all pending messages, then mark them delivered")

    args = ap.parse_args(argv)
    db = get_local_db()

    if args.cmd == "list":
        rows = db.get_pending_bus_messages()
        if args.table:
            _table(rows)
        else:
            _json(rows)
        return 0
    if args.cmd == "ack":
        ok = db.mark_bus_message_delivered(args.id)
        print(json.dumps({"ok": bool(ok), "id": args.id}))
        return 0 if ok else 1
    if args.cmd == "drain":
        rows = db.get_pending_bus_messages()
        _json(rows)
        for r in rows:
            db.mark_bus_message_delivered(r["id"])
        return 0
    ap.error("unknown command")  # unreachable: subparsers are required


if __name__ == "__main__":
    sys.exit(main())
