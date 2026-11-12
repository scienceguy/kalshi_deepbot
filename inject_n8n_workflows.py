#!/usr/bin/env python3
"""
Inject n8n workflow definitions for SuperBot integration.

Creates n8n workflows that act as tools:
1. superbot-backtest - runs parameter sweep backtest
2. superbot-collect - collects market data for backtesting
3. superbot-trade - executes trades via SuperBot
4. superbot-monitor - checks positions and status

Each workflow uses:
- Webhook trigger node (receives HTTP POST)
- Execute Command node (runs a Python script)
- Respond to Webhook node (returns results)

Injects directly into n8n SQLite database.
"""

import os
import shlex
import sqlite3
import sys
import json
import uuid
from datetime import datetime, timezone

DB_PATH = os.environ.get("N8N_DB_PATH", "/data/data/com.termux/files/home/.n8n/database.sqlite")
PROJECT_ID = os.environ.get("N8N_PROJECT_ID", "Tnj4bqvQK0YrKDi3")
USER_ID = os.environ.get("N8N_USER_ID", "8479b7c4-cebe-4ef5-807e-9cb8e6f2f773")

# Scripts the workflows execute, resolved relative to this file so the
# injector is not tied to one checkout path.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

now = datetime.now(timezone.utc).isoformat() + "Z"

def new_id():
    return uuid.uuid4().hex

def make_command(script, args):
    """Shell command for the Execute Command node.

    n8n 2.x blocks `require('child_process')` in Code nodes, so the script
    runs through n8n-nodes-base.executeCommand instead. None of the SuperBot
    scripts read stdin; each one is invoked for its side effect and CLI flags.
    """
    return shlex.join(["env", f"PYTHONPATH={BASE_DIR}", sys.executable,
                       script] + shlex.split(args))


