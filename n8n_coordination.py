#!/usr/bin/env python3
"""
n8n_coordination.py — Inter-agent coordination webhook handler for n8n.

Receives messages from OpenCode agents via n8n webhook and:
1. Writes them to MemPalace logstream (project/kalshi_superbot, room=delegation)
2. Routes responses between peer agents
3. Maintains a rolling task list for the kalshi_superbot project

Usage (called by n8n Code node):
    Input JSON body: {
        "to_agent": "termux:opencode:<project>",  # or "*" for broadcast
        "type": "task.request|task.reply|status|patch.ready",
        "body": "message text",
        "topic": "optional-lane",
        "correlation_id": "task_..."
    }
"""

import json
import os
import sys
from datetime import datetime, timezone

# Add mempalace to path so we can use its logstream CLI via subprocess
MEMPALACE_HOME = os.environ.get("MEMPALACE_HOME", "/data/data/com.termux/files/home/mempalace")


def log(msg):
    """Write to stderr so n8n captures it in logs."""
    print(msg, file=sys.stderr)


def main():
    # Read input from stdin (n8n passes webhook body via stdin)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log("ERROR: Invalid JSON input")
        print(json.dumps({"success": False, "error": "Invalid JSON"}))
        return

    body = payload.get("body", {}) if isinstance(payload.get("body"), dict) else {}
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
        "--stream", "project/kalshi_superbot",
        "--room", "delegation",
        "--topic", topic,
        "--from-agent", "termux:opencode:kalshi_superbot",
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


if __name__ == "__main__":
    main()
