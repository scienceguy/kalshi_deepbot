#!/usr/bin/env python3
"""N4 — enqueue a signed control command into the traders' local inbox.

Two modes:

1. n8n path (superbot-control webhook -> Verify Signature -> this script):
       scripts/send_command.py --stdin
   Reads the verify node's output {"body": {...}, "headers": {...}} from
   stdin, re-verifies the HMAC independently, and inserts the row. The
   trader drains control_inbox at the top of its next cycle.

2. Operator path on the VPS (the "manual" escape hatch):
       scripts/send_command.py --command pause_entries --by <agent-id> \
           [--reason "..."] [--payload-json '{"per_trade_pct": 0.04}']
   Signs with the shared secret and inserts directly.

Stdin body shape (mode 1): the command object itself, e.g.
    {"command": "pause_entries", "agent_id": "ubuntu:opencode:kalshi_deepbot",
     "reason": "vol spike", "payload": {...}}
Headers: x-superbot-timestamp, x-superbot-nonce, x-superbot-signature.

Exit codes: 0 accepted-and-enqueued, 2 refused (unsigned/expired/replayed/
unknown command), 1 usage error.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # when run by n8n from elsewhere

import n8n_webhook_auth  # noqa: E402
from local_db import get_local_db  # noqa: E402
from control import COMMANDS, allowed_senders  # noqa: E402


def _refuse(reason: str) -> int:
    print(json.dumps({"accepted": False, "reason": reason}), file=sys.stderr)
    return 2


def _enqueue(command: str, agent_id: str, nonce: str, ts_ms: int,
             payload: dict, signature: str) -> int:
    db = get_local_db()
    row_id = db.enqueue_control_command(
        command=command, agent_id=agent_id, nonce=nonce, ts=ts_ms,
        payload_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        signature=signature)
    if row_id is None:
        return _refuse("replayed nonce or DB insert failed")
    print(json.dumps({"accepted": True, "command": command,
                      "agent_id": agent_id, "row_id": row_id}))
    return 0


def _from_stdin() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _refuse("invalid JSON on stdin")
    headers = payload.get("headers") or {}
    low = {str(k).lower(): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}
    ts = low.get("x-superbot-timestamp")
    nonce = low.get("x-superbot-nonce")
    sig = low.get("x-superbot-signature")
    body = payload.get("body")
    if isinstance(body, (dict, list)):
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    else:
        body_str = str(body or "")
    # Extract identity from body if present
    try:
        cmd_for_identity = json.loads(body_str) if isinstance(body, str) else body
        agent_id_for_verify = str(cmd_for_identity.get("agent_id") or "n8n") if isinstance(cmd_for_identity, dict) else "n8n"
    except (json.JSONDecodeError, TypeError):
        agent_id_for_verify = "n8n"
    ok, reason = n8n_webhook_auth.verify_request(ts, nonce, body_str, sig, identity=agent_id_for_verify)
    if not ok:
        return _refuse(f"signature: {reason}")
    try:
        cmd = json.loads(body_str) if isinstance(body, str) else body
    except (json.JSONDecodeError, TypeError):
        return _refuse("body is not a JSON object")
    if not isinstance(cmd, dict):
        return _refuse("body must be a JSON object")
    command = str(cmd.get("command") or "").strip()
    if command not in COMMANDS:
        return _refuse(f"unknown command: {command!r}")
    agent_id = str(cmd.get("agent_id") or "n8n")
    if agent_id not in allowed_senders():
        return _refuse(f"sender not allowlisted: {agent_id!r}")
    payload_obj = cmd.get("payload") or {}
    if not isinstance(payload_obj, dict):
        return _refuse("payload must be a JSON object")
    # the command itself rides in the signed body; the DB row stores it too
    return _enqueue(command, agent_id, nonce, int(ts),
                    {"command": command, **payload_obj}, sig)


def _from_args(args) -> int:
    command = args.command.strip()
    if command not in COMMANDS:
        return _refuse(f"unknown command: {command!r}")
    payload = {}
    if args.payload_json:
        try:
            payload = json.loads(args.payload_json)
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
        except Exception as e:
            return _refuse(f"payload: {e}")
    if args.reason and command in ("pause_entries", "resume_entries"):
        payload.setdefault("reason", args.reason)
    agent_id = args.by or "operator"
    if agent_id not in allowed_senders():
        return _refuse(f"sender not allowlisted: {agent_id!r}")
    ts_ms = int(time.time() * 1000)
    nonce = _mk_nonce()
    body_str = json.dumps({"command": command, **payload},
                          separators=(",", ":"), ensure_ascii=False)
    # J10: Sign with per-identity secret
    sig = n8n_webhook_auth.sign_body(ts_ms, nonce, body_str, identity=agent_id)
    return _enqueue(command, agent_id, nonce, ts_ms,
                    {"command": command, **payload}, sig)


def _mk_nonce() -> str:
    import uuid
    return uuid.uuid4().hex


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stdin", action="store_true",
                    help="read the verify-node output from stdin (n8n path)")
    ap.add_argument("--command", help="command name (operator path)")
    ap.add_argument("--by", default="", help="issuing agent identity (operator path)")
    ap.add_argument("--reason", default="", help="human reason (operator path)")
    ap.add_argument("--payload-json", default="", help="JSON object of command params")
    args = ap.parse_args(argv)
    if args.stdin:
        return _from_stdin()
    if not args.command:
        ap.error("need --stdin or --command")
    return _from_args(args)


if __name__ == "__main__":
    sys.exit(main())
