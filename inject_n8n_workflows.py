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
- Code node (executes Python via subprocess)
- Respond to Webhook node (returns results)

Injects directly into n8n SQLite database.
"""

import os
import sqlite3
import json
import uuid
from datetime import datetime, timezone

DB_PATH = os.environ.get("N8N_DB_PATH", "/data/data/com.termux/files/home/.n8n/database.sqlite")
PROJECT_ID = os.environ.get("N8N_PROJECT_ID", "9WCli3nfVzBpxYqg")
USER_ID = os.environ.get("N8N_USER_ID", "user-gentry")

now = datetime.now(timezone.utc).isoformat() + "Z"

def new_id():
    return uuid.uuid4().hex

def make_code_node_code(script, args, webhook_path):
    """Build the JavaScript code for the Code node."""
    # Escape % for Python % formatting safety
    script_s = script.replace("%", "%%")
    args_s = args.replace("%", "%%")
    path_s = webhook_path.replace("%", "%%")
    # Escape backticks and ${ for shell safety in execSync template literal
    script_s = script_s.replace("`", "\\`").replace("${", "\\${")
    args_s = args_s.replace("`", "\\`").replace("${", "\\${")
    return """// Execute SuperBot Python script
const { execSync } = require('child_process');
const script = '%s';
const args = '%s';
const input = $input.all()[0].json || {};

try {
  const payload = JSON.stringify({ body: input, webhookPath: '%s' });
  const result = execSync(`python3 ${script} ${args}`, {
    input: payload,
    encoding: 'utf8',
    timeout: 60000,
    maxBuffer: 10 * 1024 * 1024
  });

  try {
    return [{
      json: {
        success: true,
        output: JSON.parse(result.toString().trim())
      }
    }];
  } catch (e) {
    return [{
      json: {
        success: true,
        output: result.toString().trim()
      }
    }];
  }
} catch (e) {
  return [{
    json: {
      success: false,
      error: e.message,
      stdout: e.stdout?.toString() || '',
      stderr: e.stderr?.toString() || ''
    }
  }];
}
""" % (script_s, args_s, path_s)


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
            "responseMode": "responseNode" if workflow_type != "settlement_processor" else "lastNode",
            "responseNode": "node_respond",
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
                "requestMethod": "PATCH",
                "url": f"{supabase_url_clean}/rest/v1/orders?order_id=eq={{{{ $json.order_id }}}}",
                "authentication": "headerAuth",
                "authHeaderName": "apikey",
                "authHeaderValue": supabase_key,
                "sendHeaders": True,
                "headerParameters": [
                    {
                        "name": "Authorization",
                        "value": f"Bearer {supabase_key}"
                    },
                    {
                        "name": "Content-Type",
                        "value": "application/json"
                    },
                    {
                        "name": "Prefer",
                        "value": "return=representation"
                    }
                ],
                "bodyContentType": "json",
                "jsonParameters": True,
                "jsonParametersBody": {
                    "status": "={{ $json.status }}",
                    "fill_price_cents": "{{= $json.fill_price_cents }}",
                    "outcome_won": "{{= $json.outcome_won }}",
                    "pnl_dollars": "{{= $json.pnl_dollars }}",
                    "settled_at": "{{= $json.ts if $json.ts else $now }}"
                },
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
        
        connections = {
            "node_webhook": {
                "main": [[
                    {
                        "node": "node_http_request",
                        "type": "main",
                        "index": 0
                    }
                ]]
            },
            "node_http_request": {
                "main": [[
                    {
                        "node": "node_respond",
                        "type": "main",
                        "index": 0
                    }
                ]]
            }
        }
    else:
        # Python script workflow (original behavior)
        code_js = make_code_node_code(python_script, python_args, webhook_path)
        
        code_node = {
            "id": "node_code",
            "name": "Execute Python",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [300, 0],
            "parameters": {
                "jsCode": code_js,
                "language": "javascript",
                "mode": "runOnceForAllItems"
            },
            "position": [300, 0]
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
        
        connections = {
            "node_webhook": {
                "main": [[
                    {
                        "node": "node_code",
                        "type": "main",
                        "index": 0
                    }
                ]]
            },
            "node_code": {
                "main": [[
                    {
                        "node": "node_respond",
                        "type": "main",
                        "index": 0
                    }
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
            'script': '/data/data/com.termux/files/home/kalshi_superbot/backtest.py',
            'args': '--quick --limit 50 --top 10 --jobs 2'
        },
        {
            'id': new_id(),
            'name': 'SuperBot Data Collect',
            'webhook_path': 'superbot-collect',
            'webhook_id': 'superbot-collect-auto',
            'node_name': 'SuperBot Data Collect',
            'script': '/data/data/com.termux/files/home/kalshi_superbot/data_collector.py',
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
            'script': '/data/data/com.termux/files/home/kalshi_superbot/trader.py',
            'args': '--once --debug'
        }
    ]
    
    for wf in workflows:
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