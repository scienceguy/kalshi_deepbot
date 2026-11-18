#!/usr/bin/env python3
"""H5 — the jobs n8n used to schedule, run from a systemd timer instead.

n8n's schedule triggers ran these through the Execute Command node, which
required re-enabling that node for the whole instance (arbitrary shell for
anyone with editor/API access). They now run here, from
superbot-jobs.timer (once a minute), and post the same alert the workflow's
Parse/Alert?/Post nodes produced.

Each job is due when ``(now - offset_s) % every_s < 60``: stateless, so a
missed tick costs one interval, never a replay. A job that returns
``alert: true`` is posted to ``SUPERBOT_ALERT_WEBHOOK_URL`` (the digest sets
the flag itself, so every job uses one rule).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]

# name, argv, every_s, offset_s
JOBS = (
    ("daily-digest", ["scripts/daily_digest.py"], 86400, 600),
    ("sync-lag", ["scripts/sync_lag_alert.py"], 600, 120),
    ("heartbeat-kalshi", ["scripts/heartbeat_watchdog.py", "--source", "kalshi",
                          "--max-age-s", "1200"], 300, 60),
    ("heartbeat-poly", ["scripts/heartbeat_watchdog.py", "--source", "poly_us",
                        "--max-age-s", "1200"], 300, 240),
    ("position-watchdog", ["scripts/position_watchdog.py", "--max-age-s", "7200"],
     3600, 840),
)

TIMEOUT_SECONDS = float(os.environ.get("SUPERBOT_JOB_TIMEOUT_SECONDS", "300"))


def due(now_s: float, every_s: int, offset_s: int) -> bool:
    """True when this job's slot falls inside the current minute."""
    return (int(now_s) - offset_s) % every_s < 60


def run_job(argv: list, timeout: float = TIMEOUT_SECONDS) -> dict:
    """Run one job and parse its JSON stdout. Never raises."""
    try:
        proc = subprocess.run(
            [sys.executable, *argv], cwd=str(BASE_DIR), capture_output=True,
            timeout=timeout, env={**os.environ, "PYTHONPATH": str(BASE_DIR)})
    except Exception as e:
        return {"alert": True, "error": f"{argv[0]} failed to start: {e}"}
    out = proc.stdout.decode(errors="replace").strip()
    if proc.returncode != 0 and not out:
        return {"alert": True, "error": f"{argv[0]} exited {proc.returncode}",
                "stderr": proc.stderr.decode(errors="replace")[-800:]}
    try:
        parsed = json.loads(out)
    except ValueError:
        return {"alert": True, "error": f"{argv[0]} printed non-JSON",
                "stdout": out[-800:]}
    return parsed if isinstance(parsed, dict) else {"alert": True, "result": parsed}


def alert_payload(name: str, result: dict) -> dict:
    """The {subject, text, ...} shape the workflow's Parse node built."""
    if name == "daily-digest":
        subject = "[SuperBot] daily digest"
        text = result.get("digest_text") or json.dumps(result)
    else:
        state = "ALERT" if result.get("alert") else "ok"
        subject = f"[SuperBot] {state}: {result.get('trader') or result.get('reason') or name}"
        text = json.dumps(result)[:800]
    return {"subject": subject, "text": text, **result}


def post_alert(payload: dict, url: str = "", timeout: float = 5.0) -> bool:
    """POST to SUPERBOT_ALERT_WEBHOOK_URL. False when unset/placeholder."""
    url = url or os.environ.get("SUPERBOT_ALERT_WEBHOOK_URL", "")
    if not url or ":9/" in url:  # n8n's placeholder when the env var is unset
        return False
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"alert post failed: {e}", file=sys.stderr)
        return False


def run_due(now_s: float = 0.0, runner=run_job, poster=post_alert) -> list:
    """Run every due job, post its alert. Returns the job names run."""
    ran = []
    now_s = now_s or time.time()
    for name, argv, every_s, offset_s in JOBS:
        if not due(now_s, every_s, offset_s):
            continue
        ran.append(name)
        result = runner(argv)
        if result.get("alert"):
            poster(alert_payload(name, result))
    return ran


def main(argv=None) -> int:
    ran = run_due()
    print(json.dumps({"ran": ran}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
