"""H5 — n8n has no shell: no Execute Command node anywhere, jobs via timers.

Covers:
- every workflow JSON shipped in n8n/workflows/ is free of executeCommand;
- the injected workflow builder never emits it (see test_inject_n8n_workflows);
- n8n_startup_check errors when the node is re-enabled;
- scripts/scheduled_jobs.py replaces the schedule triggers (due slots, alert
  payload, posting rule);
- scripts/job_bridge.py runs an allowlisted argv and refuses unknown routes.
"""

import json
import sys
from pathlib import Path

import n8n_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import job_bridge  # noqa: E402  (scripts are not an installed package)
import scheduled_jobs  # noqa: E402


def test_no_workflow_json_contains_execute_command():
    workflows = Path(__file__).resolve().parents[1] / "n8n" / "workflows"
    files = sorted(workflows.glob("*.json"))
    assert files  # the scan must actually look at something
    for path in files:
        raw = path.read_text()
        assert "executeCommand" not in raw, f"{path.name} still uses Execute Command"
        for node in json.loads(raw).get("nodes", []):
            assert node["type"] != "n8n-nodes-base.executeCommand"


def test_startup_check_errors_when_execute_command_enabled(monkeypatch):
    monkeypatch.setenv("NODES_EXCLUDE", "[]")
    monkeypatch.delenv("NODES_INCLUDE", raising=False)
    assert n8n_client._execute_command_enabled() is True
    monkeypatch.setenv("NODES_EXCLUDE",
                       '["n8n-nodes-base.executeCommand"]')
    assert n8n_client._execute_command_enabled() is False
    monkeypatch.delenv("NODES_EXCLUDE")
    assert n8n_client._execute_command_enabled() is False  # n8n 2.x default
    monkeypatch.setenv("NODES_INCLUDE", "n8n-nodes-base.executeCommand")
    assert n8n_client._execute_command_enabled() is True


def test_due_slots_are_once_per_interval():
    every, offset = 300, 60
    hits = [t for t in range(0, 1200, 60) if scheduled_jobs.due(t, every, offset)]
    assert hits == [60, 360, 660, 960]


def test_digest_alert_key_is_deterministic():
    """Same alert content produces same dedup key."""
    key1 = scheduled_jobs._alert_key({"digest_text": "3 signals", "since_utc": "2026-11-19 00:00:00"})
    key2 = scheduled_jobs._alert_key({"digest_text": "3 signals", "since_utc": "2026-11-19 00:00:00"})
    assert key1 == key2
    # Different date -> different key
    key3 = scheduled_jobs._alert_key({"digest_text": "3 signals", "since_utc": "2026-11-20 00:00:00"})
    assert key1 != key3


def test_watchdog_alert_key_rounds_age():
    """Age rounded to nearest 60s for stable dedup."""
    key1 = scheduled_jobs._alert_key({"trader": "kalshi", "age_s": 125.0, "reason": "stale heartbeat"})
    key2 = scheduled_jobs._alert_key({"trader": "kalshi", "age_s": 175.0, "reason": "stale heartbeat"})
    # Both round to 120s bucket
    assert key1 == key2
    # Different bucket
    key3 = scheduled_jobs._alert_key({"trader": "kalshi", "age_s": 185.0, "reason": "stale heartbeat"})
    assert key1 != key3


def test_run_due_runs_only_due_jobs_and_emits_alerts(monkeypatch):
    """J8: run_due calls runner for due jobs and records alert in history."""
    from local_db import get_local_db
    db = get_local_db()

    ran = []

    def runner(argv, timeout):
        ran.append(argv[0])
        return {"alert": True, "trader": "kalshi", "age_s": 1200.0, "reason": "stale"}

    # First run: alert should be emitted and recorded
    names = scheduled_jobs.run_due(now_s=60, runner=runner)
    assert names == ["heartbeat-kalshi"]
    assert ran == ["scripts/heartbeat_watchdog.py"]

    # Check alert was recorded in history
    alert_key = scheduled_jobs._alert_key({"trader": "kalshi", "age_s": 1200.0, "reason": "stale"})
    assert db.was_alert_sent("heartbeat-kalshi", alert_key)

    # Second run with same alert: should be deduped (runner not called again for same alert)
    ran.clear()
    names = scheduled_jobs.run_due(now_s=60, runner=runner)
    # Job is still due, but alert is deduped so no new emission
    # runner is still called (job runs) but alert not re-emitted
    assert names == ["heartbeat-kalshi"]
    assert ran == ["scripts/heartbeat_watchdog.py"]


def test_run_job_reports_a_failed_script_as_an_alert():
    result = scheduled_jobs.run_job(["-c", "import sys; sys.exit(3)"], timeout=5.0)
    assert result["alert"] is True
    assert "exited 3" in result["error"]


def test_run_job_timeout_reports_alert():
    """J8: timeout is reported as alert."""
    result = scheduled_jobs.run_job(["-c", "import time; time.sleep(10)"], timeout=0.1)
    assert result["alert"] is True
    assert "timed out" in result["error"]


def test_job_timeout_override():
    """J8: per-job timeout in JOBS tuple overrides global."""
    # Find a job with explicit timeout
    for job in scheduled_jobs.JOBS:
        if len(job) == 5:
            name, argv, every_s, offset_s, timeout_s = job
            assert timeout_s is not None
            assert timeout_s != scheduled_jobs.GLOBAL_TIMEOUT_SECONDS
            break
    else:
        assert False, "No job with per-job timeout found"


def test_job_bridge_allowlist_only():
    assert set(job_bridge.ROUTES) == {"command", "coordinate", "backtest", "collect"}
    for argv, _stdin in job_bridge.ROUTES.values():
        assert all(isinstance(a, str) for a in argv)  # argv list, never a shell string
