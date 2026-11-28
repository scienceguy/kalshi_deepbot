#!/usr/bin/env python3
"""J8 — the jobs n8n used to schedule, run from a systemd timer instead.

n8n's schedule triggers ran these through the Execute Command node, which
required re-enabling that node for the whole instance (arbitrary shell for
anyone with editor/API access). They now run here, from
superbot-jobs.timer (once a minute), and emit alerts through the durable
outbox (local_db.event_outbox + n8n_client sender threads).

Each job is due when ``(now - offset_s) % every_s < 60``: stateless, so a
missed tick costs one interval, never a replay. A job that returns
``alert: true`` is emitted as a critical/normal event (never telemetry) and
de-duplicated via local_db.alert_history so the operator is not spammed.

Per-job timeouts are configurable via SUPERBOT_JOB_TIMEOUT_SECONDS (default
300s) or per-job override in JOBS tuple (5th element).
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE_DIR = Path(__file__).resolve().parents[1]

# name, argv, every_s, offset_s, timeout_s (optional, None = global default)
# timeout_s overrides SUPERBOT_JOB_TIMEOUT_SECONDS for that job
JOBS = (
    ("daily-digest", ["scripts/daily_digest.py"], 86400, 600, 600),
    ("sync-lag", ["scripts/sync_lag_alert.py"], 600, 120, 120),
    ("heartbeat-kalshi", ["scripts/heartbeat_watchdog.py", "--source", "kalshi",
                          "--max-age-s", "1200"], 300, 60, 120),
    ("heartbeat-poly", ["scripts/heartbeat_watchdog.py", "--source", "poly_us",
                        "--max-age-s", "1200"], 300, 240, 120),
    ("position-watchdog", ["scripts/position_watchdog.py", "--max-age-s", "7200"],
     3600, 840, 180),
    ("db-prune", ["scripts/prune_control_tables.py"], 86400, 300, 120),
)

GLOBAL_TIMEOUT_SECONDS = float(os.environ.get("SUPERBOT_JOB_TIMEOUT_SECONDS", "300"))
STDOUT_TAIL_BYTES = 2000
STDERR_TAIL_BYTES = 1000


def _read_tail(stream, tail: bytearray, limit: int) -> None:
    """Drain one child stream while retaining only its final bytes."""
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            tail.extend(chunk)
            if len(tail) > limit:
                del tail[:-limit]
    finally:
        stream.close()


def _run_bounded(argv, timeout: float):
    """Run a child without buffering its total stdout or stderr."""
    proc = subprocess.Popen(
        argv, cwd=str(BASE_DIR), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(BASE_DIR)},
        start_new_session=True)
    stdout = bytearray()
    stderr = bytearray()
    readers = [
        threading.Thread(target=_read_tail, args=(proc.stdout, stdout, STDOUT_TAIL_BYTES)),
        threading.Thread(target=_read_tail, args=(proc.stderr, stderr, STDERR_TAIL_BYTES)),
    ]
    for reader in readers:
        reader.daemon = True
        reader.start()
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        raise
    finally:
        for reader in readers:
            reader.join(timeout=1.0)
    return returncode, bytes(stdout), bytes(stderr)

# Alert de-dup window: don't re-send same alert within this many seconds
ALERT_DEDUP_WINDOW_S = float(os.environ.get("SUPERBOT_ALERT_DEDUP_WINDOW_S", "3600"))


def due(now_s: float, every_s: int, offset_s: int) -> bool:
    """True when this job's slot falls inside the current minute."""
    return (int(now_s) - offset_s) % every_s < 60


def run_job(argv: list, timeout: float) -> dict:
    """Run one job and parse its JSON stdout. Never raises."""
    try:
        returncode, stdout, stderr = _run_bounded(
            [sys.executable, *argv], timeout)
    except subprocess.TimeoutExpired:
        return {"alert": True, "error": f"{argv[0]} timed out after {timeout}s"}
    except Exception as e:
        return {"alert": True, "error": f"{argv[0]} failed to start: {e}"}
    out = stdout.decode(errors="replace").strip()
    if returncode != 0 and not out:
        return {"alert": True, "error": f"{argv[0]} exited {returncode}",
                "stdout": out[-800:],
                "stderr": stderr.decode(errors="replace")[-800:]}
    try:
        parsed = json.loads(out)
    except ValueError:
        return {"alert": True, "error": f"{argv[0]} printed non-JSON",
                "stdout": out[-800:]}
    if returncode != 0:
        result = parsed if isinstance(parsed, dict) else {"result": parsed}
        return {**result, "alert": True, "error": f"{argv[0]} exited {returncode}",
                "stdout": out[-800:],
                "stderr": stderr.decode(errors="replace")[-800:]}
    return parsed if isinstance(parsed, dict) else {"alert": True, "result": parsed}


