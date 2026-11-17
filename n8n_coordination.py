#!/usr/bin/env python3
"""
n8n_coordination.py — Inter-agent coordination webhook handler for n8n.

Receives messages from OpenCode agents via the superbot-coordinate n8n
webhook and writes them to the MemPalace logstream (project/kalshi_deepbot,
room=delegation).

N1: the caller (the n8n workflow's Verify Signature Code node) is the
replay/freshness gate. This handler independently re-verifies the HMAC
signature over the body when the payload carries the signature material,
and refuses to run at all when it does not — a webhook that skips the
verify node must not be able to append to the shared logstream.

Expected stdin payload (produced by the Verify Signature node):
    {"body": <the message object or raw string>,
     "headers": {"x-superbot-timestamp": ..., "x-superbot-nonce": ...,
                 "x-superbot-signature": ...}}

A message body looks like:
    {"to_agent": "termux:opencode:<project>",  # or "*" for broadcast
     "type": "task.request|task.reply|status|patch.ready",
     "body": "message text",
     "topic": "optional-lane",
     "correlation_id": "task_..."}

Escape hatch: set INSECURE_COORDINATION_UNVERIFIED=1 to run without
signature material (local dev only; logs a warning).
"""

import json
import os
import sys
from datetime import datetime, timezone

# Add mempalace to path so we can use its logstream CLI via subprocess
MEMPALACE_HOME = os.environ.get("MEMPALACE_HOME", os.path.expanduser("~/mempalace"))

try:
    import n8n_webhook_auth
except Exception:  # pragma: no cover - import must not break the handler's refusal path
    n8n_webhook_auth = None


def log(msg):
    """Write to stderr so n8n captures it in logs."""
    print(msg, file=sys.stderr)


def _verify_payload(payload):
    """Re-verify the signed payload. Returns (ok: bool, reason: str).

    Replay protection is the workflow's job ($getWorkflowStaticData store
    in the Verify Signature node) — this handler is spawned per call, so a
    process-local nonce store would enforce nothing. It checks freshness
    and authenticity only, with both body canonicalizations the sender
    and the JS mirror can produce.
    """
    headers = payload.get("headers")
    low = {}
    if isinstance(headers, dict):
        low = {str(k).lower(): str(v) for k, v in headers.items()}
    ts = low.get("x-superbot-timestamp")
    nonce = low.get("x-superbot-nonce")
    sig = low.get("x-superbot-signature")
    if not (ts and nonce and sig):
        if os.environ.get("INSECURE_COORDINATION_UNVERIFIED") == "1":
            log("WARNING: INSECURE_COORDINATION_UNVERIFIED=1 — skipping signature checks")
            return True, "verification skipped (INSECURE_COORDINATION_UNVERIFIED=1)"
        return False, "no signature material in payload"
    if n8n_webhook_auth is None:
        return False, "n8n_webhook_auth unavailable"
    body = payload.get("body")
    candidates = []
    if isinstance(body, str):
        candidates.append(body)
    candidates.append(json.dumps(body, separators=(",", ":"), ensure_ascii=False))
    candidates.append(json.dumps(body, separators=(",", ":")))
    for cand in candidates:
        ok, reason = n8n_webhook_auth.verify_request(ts, nonce, cand, sig)
        if ok:
            return True, "ok"
    return False, "signature mismatch"


def _refuse(reason):
    log(f"REFUSED coordination message: {reason}")
    print(json.dumps({"success": False, "error": f"refused: {reason}"}))
    return 1


def main():
    # Read input from stdin (n8n passes the verify node's output via stdin)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _refuse("invalid JSON input")

    ok, reason = _verify_payload(payload)
    if not ok:
        return _refuse(reason)

    body = payload.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = {}
    if not isinstance(body, dict):
        body = {}

    to_agent = body.get("to_agent", "*")
    msg_type = body.get("type", "status")
    msg_body = body.get("body", "")
    topic = body.get("topic", "kalshi-superbot")
    correlation_id = body.get("correlation_id")

    log(f"Coordination: {msg_type} to {to_agent} (topic={topic})")

    # Write to MemPalace logstream via CLI
    cmd = [
        "python3", "-m", "mempalace", "logstream", "append",
        "--type", msg_type,
        "--stream", "project/kalshi_deepbot",
        "--room", "delegation",
        "--topic", topic,
        "--from-agent", "termux:opencode:kalshi_deepbot",
        "--to-agent", to_agent,
        "--body", msg_body,
    ]
    if correlation_id:
        cmd.extend(["--correlation-id", correlation_id])

    # Add metadata about the coordination
    metadata = {
        "source": "n8n_coordination",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n8n_webhook": "superbot-coordinate",
    }
    cmd.extend(["--metadata", json.dumps(metadata)])

    try:
        import subprocess
        result = subprocess.run(
            cmd,
            cwd=MEMPALACE_HOME,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            log("Coordination event written to MemPalace logstream")
            print(json.dumps({
                "success": True,
                "event_written": True,
                "to_agent": to_agent,
                "type": msg_type,
                "topic": topic,
            }))
        else:
            log(f"MemPalace logstream error: {result.stderr}")
            print(json.dumps({
                "success": False,
                "error": f"MemPalace logstream failed: {result.stderr[:200]}",
            }))
    except Exception as e:
        log(f"Coordination error: {e}")
        print(json.dumps({"success": False, "error": str(e)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
