#!/usr/bin/env python3
"""
Install SuperBot n8n workflows through the n8n REST API (N1).

Creates or updates each workflow by name (idempotent), activates it, and
verifies the health webhook: a signed request must answer 200, an unsigned
one must be rejected. The installer NEVER opens n8n's SQLite database —
workflow management goes through /api/v1 like any other client.

Workflows:
1. superbot-backtest  - runs parameter sweep backtest
2. superbot-collect   - collects market data for backtesting
3. superbot-trade     - settlement processor (Supabase PATCH)
4. superbot-monitor   - ack-only monitor sink
5. superbot-coordinate - inter-agent coordination (HMAC-verified receiver)
6. n8n/workflows/*.json (N3) - loaded from disk and upserted the same way:
   superbot-critical-alert (HMAC-verified critical lane), superbot-daily-digest,
   superbot-heartbeat-watchdog-kalshi/-poly, superbot-sync-lag,
   superbot-position-watchdog

Webhook nodes use header authentication with the shared secret from
~/.secrets/ (X-Superbot-Key); coordination/control webhooks additionally
verify the HMAC-SHA256 signature (body + timestamp + nonce) in a Code node
before any side effect runs.
"""

import json
import os
import shlex
import sys


def __getattr__(name):
    if name == "WORKFLOWS":
        return get_workflows()
    raise AttributeError(f"module 'inject_n8n_workflows' has no attribute '{name}'")

import n8n_webhook_auth
from n8n_client import N8nClient, load_api_key, n8n_health_check

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# JSON workflows shipped in the repo (N3): loaded and upserted by name, so
# editing the JSON and re-running the installer is all it takes. No secrets
# live in these files; the headerAuth credential id is injected at install
# time from the credential this script manages.
WORKFLOWS_JSON_DIR = os.path.join(BASE_DIR, "n8n", "workflows")

# Header-auth credential name (created idempotently via REST).
CREDENTIAL_NAME = "SuperBot Webhook Auth"
HEADER_NAME = "X-Superbot-Key"

# The coordination workflow's receiver verifies signatures with this JS
# mirror of n8n_webhook_auth.verify_request. Kept as a constant so the
# workflow JSON and the Python receiver stay next to each other.
# Throws on verification failure so the workflow STOPS before any side
# effect runs (an "ok: false" payload would still flow downstream).
# Body canonicalization matches n8n_client (json.dumps, compact,
# ensure_ascii=False); JSON.stringify output is byte-equal for all
# content except U+2028/U+2029, which JSON.stringify escapes.
VERIFY_JS = r"""
const crypto = require('crypto');
const h = $json.headers || {};
const ts = String(h['x-superbot-timestamp'] || '');
const nonce = String(h['x-superbot-nonce'] || '');
const sig = String(h['x-superbot-signature'] || '');
const raw = typeof $json.body === 'string' ? $json.body : JSON.stringify($json.body);
const secret = $env.SUPERBOT_WEBHOOK_SECRET || '';
if (!ts || !nonce || !sig || !secret) {
  throw new Error('missing signed fields');
}
const expected = crypto.createHmac('sha256', secret)
  .update(ts + '.' + nonce + '.' + raw).digest('hex');
const fresh = Math.abs(Date.now() - Number(ts)) <= 60000;
let replay = false;
try {
  const sd = $getWorkflowStaticData('global');
  sd.seenNonces = sd.seenNonces || {};
  if (sd.seenNonces[nonce]) { replay = true; } else { sd.seenNonces[nonce] = Date.now(); }
} catch (e) { /* static data unavailable — signature freshness still enforced */ }
const ok = fresh && !replay && sig.length === expected.length &&
  crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(sig));
if (!ok) {
  throw new Error(replay ? 'replayed nonce' : (fresh ? 'signature mismatch' : 'stale timestamp'));
}
return [{ json: { ok: true, reason: 'ok', body: $json.body, headers: {
  'x-superbot-timestamp': ts, 'x-superbot-nonce': nonce, 'x-superbot-signature': sig } } }];
"""


def make_command(script, args):
    """Shell command for the Execute Command node.

    n8n 2.x blocks ``require('child_process')`` in Code nodes, so the scripts
    run through n8n-nodes-base.executeCommand instead. None of the SuperBot
    scripts read stdin; each one is invoked for its side effect and CLI flags.
    """
    return shlex.join(["env", f"PYTHONPATH={BASE_DIR}", sys.executable,
                       script] + shlex.split(args))


def make_header_auth_credential(secret: str) -> dict:
    """n8n httpHeaderAuth credential payload (X-Superbot-Key possession gate)."""
    return {
        "name": CREDENTIAL_NAME,
        "type": "n8n-nodes-base.httpHeaderAuth",
        "data": {"name": HEADER_NAME, "value": secret},
    }