def make_workflow(workflow_id, name, webhook_path, webhook_id, node_name, python_script=None, python_args="",
                  workflow_type="python_script", supabase_url="", supabase_key=""):
    """Create a complete n8n workflow definition."""
    
    version_id = new_id()
    
    # Build the webhook node (common to all workflow types)
    webhook_node = {
        "id": "node_webhook",
        "name": node_name,
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [0, 0],
        "parameters": {
            "httpMethod": "POST",
            "path": webhook_path,
            "responseMode": "responseNode",
            "authentication": "none",
            "webhookId": webhook_id,
            "responseData": True,
            "options": {}
        },
        "webhookId": webhook_id,
        "isActive": True
    }
    
    if workflow_type == "settlement_processor":
        # Settlement processor: webhook → HTTP request to Supabase
        supabase_url_clean = supabase_url.rstrip("/") if supabase_url else ""
        http_request_node = {
            "id": "node_http_request",
            "name": "Update Order Settlement",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.1,
            "position": [300, 0],
            "parameters": {
                "method": "PATCH",
                "url": f"={{{{ '{supabase_url_clean}/rest/v1/orders?order_id=eq.' + $json.body.order_id }}}}",
                "sendHeaders": True,
                "headerParameters": {
                    "parameters": [
                        {"name": "apikey", "value": supabase_key},
                        {"name": "Authorization", "value": f"Bearer {supabase_key}"},
                        {"name": "Content-Type", "value": "application/json"},
                        {"name": "Prefer", "value": "return=representation"},
                    ]
                },
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": (
                    # The deployed public.orders table has no fill_price_cents /
                    # outcome_won / settled_at columns; PATCHing them returns
                    # PGRST204. status + pnl_dollars are the settlement facts it
                    # can hold. See supabase/schema.sql for the full schema.
                    "={{ JSON.stringify({ status: $json.body.status,"
                    " pnl_dollars: $json.body.pnl_dollars }) }}"
                ),
                "options": {}
            },
            "isActive": True
        }
        
        respond_node = {
            "id": "node_respond",
            "name": "Respond to Webhook",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [600, 0],
            "parameters": {
                "responseCode": "200",
                "responseBody": "={{ $json }}",
                "respondWith": "json",
                "options": {}
            },
            "position": [600, 0]
        }
        
        nodes = [webhook_node, http_request_node, respond_node]
        
        # n8n resolves connections by node NAME, not id.
        connections = {
            node_name: {
                "main": [[
                    {"node": "Update Order Settlement", "type": "main", "index": 0}
                ]]
            },
            "Update Order Settlement": {
                "main": [[
                    {"node": "Respond to Webhook", "type": "main", "index": 0}
                ]]
            }
        }
    elif workflow_type == "ack_only":
        # Health/alert sink: acknowledge without running any subprocess, so a
        # monitor ping can never start a trading cycle.
        respond_node = {
            "id": "node_respond",
            "name": "Respond to Webhook",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [300, 0],
            "parameters": {
                "responseCode": "200",
                "responseBody": "={{ {ok: true, received: $json.body} }}",
                "respondWith": "json",
                "options": {}
            },
            "position": [300, 0]
        }

        nodes = [webhook_node, respond_node]

        connections = {
            node_name: {
                "main": [[
                    {"node": "Respond to Webhook", "type": "main", "index": 0}
                ]]
            }
        }
    else:
        command = make_command(python_script, python_args)

        code_node = {
            "id": "node_exec",
            "name": "Execute Python",
            "type": "n8n-nodes-base.executeCommand",
            "typeVersion": 1,
            "position": [300, 0],
            "parameters": {
                "command": command,
                "executeOnce": True
            }
        }
        
        respond_node = {
            "id": "node_respond",
            "name": "Respond to Webhook",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [600, 0],
            "parameters": {
                "responseCode": "200",
                "responseBody": "={{ $json }}",
                "respondWith": "json",
                "options": {}
            },
            "position": [600, 0]
        }
        
        nodes = [webhook_node, code_node, respond_node]
        
        # n8n resolves connections by node NAME, not id.
        connections = {
            node_name: {
                "main": [[
                    {"node": "Execute Python", "type": "main", "index": 0}
                ]]
            },
            "Execute Python": {
                "main": [[
                    {"node": "Respond to Webhook", "type": "main", "index": 0}
                ]]
            }
        }
    
    # Build nodes JSON string
    nodes_json = json.dumps(nodes, separators=(',', ':'))
    connections_json = json.dumps(connections, separators=(',', ':'))
    
    # workflow_entity row
    workflow_entity = (
        workflow_id,
        name,
        1,  # active
        nodes_json,  # nodes
        connections_json,  # connections
        '{}',  # settings
        '{}',  # staticData
        '{}',  # pinData
        version_id,  # versionId
        0,  # triggerCount
        '{}',  # meta
        None,  # parentFolderId
        now,  # createdAt
        now,  # updatedAt
        0,  # isArchived
        1,  # versionCounter
        None,  # description
        version_id,  # activeVersionId
        '[]',  # nodeGroups
        None  # sourceWorkflowId
    )
    
    # workflow_history row
    workflow_history = (
        version_id,
        workflow_id,
        USER_ID,  # authors
        now,  # createdAt
        now,  # updatedAt
        nodes_json,  # nodes
        connections_json,  # connections
        name,  # name
        False,  # autosaved
        None,  # description
        '[]'  # nodeGroups
    )
    
    # workflow_published_version row
    published_version = (
        workflow_id,
        version_id,  # publishedVersionId
        now,  # createdAt
        now  # updatedAt
    )
    
    # workflow_publish_history row (id is INTEGER PRIMARY KEY - auto-assign)
    publish_history = (
        None,  # id (auto-assigned by SQLite)
        workflow_id,
        version_id,
        'activated',  # event
        USER_ID,  # userId
        now  # createdAt
    )
    
    # shared_workflow row
    shared_workflow = (
        workflow_id,
        PROJECT_ID,
        'workflow:owner',
        now,
        now
    )
    
    # webhook_entity row
    webhook_entity = (
        workflow_id,
        webhook_path,
        'POST',
        node_name,
        webhook_id,
        len(webhook_path)  # pathLength
    )
    
    return {
        'workflow_entity': workflow_entity,
        'workflow_history': workflow_history,
        'published_version': published_version,
        'publish_history': publish_history,
        'shared_workflow': shared_workflow,
        'webhook_entity': webhook_entity
    }


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # Check if workflows already exist
    cur.execute("SELECT id FROM workflow_entity WHERE name LIKE 'SuperBot%'")
    existing = cur.fetchall()
    if existing:
        print("SuperBot workflows already exist. Removing old ones...")
        for row in existing:
            wid = row[0]
            cur.execute("DELETE FROM webhook_entity WHERE workflowId=?", (wid,))
            cur.execute("DELETE FROM shared_workflow WHERE workflowId=?", (wid,))
            cur.execute("DELETE FROM workflow_publish_history WHERE workflowId=?", (wid,))
            cur.execute("DELETE FROM workflow_published_version WHERE workflowId=?", (wid,))
            cur.execute("DELETE FROM workflow_history WHERE workflowId=?", (wid,))
            cur.execute("DELETE FROM workflow_entity WHERE id=?", (wid,))
        conn.commit()
    
    workflows = [
        {
            'id': new_id(),
            'name': 'SuperBot Backtest',
            'webhook_path': 'superbot-backtest',
            'webhook_id': 'superbot-backtest-auto',
            'node_name': 'SuperBot Backtest',
            'script': os.path.join(BASE_DIR, 'backtest.py'),
            'args': '--quick --limit 50 --top 10 --jobs 2'
        },
        {
            'id': new_id(),
            'name': 'SuperBot Data Collect',
            'webhook_path': 'superbot-collect',
            'webhook_id': 'superbot-collect-auto',
            'node_name': 'SuperBot Data Collect',
            'script': os.path.join(BASE_DIR, 'legacy', 'data_collector.py'),
            'args': '--series KXBTC15M KXETH15M KXSOL15M KXXRP15M KXDOGE15M --limit 100'
        },
        {
            'id': new_id(),
            'name': 'SuperBot Trade',
            'webhook_path': 'superbot-trade',
            'webhook_id': 'superbot-trade-auto',
            'node_name': 'SuperBot Trade',
            'workflow_type': 'settlement_processor',
            'supabase_url': os.environ.get('SUPABASE_URL', ''),
            'supabase_key': os.environ.get('SUPABASE_SERVICE_KEY', ''),
        },
        {
            'id': new_id(),
            'name': 'SuperBot Monitor',
            'webhook_path': 'superbot-monitor',
            'webhook_id': 'superbot-monitor-auto',
            'node_name': 'SuperBot Monitor',
            # Acknowledge alerts only. Spawning trader.py here would run a
            # second cycle that can place orders once DRY_RUN is false.
            'workflow_type': 'ack_only',
        }
    ]
    
    for wf in workflows:
        # The settlement workflow writes the Supabase key into the n8n node.
        # Refuse to inject it with empty credentials rather than create a
        # silently broken node; the operator supplies them at injection time.
        if wf.get('workflow_type') == 'settlement_processor' and not (
                wf.get('supabase_url') and wf.get('supabase_key')):
            print("SKIP %s: SUPABASE_URL / SUPABASE_SERVICE_KEY not set "
                  "(settlement routing needs both)" % wf['name'])
            continue
        print("Inserting workflow: %s (webhook: %s)" % (wf['name'], wf['webhook_path']))
        data = make_workflow(
            wf['id'], wf['name'], wf['webhook_path'],
            wf['webhook_id'], wf['node_name'],
            wf.get('script'), wf.get('args', ''),
            wf.get('workflow_type', 'python_script'),
            wf.get('supabase_url', ''),
            wf.get('supabase_key', '')
        )

        # Insert into all tables
        cur.execute(
            "INSERT INTO workflow_entity (id, name, active, nodes, connections, settings, staticData, pinData, versionId, triggerCount, meta, parentFolderId, createdAt, updatedAt, isArchived, versionCounter, description, activeVersionId, nodeGroups, sourceWorkflowId) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            data['workflow_entity']
        )
        
        cur.execute(
            "INSERT INTO workflow_history (versionId, workflowId, authors, createdAt, updatedAt, nodes, connections, name, autosaved, description, nodeGroups) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            data['workflow_history']
        )
        
        cur.execute(
            "INSERT INTO workflow_published_version (workflowId, publishedVersionId, createdAt, updatedAt) VALUES (?, ?, ?, ?)",
            data['published_version']
        )
        
        cur.execute(
            "INSERT INTO workflow_publish_history (id, workflowId, versionId, event, userId, createdAt) VALUES (?, ?, ?, ?, ?, ?)",
            data['publish_history']
        )
        
        cur.execute(
            "INSERT INTO shared_workflow (workflowId, projectId, role, createdAt, updatedAt) VALUES (?, ?, ?, ?, ?)",
            data['shared_workflow']
        )
        
        cur.execute(
            "INSERT INTO webhook_entity (workflowId, webhookPath, method, node, webhookId, pathLength) VALUES (?, ?, ?, ?, ?, ?)",
            data['webhook_entity']
        )
    
    conn.commit()
    
    # Verify
    print("\n=== VERIFICATION ===")
    cur.execute("SELECT id, name, active, activeVersionId FROM workflow_entity WHERE name LIKE 'SuperBot%'")
    for row in cur.fetchall():
        print("Workflow: %s (id=%s, active=%s, versionId=%s)" % (row[1], row[0], row[2], row[3]))
    
    cur.execute("SELECT workflowId, webhookPath, method, node, webhookId FROM webhook_entity WHERE webhookPath LIKE 'superbot-%'")
    for row in cur.fetchall():
        print("Webhook: %s %s node=%s webhookId=%s" % (row[1], row[2], row[3], row[4]))
    
    cur.execute("SELECT workflowId, projectId, role FROM shared_workflow WHERE workflowId IN (SELECT id FROM workflow_entity WHERE name LIKE 'SuperBot%')")
    for row in cur.fetchall():
        print("Shared: workflowId=%s, projectId=%s, role=%s" % (row[0], row[1], row[2]))
    
    conn.close()
    print("\nAll SuperBot n8n workflows injected successfully!")


if __name__ == "__main__":
    main()