"""Tests for inject_n8n_workflows.py — workflow construction only.

make_workflow() is pure (returns row tuples); no SQLite is touched here, so
these tests never modify the real n8n database.
"""
import json

import inject_n8n_workflows as inj


def _nodes(data):
    return json.loads(data["workflow_entity"][3])


def _connections(data):
    return json.loads(data["workflow_entity"][4])


def test_python_script_workflow_shape():
    data = inj.make_workflow(
        "wid1", "SuperBot Backtest", "superbot-backtest", "wh1",
        "SuperBot Backtest", "/tmp/backtest.py", "--quick")
    nodes = _nodes(data)
    types = [n["type"] for n in nodes]
    assert "n8n-nodes-base.webhook" in types
    assert "n8n-nodes-base.code" in types
    assert "n8n-nodes-base.respondToWebhook" in types
    conns = _connections(data)
    assert set(conns) == {"node_webhook", "node_code"}


def test_settlement_workflow_uses_http_request():
    data = inj.make_workflow(
        "wid2", "SuperBot Trade", "superbot-trade", "wh2", "SuperBot Trade",
        None, "", "settlement_processor",
        "https://example.supabase.co", "service-key")
    nodes = _nodes(data)
    types = [n["type"] for n in nodes]
    assert "n8n-nodes-base.httpRequest" in types
    assert "n8n-nodes-base.code" not in types
    # webhook path is recorded on the webhook row
    assert data["webhook_entity"][1] == "superbot-trade"


def test_rows_cover_all_tables():
    data = inj.make_workflow(
        "wid3", "SuperBot Monitor", "superbot-monitor", "wh3",
        "SuperBot Monitor", "/tmp/trader.py", "--once")
    assert set(data) == {
        "workflow_entity", "workflow_history", "published_version",
        "publish_history", "shared_workflow", "webhook_entity"}
