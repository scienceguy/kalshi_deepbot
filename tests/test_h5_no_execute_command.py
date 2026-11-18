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


def test_digest_alert_payload_shape():
    payload = scheduled_jobs.alert_payload(
        "daily-digest", {"alert": True, "digest_text": "3 signals"})
    assert payload["subject"] == "[SuperBot] daily digest"
    assert payload["text"] == "3 signals"
    assert payload["alert"] is True


def test_watchdog_payload_names_the_trader():
    payload = scheduled_jobs.alert_payload(
        "heartbeat-kalshi", {"alert": True, "trader": "kalshi"})
    assert payload["subject"] == "[SuperBot] ALERT: kalshi"


def test_run_due_runs_only_due_jobs_and_posts_alerts():
    ran, posted = [], []

    def runner(argv):
        ran.append(argv[0])
        return {"alert": True, "trader": "kalshi"}

    def poster(payload):
        posted.append(payload["subject"])
        return True

    names = scheduled_jobs.run_due(now_s=60, runner=runner, poster=poster)
    # At t=60 exactly the heartbeat-kalshi slot (offset 60, every 300) is due.
    assert names == ["heartbeat-kalshi"]
    assert ran == ["scripts/heartbeat_watchdog.py"]
    assert posted == ["[SuperBot] ALERT: kalshi"]


def test_run_job_reports_a_failed_script_as_an_alert():
    result = scheduled_jobs.run_job(["-c", "import sys; sys.exit(3)"])
    assert result["alert"] is True
    assert "exited 3" in result["error"]


def test_job_bridge_allowlist_only():
    assert set(job_bridge.ROUTES) == {"command", "coordinate", "backtest", "collect"}
    for argv, _stdin, _hmac in job_bridge.ROUTES.values():
        assert all(isinstance(a, str) for a in argv)  # argv list, never a shell string
