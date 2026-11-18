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

B3: a bounded nonce store is kept in a sidecar file so replays across
handler invocations are caught even though the process is short-lived.

Expected stdin payload (produced by the Verify Signature node):
    {"body": <the message object or raw string>,
     "headers": {"x-superbot-timestamp": ..., "x-superbot-nonce": ...,
                 "x-superbot-signature": ...}}

A message body looks like:
    {"to_agent": "ubuntu:opencode:<project>",  # or "*" for broadcast
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
import fcntl
import agent_identity
from datetime import datetime, timezone
from collections import OrderedDict
from typing import Optional
from pathlib import Path
import time

# Add mempalace to path so we can use its logstream CLI via subprocess
MEMPALACE_HOME = os.environ.get("MEMPALACE_HOME", os.path.expanduser("~/mempalace"))

try:
    import n8n_webhook_auth
except Exception:  # pragma: no cover - import must not break the handler's refusal path
    n8n_webhook_auth = None


def log(msg):
    """Write to stderr so n8n captures it in logs."""
    print(msg, file=sys.stderr)


# B3: bounded nonce store persisted to a sidecar file so replays across
# handler invocations are caught even though the process is short-lived.
# Mirrors n8n_webhook_auth.NonceStore with file-backed TTL.
class _NonceStore:
    def __init__(self, ttl_ms: int = 120_000, max_entries: int = 4096):
        self._ttl_ms = ttl_ms
        self._max = max_entries
        self._path = Path(os.environ.get(
            "SUPERBOT_NONCE_STORE_FILE",
            os.path.expanduser("~/.secrets/coordination_nonces.json")))
        self._seen: "OrderedDict[str, int]" = OrderedDict()
        self._load()

    def _load(self):
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                now = int(time.time() * 1000)
                cutoff = now - self._ttl_ms
                for k, v in data.items():
                    if v >= cutoff:
                        self._seen[k] = v
        except Exception:
            pass

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Open r+ to avoid truncate-before-lock race; truncate after lock.
            with open(self._path, "a+") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.seek(0)
                f.truncate()
                json.dump(self._seen, f)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass

    def check_and_mark(self, nonce: str, now_ms: Optional[int] = None) -> bool:
        """True when the nonce is fresh; False if replayed or malformed."""
        if not nonce or len(nonce) > 256 or any(c not in "0123456789abcdefABCDEF" for c in nonce):
            return False
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        cutoff = now_ms - self._ttl_ms
        while self._seen:
            oldest_ts = next(iter(self._seen.values()))
            if oldest_ts >= cutoff:
                break
            self._seen.popitem(last=False)
        if nonce in self._seen:
            return False
        self._seen[nonce] = now_ms
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)
        self._save()
        return True


_NONCE_STORE = _NonceStore()


def _clear_nonce_store_for_test():
    """Clear the nonce store and its sidecar file (test helper)."""
    _NONCE_STORE._seen.clear()
    try:
        _NONCE_STORE._path.unlink(missing_ok=True)
    except Exception:
        pass


def _verify_payload(payload):
    """Re-verify the signed payload. Returns (ok: bool, reason: str).

    Replay protection is the workflow's job ($getWorkflowStaticData store
    in the Verify Signature node) — this handler is spawned per call, so a
    process-local nonce store would enforce nothing. B3: a sidecar file
    persists nonces across invocations so replays are caught.
    It checks freshness and authenticity only, with both body canonicalizations
    the sender and the JS mirror can produce.
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
    # B3: nonce check before signature so a flood of unsigned junk cannot
    # evict valid nonces from the sidecar store.
    now_ms = int(time.time() * 1000)
    if not _NONCE_STORE.check_and_mark(nonce, now_ms):
        return False, "replayed or malformed nonce"
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


def _post_to_bus(to_agent, msg_type, msg_body, topic, correlation_id):
    """C1: push the same message to the target's localhost receiver.

    Advisory only — never fails the webhook. The logstream append is the
    durable record; if the bus is down or refuses, the target's fallback
    poll still delivers. A known-stale peer is refused; an unknown peer is
    attempted anyway (the receiver's TTL will drop a late message).
    """
    try:
        import agent_bus
        if to_agent == "*":
            return {"delivered": False, "reason": "broadcast"}
        status = agent_bus.peer_status(to_agent)
        if status == "stale":
            return {"delivered": False, "reason": "stale peer refused"}
        env = agent_bus.envelope(to_agent=to_agent, body=msg_body, type=msg_type,
                                 topic=topic, correlation_id=correlation_id)
        result = agent_bus.post_envelope(env, timeout=1.0)
        return {"delivered": bool(result.get("ok")), **result}
    except Exception as e:
        return {"delivered": False, "reason": f"bus error: {e}"}


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
        "--from-agent", agent_identity.CODING_AGENT,
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
            bus = _post_to_bus(to_agent, msg_type, msg_body, topic, correlation_id)
            print(json.dumps({
                "success": True,
                "event_written": True,
                "to_agent": to_agent,
                "type": msg_type,
                "topic": topic,
                "bus": bus,
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