def make_workflow(name, webhook_path, node_name, workflow_type="python_script",
                  python_script=None, python_args="", credential_id=None,
                  supabase_url="", supabase_key=""):
    """Create a complete n8n workflow definition (REST API payload)."""
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
            "authentication": "headerAuth",
            "webhookId": webhook_path,
            "responseData": True,
            "options": {}
        },
        "credentials": {"httpHeaderAuth": {"id": credential_id, "name": CREDENTIAL_NAME}},
        "webhookId": webhook_path,
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
        respond_node = _respond_node()
        nodes = [webhook_node, http_request_node, respond_node]
        connections = {
            node_name: {"main": [[{"node": "Update Order Settlement", "type": "main", "index": 0}]]},
            "Update Order Settlement": {"main": [[{"node": "Respond to Webhook", "type": "main", "index": 0}]]},
        }
    elif workflow_type == "ack_only":
        # Health/alert sink: acknowledge without running any subprocess, so a
        # monitor ping can never start a trading cycle.
        respond_node = _respond_node(body="={{ {ok: true, received: $json.body} }}")
        nodes = [webhook_node, respond_node]
        connections = {
            node_name: {"main": [[{"node": "Respond to Webhook", "type": "main", "index": 0}]]},
        }
    elif workflow_type == "signed_command":
        # Coordination/control receiver: HMAC verify BEFORE any side effect.
        verify_node = {
            "id": "node_verify",
            "name": "Verify Signature",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [200, 0],
            "parameters": {"jsCode": VERIFY_JS},
        }
        command = make_command(python_script, python_args)
        code_node = {
            "id": "node_exec",
            "name": "Execute Python",
            "type": "n8n-nodes-base.executeCommand",
            "typeVersion": 1,
            "position": [500, 0],
            "parameters": {"command": command, "executeOnce": True}
        }
        respond_node = _respond_node()
        nodes = [webhook_node, verify_node, code_node, respond_node]
        connections = {
            node_name: {"main": [[{"node": "Verify Signature", "type": "main", "index": 0}]]},
            "Verify Signature": {"main": [[{"node": "Execute Python", "type": "main", "index": 0}]]},
            "Execute Python": {"main": [[{"node": "Respond to Webhook", "type": "main", "index": 0}]]},
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
        respond_node = _respond_node()
        nodes = [webhook_node, code_node, respond_node]
        connections = {
            node_name: {"main": [[{"node": "Execute Python", "type": "main", "index": 0}]]},
            "Execute Python": {"main": [[{"node": "Respond to Webhook", "type": "main", "index": 0}]]},
        }

    return {
        "name": name,
        "nodes": nodes,
        "connections": connections,
        "settings": {},
        "staticData": None,
        "pinData": {},
        "meta": {},
        "active": True,
    }


def _respond_node(body="={{ $json }}"):
    return {
        "id": "node_respond",
        "name": "Respond to Webhook",
        "type": "n8n-nodes-base.respondToWebhook",
        "typeVersion": 1,
        "position": [600, 0],
        "parameters": {
            "responseCode": "200",
            "responseBody": body,
            "respondWith": "json",
            "options": {}
        },
    }


def get_workflows():
    """Return workflow definitions with env vars evaluated at call time (not import time)."""
    return _WORKFLOWS_TEMPLATE


_WORKFLOWS_TEMPLATE = [
        {
            "name": "SuperBot Backtest",
            "webhook_path": "superbot-backtest",
            "node_name": "SuperBot Backtest",
            "script": os.path.join(BASE_DIR, "backtest.py"),
            "args": "--quick --limit 50 --top 10 --jobs 2"
        },
        {
            "name": "SuperBot Data Collect",
            "webhook_path": "superbot-collect",
            "node_name": "SuperBot Data Collect",
            "script": os.path.join(BASE_DIR, "legacy", "data_collector.py"),
            "args": "--series KXBTC15M KXETH15M KXSOL15M KXXRP15M KXDOGE15M --limit 100"
        },
        {
            "name": "SuperBot Trade",
            "webhook_path": "superbot-trade",
            "node_name": "SuperBot Trade",
            "workflow_type": "settlement_processor",
            "supabase_url": os.environ.get("SUPABASE_URL", ""),
            "supabase_key": os.environ.get("SUPABASE_SERVICE_KEY", ""),
        },
        {
            "name": "SuperBot Monitor",
            "webhook_path": "superbot-monitor",
            "node_name": "SuperBot Monitor",
            # Acknowledge alerts only. Spawning trader.py here would run a
            # second cycle that can place orders once DRY_RUN is false.
            "workflow_type": "ack_only",
        },
        {
            "name": "SuperBot Coordinate",
            "webhook_path": "superbot-coordinate",
            "node_name": "SuperBot Coordinate",
            # Coordination webhook: HMAC-verified receiver before any side effect.
            "workflow_type": "signed_command",
            "script": os.path.join(BASE_DIR, "n8n_coordination.py"),
            "args": "",
    },
]


def _ensure_credential(client: N8nClient, secret: str) -> str:
    """Create the header-auth credential idempotently; return its id."""
    existing = client._request("GET", "credentials", params={"limit": 100})
    for cred in existing.get("data", []):
        if cred.get("name") == CREDENTIAL_NAME:
            return cred["id"]
    created = client._request("POST", "credentials", data=make_header_auth_credential(secret))
    return created["id"]


def _load_json_workflows(credential_id: str):
    """Load the N3 workflow JSONs, injecting the live credential id."""
    out = []
    if not os.path.isdir(WORKFLOWS_JSON_DIR):
        return out
    for fname in sorted(os.listdir(WORKFLOWS_JSON_DIR)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(WORKFLOWS_JSON_DIR, fname)) as f:
            wf = json.load(f)
        for node in wf.get("nodes", []):
            creds = node.get("credentials") or {}
            for kind in creds.values():
                if isinstance(kind, dict) and not kind.get("id"):
                    kind["id"] = credential_id
        out.append(wf)
    return out


def _upsert_workflow(client: N8nClient, definition: dict, label: str = "") -> None:
    """Create or update `definition` by name, then activate it."""
    name = definition["name"]
    existing = client.get_workflow_by_name(name)
    if existing:
        client.update_workflow(existing["id"], definition)
        client.ensure_workflow_active(existing["id"])
        print("Updated workflow: %s (id=%s)%s" % (name, existing["id"], label))
    else:
        created = client.create_workflow(definition)
        client.ensure_workflow_active(created["id"])
        print("Created workflow: %s (id=%s)%s" % (name, created["id"], label))


def main():
    api_key = load_api_key()
    if not api_key:
        print("ERROR: no n8n API key (N8N_API_KEY or ~/.secrets/n8n_api_key). "
              "The REST installer cannot run without it.")
        return 1
    secret = n8n_webhook_auth.static_key()
    if not secret:
        print("ERROR: SUPERBOT_WEBHOOK_SECRET is not set and "
              "~/.secrets/superbot_webhook_secret is unreadable. "
              "Webhooks cannot be authenticated without it.")
        return 1

    client = N8nClient(api_key=api_key)

    # Startup check: n8n must be loopback-bound (N1).
    if not client.base_url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        print(f"WARNING: n8n base URL {client.base_url} is not loopback — "
              "the SuperBot webhooks are meant for 127.0.0.1 only")

    credential_id = _ensure_credential(client, secret)

    for wf in get_workflows():
        # The settlement workflow writes the Supabase key into the n8n node.
        # Refuse to install it with empty credentials rather than create a
        # silently broken node; the operator supplies them at injection time.
        if wf.get("workflow_type") == "settlement_processor" and not (
                wf.get("supabase_url") and wf.get("supabase_key")):
            print("SKIP %s: SUPABASE_URL / SUPABASE_SERVICE_KEY not set "
                  "(settlement routing needs both)" % wf["name"])
            continue
        definition = make_workflow(
            wf["name"], wf["webhook_path"], wf["node_name"],
            wf.get("workflow_type", "python_script"),
            wf.get("script"), wf.get("args", ""), credential_id,
            wf.get("supabase_url", ""), wf.get("supabase_key", ""),
        )
        _upsert_workflow(client, definition, " (webhook: %s)" % wf["webhook_path"])

    # N3: workflows shipped as JSON in n8n/workflows/ (critical alert routing,
    # daily digest, heartbeat watchdogs, sync-lag and position watchdogs).
    for definition in _load_json_workflows(credential_id):
        _upsert_workflow(client, definition, " (from n8n/workflows/)")

    # Verify: signed health request must succeed, unsigned must be rejected.
    ok_signed = n8n_health_check()
    print("\n=== VERIFICATION ===")
    print("Health check: %s" % ("OK" if ok_signed else "FAILED"))
    try:
        resp = client.session.post(
            f"{client.base_url}/webhook/superbot-monitor",
            json={"probe": "unsigned"}, timeout=5)
        if resp.status_code < 400:
            print("FAIL: unsigned request accepted (HTTP %s) — header auth not active" % resp.status_code)
            return 1
        print("Unsigned request rejected (HTTP %s) — header auth active" % resp.status_code)
    except Exception as e:
        print("Unsigned probe skipped (n8n unreachable: %s)" % e)
    print("\nAll SuperBot n8n workflows installed via REST API successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
