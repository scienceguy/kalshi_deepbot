"""Tests for inject_n8n_workflows.py — workflow JSON construction only.

make_workflow() is pure; no n8n server is touched here. The installer's
end-to-end behaviour (idempotent create/update, headerAuth on every webhook,
refusal without a secret, no SQLite anywhere) lives in
test_n1_webhook_auth.py against a fake REST server.
"""
import json

import inject_n8n_workflows as inj


def test_python_script_workflow_shape():
    wf = inj.make_workflow(
        "SuperBot Backtest", "superbot-backtest", "SuperBot Backtest",
        "python_script", python_script="/tmp/backtest.py", python_args="--quick",
        credential_id="cred-1")
    types = [n["type"] for n in wf["nodes"]]
    assert "n8n-nodes-base.webhook" in types
    assert "n8n-nodes-base.executeCommand" in types
    assert "n8n-nodes-base.code" not in types
    assert "n8n-nodes-base.respondToWebhook" in types
    # the command must invoke the script with its flags
    cmd = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.executeCommand")
    assert "/tmp/backtest.py --quick" in cmd["parameters"]["command"]
    assert "PYTHONPATH=" in cmd["parameters"]["command"]
    conns = wf["connections"]
    assert set(conns) == {"SuperBot Backtest", "Execute Python"}
    # targets must be node NAMES, not ids (n8n resolves by name)
    assert conns["SuperBot Backtest"]["main"][0][0]["node"] == "Execute Python"
    assert conns["Execute Python"]["main"][0][0]["node"] == "Respond to Webhook"
    # the webhook node carries the headerAuth credential and the path
    webhook = wf["nodes"][0]
    assert webhook["parameters"]["path"] == "superbot-backtest"
    assert webhook["parameters"]["authentication"] == "headerAuth"
    assert webhook["credentials"]["httpHeaderAuth"]["id"] == "cred-1"


def test_settlement_workflow_uses_http_request():
    wf = inj.make_workflow(
        "SuperBot Trade", "superbot-trade", "SuperBot Trade",
        "settlement_processor", credential_id="cred-1",
        supabase_url="https://example.supabase.co", supabase_key="service-key")
    types = [n["type"] for n in wf["nodes"]]
    assert "n8n-nodes-base.httpRequest" in types
    assert "n8n-nodes-base.code" not in types
    assert wf["nodes"][0]["parameters"]["path"] == "superbot-trade"


def test_monitor_workflow_is_ack_only_no_subprocess():
    # A monitor ping must never spawn trader.py — that would run a second
    # cycle able to place orders once DRY_RUN is false.
    wf = inj.make_workflow(
        "SuperBot Monitor", "superbot-monitor", "SuperBot Monitor",
        "ack_only", credential_id="cred-1")
    types = [n["type"] for n in wf["nodes"]]
    assert "n8n-nodes-base.executeCommand" not in types
    assert "n8n-nodes-base.webhook" in types
    assert "n8n-nodes-base.respondToWebhook" in types


def test_coordinate_workflow_verifies_before_any_side_effect():
    wf = inj.make_workflow(
        "SuperBot Coordinate", "superbot-coordinate", "SuperBot Coordinate",
        "signed_command", python_script="/opt/superbot/n8n_coordination.py",
        python_args="", credential_id="cred-1")
    names = [n["name"] for n in wf["nodes"]]
    assert names == ["SuperBot Coordinate", "Verify Signature",
                     "Execute Python", "Respond to Webhook"]
    verify = wf["nodes"][1]
    assert verify["type"] == "n8n-nodes-base.code"
    assert "createHmac" in verify["parameters"]["jsCode"]
    # no path from the webhook to the subprocess that skips the HMAC check
    assert wf["connections"]["SuperBot Coordinate"]["main"][0][0]["node"] == "Verify Signature"
    assert wf["connections"]["Verify Signature"]["main"][0][0]["node"] == "Execute Python"


def test_every_configured_workflow_builds():
    for spec in inj.WORKFLOWS:
        kwargs = {"workflow_type": spec.get("workflow_type", "python_script")}
        if spec.get("script"):
            kwargs["python_script"] = spec["script"]
            kwargs["python_args"] = spec.get("args", "")
        if spec.get("workflow_type") == "settlement_processor":
            kwargs["supabase_url"] = spec.get("supabase_url", "")
            kwargs["supabase_key"] = spec.get("supabase_key", "")
        wf = inj.make_workflow(
            spec["name"], spec["webhook_path"], spec["node_name"],
            credential_id="cred-1", **kwargs)
        assert wf["nodes"][0]["parameters"]["path"] == spec["webhook_path"]
        # every webhook node is headerAuth-gated with the shared credential
        for node in wf["nodes"]:
            if node["type"] == "n8n-nodes-base.webhook":
                assert node["parameters"]["authentication"] == "headerAuth"


def test_workflow_payload_json_round_trips():
    wf = inj.make_workflow(
        "SuperBot Monitor", "superbot-monitor", "SuperBot Monitor",
        "ack_only", credential_id="cred-1")
    json.dumps(wf)  # REST installer POSTs JSON; it must serialize