def _alert_key(result: dict) -> str:
    """Deterministic key for de-duplication from alert content."""
    # Use relevant fields that define the alert's identity
    key_parts = []
    if "trader" in result:
        key_parts.append(f"trader:{result['trader']}")
    if "reason" in result:
        key_parts.append(f"reason:{result['reason']}")
    if "age_s" in result and result["age_s"] is not None:
        # Round age to nearest 60s for dedup stability
        key_parts.append(f"age:{int(result['age_s'] // 60) * 60}")
    if "unsynced_count" in result:
        key_parts.append(f"unsynced:{result['unsynced_count']}")
    if "unsettled_count" in result:
        key_parts.append(f"unsettled:{result['unsettled_count']}")
    if "digest_text" in result:
        # For digest, use date prefix
        key_parts.append(f"digest:{result.get('since_utc', '')[:10]}")
    if not key_parts:
        key_parts.append("generic")
    raw = "|".join(key_parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _alert_type_from_job(name: str, result: dict) -> str:
    """Map job name to alert type for tracking."""
    if name == "daily-digest":
        return "digest"
    if name.startswith("heartbeat"):
        return "heartbeat"
    if name == "sync-lag":
        return "sync_lag"
    if name == "position-watchdog":
        return "position"
    if name == "db-prune":
        return "prune"
    return "unknown"


def run_due(now_s: float = 0.0, runner=run_job) -> list:
    """Run every due job, emit alerts via durable outbox. Returns the job names run."""
    import config
    from local_db import get_local_db
    from emit import emit_event

    ran = []
    now_s = now_s or time.time()
    db = get_local_db()
    mode = "dry" if config.DRY_RUN else "live"

    for job in JOBS:
        # Unpack with optional timeout
        if len(job) == 5:
            name, argv, every_s, offset_s, timeout_s = job
        else:
            name, argv, every_s, offset_s = job
            timeout_s = None

        if not due(now_s, every_s, offset_s):
            continue

        ran.append(name)
        timeout = timeout_s if timeout_s is not None else GLOBAL_TIMEOUT_SECONDS
        result = runner(argv, timeout=timeout)

        if not result.get("alert"):
            continue

        # De-duplication check - record FIRST to claim ownership, then emit
        alert_key = _alert_key(result)
        alert_type = _alert_type_from_job(name, result)
        
        # Try to record - if already sent (returns False), skip
        # Use a small summary payload, not the full result
        summary_payload = _alert_summary(name, result, alert_type)
        if not db.record_alert_sent(name, alert_key, alert_type, summary_payload):
            # Already sent recently — skip
            continue

        # Build event payload
        if name == "daily-digest":
            subject = "[SuperBot] daily digest"
            text = result.get("digest_text") or json.dumps(result)
            event_type = "daily.summary"
            severity = "info"
        else:
            state = "ALERT" if result.get("alert") else "ok"
            subject = f"[SuperBot] {state}: {result.get('trader') or result.get('reason') or name}"
            text = json.dumps(summary_payload)[:800]
            event_type = "operational.alert"  # normal lane, goes to superbot-trade
            severity = "warning"

        payload = {
            "subject": subject,
            "text": text,
            "job_name": name,
            "alert_type": alert_type,
            **summary_payload,
        }

        # Emit through durable outbox (N2/N3)
        # The key ensures deterministic event_id for replay safety
        emit_event(
            source="scheduled_jobs",
            event_type=event_type,
            payload=payload,
            key=f"alert:{name}:{alert_key}",
            severity=severity,
            mode=mode,
        )

    return ran


def _alert_summary(name: str, result: dict, alert_type: str) -> dict:
    """Extract small summary for alert_history and event payload."""
    summary = {"alert_type": alert_type}
    if name == "daily-digest":
        summary["since_utc"] = result.get("since_utc", "")
        summary["markets"] = result.get("markets", 0)
        summary["fills"] = result.get("fills", 0)
        summary["pnl"] = result.get("pnl", 0.0)
    elif name.startswith("heartbeat"):
        summary["trader"] = result.get("trader", "")
        summary["reason"] = result.get("reason", "")
        summary["age_s"] = result.get("age_s", 0.0)
    elif name == "sync-lag":
        summary["trader"] = result.get("trader", "")
        summary["reason"] = result.get("reason", "")
        summary["unsynced_count"] = result.get("unsynced_count", 0)
        summary["lag_s"] = result.get("lag_s", 0.0)
    elif name == "position-watchdog":
        summary["trader"] = result.get("trader", "")
        summary["reason"] = result.get("reason", "")
        summary["unsettled_count"] = result.get("unsettled_count", 0)
        summary["age_s"] = result.get("age_s", 0.0)
    return summary


def main(argv=None) -> int:
    ran = run_due()
    print(json.dumps({"ran": ran}))
    return 0


if __name__ == "__main__":
    sys.exit(main())