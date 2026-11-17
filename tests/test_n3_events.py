"""Tests for N3 — one versioned envelope, both traders, n8n workflow JSONs.

Acceptance (AGENT_TASKS_v7.md):
- each event type has a test that the trader emits a schema-valid envelope
  (JSON Schema checked here with FORMAT_CHECKER-free draft-07 schema);
- the envelope contains no secrets (scanned from the trader sources'
  emit_event payload arguments + envelope serialization);
- workflow JSONs validate (structure, headerAuth, verify code in sync) and
  install idempotently (fake REST server, same pattern as test_n1_webhook_auth).
"""

import ast
import json
import re
import time
from pathlib import Path

import pytest

import config
import emit
import events
from events import EVENT_TYPES, delivery_class, make_event
from emit import LANE_WEBHOOK_PATH, emit_event, emit_sync_lag

REPO = Path(__file__).resolve().parent.parent
TRADER_FILES = ["trader.py", "poly_us_trader.py", "risk.py", "n8n_client.py",
                "emit.py"]

# Emit sites inside these files call emit_event("source", "type", ...).
_SECRET_TOKENS = ("SUPERBOT_WEBHOOK_SECRET", "BEGIN", "PRIVATE KEY",
                  "SERVICE_ROLE", "SERVICE_KEY", "api_key", "secret_key",
                  "password", "KALSHI_KEY", "n8n_api_key")


def _emit_event_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == "emit_event":
            yield node


def _payload_dict_keys(call):
    if len(call.args) >= 3 and isinstance(call.args[2], ast.Dict):
        keys = []
        for k in call.args[2].keys:
            if isinstance(k, ast.Constant):
                keys.append(str(k.value))
        return keys
    return None


# ── one envelope, both traders ─────────────────────────────────────────

def test_every_catalog_type_validates_and_routes():
    """Each catalog type yields a schema-valid envelope and a lane path."""
    code_paths = set(LANE_WEBHOOK_PATH.values())
    assert code_paths == {"superbot-critical-alert", "superbot-trade", "superbot-monitor"}
    for event_type in sorted(EVENT_TYPES):
        e = make_event("kalshi_trader", "dry", event_type, {"probe": True}, key="k")
        events.validate_event(e.to_dict())
        lane = delivery_class(event_type)
        assert LANE_WEBHOOK_PATH[lane] in code_paths


def _emit_type_literals(call):
    """Event type strings at an emit_event call site (literal or ternary)."""
    if len(call.args) < 2:
        return []

    def _strings(expr):
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return [expr.value]
        if isinstance(expr, ast.IfExp):  # "risk.halt" if halted else "risk.resume"
            return _strings(expr.body) + _strings(expr.orelse)
        return []

    return _strings(call.args[1])


def test_emit_sites_all_produce_valid_envelopes():
    """Every literal emit_event(..., "type", ...) call site emits valid N3."""
    found = {}
    for fname in TRADER_FILES:
        for call in _emit_event_calls(REPO / fname):
            for t in _emit_type_literals(call):
                found.setdefault(t, []).append(fname)
    # the spec-mandated critical set must be wired somewhere in the traders
    for required in ("risk.halt", "risk.resume", "reconcile.mismatch",
                     "order.placed", "order.filled", "order.cancelled",
                     "position.settled", "daily.summary", "cycle.degraded",
                     "sync.lag", "auth.failure"):
        assert required in found, f"no emit_event call site for {required}"
    # breaker events route through a variable in the delivery path
    # (_emit_breaker_event(event_type)); assert the literals exist there.
    client_src = (REPO / "n8n_client.py").read_text(encoding="utf-8")
    assert '"breaker.trip"' in client_src and '"breaker.clear"' in client_src
    for t, files in found.items():
        assert t in EVENT_TYPES, f"{files}: emits unknown type {t}"
        events.validate_event(make_event("kalshi_trader", "dry", t, {}, key="k").to_dict())


def test_emit_event_never_raises_and_never_network():
    """emit_event returns the envelope even when the client is broken."""
    import n8n_client

    class _Boom:
        def execute_webhook(self, *a, **k):
            raise RuntimeError("n8n down")

    orig = n8n_client.get_client
    n8n_client.get_client = lambda: _Boom()
    try:
        e = emit_event("kalshi_trader", "order.placed", {"probe": 1}, key="x")
    finally:
        n8n_client.get_client = orig
    assert e.type == "order.placed"


def test_emit_event_uses_lane_path(monkeypatch):
    sent = []

    class _Fake:
        def execute_webhook(self, path, data, **kwargs):
            sent.append((path, data))
            return {}

    import n8n_client
    monkeypatch.setattr(n8n_client, "get_client", lambda: _Fake())
    emit_event("poly_us_trader", "risk.halt", {"probe": True}, key="r1")
    emit_event("poly_us_trader", "order.filled", {"probe": True}, key="r2")
    emit_event("poly_us_trader", "heartbeat", {"probe": True}, key="r3")
    assert [p for p, _ in sent] == ["superbot-critical-alert", "superbot-trade", "superbot-monitor"]
    assert all(d.get("schema_version") == 1 for _, d in sent)


def test_emit_sync_lag_gates():
    assert emit_sync_lag("kalshi_trader", unsynced_count=0, oldest_age_s=0) is None
    assert emit_sync_lag("kalshi_trader", unsynced_count=5, oldest_age_s=100.0) is None
    e = emit_sync_lag("kalshi_trader", unsynced_count=1001, oldest_age_s=5.0)
    assert e is not None and e.type == "sync.lag" and e.severity == "warning"
    e = emit_sync_lag("kalshi_trader", unsynced_count=10, oldest_age_s=601.0)
    assert e is not None and e.type == "sync.lag"


def test_publish_sync_lag_needs_no_network():
    """Standalone sync_lag_alert check prints zero-diff verdict."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sync_lag_alert", REPO / "scripts" / "sync_lag_alert.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    db = type("DB", (), {"get_sync_lag_stats": lambda self: {
        "unsynced_count": 2000, "oldest_unsynced_age_s": 900.0}})()
    verdict = mod.check(db=db)
    assert verdict["alert"] is True
    db_ok = type("DB", (), {"get_sync_lag_stats": lambda self: {
        "unsynced_count": 3, "oldest_unsynced_age_s": 5.0}})()
    assert mod.check(db=db_ok)["alert"] is False


# ── no secrets in envelopes or emit call sites ────────────────────────

def test_emit_call_sites_contain_no_secret_tokens():
    for fname in TRADER_FILES:
        source = (REPO / fname).read_text(encoding="utf-8")
        for call in _emit_event_calls(REPO / fname):
            segment = ast.get_source_segment(source, call) or ""
            for tok in _SECRET_TOKENS:
                assert tok not in segment, f"{fname}: emit call leaks {tok}"


def test_envelope_serialization_has_no_secret_keys():
    for event_type in sorted(EVENT_TYPES):
        e = make_event("kalshi_trader", "dry", event_type,
                       {"order_id": "x", "ticker": "KXBTC15M"}, key="k")
        blob = json.dumps(e.to_dict())
        for tok in _SECRET_TOKENS:
            assert tok.lower() not in blob.lower()


def test_payload_key_names_are_clean():
    for fname in TRADER_FILES:
        for call in _emit_event_calls(REPO / fname):
            keys = _payload_dict_keys(call)
            if keys is None:
                continue  # non-literal payload (e.g. **kwargs) — covered by source scan
            for k in keys:
                assert not any(tok.lower() in k.lower() for tok in _SECRET_TOKENS), \
                    f"{fname}: suspicious payload key {k}"


# ── daily.summary emission from the risk manager ──────────────────────

def test_daily_summary_emitted_on_day_rollover(monkeypatch):
    import emit as emit_mod
    from risk import RiskManager

    captured = []
    monkeypatch.setattr(emit_mod, "emit_event",
                        lambda source, event_type, payload, **kw: captured.append(
                            (source, event_type, payload)))
    rm = RiskManager()
    rm.state.daily_pnl = 2.5
    rm.state.daily_pnl_reset_ts = time.time() - 90000  # > 24h ago
    halted, _reason = rm.update_daily_pnl(0.0)
    summaries = [c for c in captured if c[1] == "daily.summary"]
    assert summaries, "expected a daily.summary emission on day rollover"
    source, _t, payload = summaries[0]
    assert source == "kalshi_trader"
    assert payload["daily_pnl_dollars"] == 2.5


# ── workflow JSONs: structure, no secrets, install idempotently ───────

WORKFLOWS = sorted((REPO / "n8n" / "workflows").glob("*.json"))
EXPECTED_WORKFLOWS = {
    "superbot-critical-alert", "superbot-daily-digest",
    "superbot-heartbeat-watchdog-kalshi", "superbot-heartbeat-watchdog-poly",
    "superbot-sync-lag", "superbot-position-watchdog", "superbot-control",
}


def test_workflow_jsons_present():
    names = {p.stem for p in WORKFLOWS}
    assert names == EXPECTED_WORKFLOWS


def test_workflow_jsons_validate_structure_and_routes():
    critical_seen = False
    for p in WORKFLOWS:
        wf = json.loads(p.read_text())
        assert wf["name"] and wf["nodes"] and wf["connections"], p
        ids, names = [n["id"] for n in wf["nodes"]], [n["name"] for n in wf["nodes"]]
        assert len(ids) == len(set(ids)), f"{p}: duplicate node ids"
        assert len(names) == len(set(names)), f"{p}: duplicate node names"
        assert "sqlite" not in json.dumps(wf).lower()
        webhooks = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook"]
        for wh in webhooks:
            assert wh["parameters"]["authentication"] == "headerAuth", p
            critical_seen = True
        for src, outs in wf["connections"].items():
            assert src in names, f"{p}: connection from unknown node {src}"
            for branch in outs["main"]:
                for tgt in branch:
                    assert tgt["node"] in names, f"{p}: {src} -> {tgt['node']} unknown"
        blob = json.dumps(wf)
        assert not re.search(r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY", blob)
        assert not re.search(r"sk_live_|sk-[a-zA-Z0-9]{20,}", blob)
    assert critical_seen


def test_critical_alert_verify_code_matches_installer_js():
    """The JS verify block in the JSON must stay in sync with the installer's."""
    import inject_n8n_workflows as installer

    wf = json.loads((REPO / "n8n" / "workflows" / "superbot-critical-alert.json").read_text())
    verify = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.code"
                  and "x-superbot-signature" in n["parameters"].get("jsCode", ""))
    assert verify["parameters"]["jsCode"].strip() == installer.VERIFY_JS.strip()


def test_critical_alert_routes_to_env_webhook_not_a_secret():
    wf = json.loads((REPO / "n8n" / "workflows" / "superbot-critical-alert.json").read_text())
    http_nodes = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.httpRequest"]
    assert http_nodes
    for h in http_nodes:
        assert "SUPERBOT_ALERT_WEBHOOK_URL" in h["parameters"]["url"]


def test_json_workflows_install_idempotently(monkeypatch, capsys):
    """Two installer runs against a fake REST server install exactly once."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    import urllib.parse

    import inject_n8n_workflows
    import n8n_client
    import n8n_webhook_auth

    state = {"credentials": [], "workflows": {}, "active": {}, "updates": 0}

    class Handler(BaseHTTPRequestHandler):
        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/v1/credentials":
                return self._json({"data": list(state["credentials"])})
            if path == "/api/v1/workflows":
                return self._json({"data": list(state["workflows"].values())})
            if path.startswith("/api/v1/workflows/"):
                wid = path.rsplit("/", 1)[1]
                wf = state["workflows"].get(wid)
                if not wf:
                    return self._json({"message": "not found"}, 404)
                out = dict(wf)
                out["active"] = state["active"].get(wid, False)
                return self._json(out)
            return self._json({"message": "not found"}, 404)

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            data = self._read()
            if path == "/api/v1/credentials":
                cred = {"id": "cred-1", "name": data.get("name"), "type": data.get("type")}
                state["credentials"].append(cred)
                return self._json(cred)
            if path == "/api/v1/workflows":
                wid = f"wf-{len(state['workflows']) + 1}"
                data = dict(data, id=wid)
                state["workflows"][wid] = data
                return self._json(data)
            if path.endswith("/activate"):
                wid = path.split("/")[-2]
                state["active"][wid] = True
                return self._json({"id": wid, "active": True})
            return self._json({"message": "not found"}, 404)

        def do_PUT(self):
            path = urllib.parse.urlparse(self.path).path
            wid = path.rsplit("/", 1)[1]
            if wid not in state["workflows"]:
                return self._json({"message": "not found"}, 404)
            data = dict(self._read(), id=wid)
            state["workflows"][wid] = data
            state["updates"] += 1
            return self._json(data)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        monkeypatch.setattr(inject_n8n_workflows, "N8nClient",
                            lambda api_key: n8n_client.N8nClient(base_url=url, api_key=api_key))
        monkeypatch.setattr(inject_n8n_workflows, "load_api_key", lambda: "test-key")
        monkeypatch.setattr(n8n_webhook_auth, "static_key", lambda: "test-secret")
        monkeypatch.setattr(inject_n8n_workflows, "n8n_health_check", lambda: True)

        rc1 = inject_n8n_workflows.main()
        rc2 = inject_n8n_workflows.main()
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    assert rc1 == 0 and rc2 == 0, out
    names = sorted(w["name"] for w in state["workflows"].values())
    json_names = {"SuperBot Critical Alert", "SuperBot Daily Digest",
                  "SuperBot Heartbeat Watchdog Kalshi",
                  "SuperBot Heartbeat Watchdog Poly",
                  "SuperBot Sync Lag", "SuperBot Position Watchdog",
                  "SuperBot Control"}
    assert json_names <= set(names), names
    # second run updated JSON workflows instead of duplicating them
    assert state["updates"] >= len(json_names)
    assert len(state["credentials"]) == 1
    # every installed workflow is active
    assert len(state["active"]) == len(state["workflows"])
    # the critical-alert webhook got the live credential id injected
    crit = next(w for w in state["workflows"].values()
                if w["name"] == "SuperBot Critical Alert")
    wh = next(n for n in crit["nodes"] if n["type"] == "n8n-nodes-base.webhook")
    assert wh["credentials"]["httpHeaderAuth"]["id"] == "cred-1"
    assert wh["parameters"]["authentication"] == "headerAuth"
    # scheduled digest/watchdog JSONs install without a webhook path
    digest = next(w for w in state["workflows"].values()
                  if w["name"] == "SuperBot Daily Digest")
    assert any(n["type"] == "n8n-nodes-base.scheduleTrigger" for n in digest["nodes"])


def test_install_deletes_nothing_from_disk():
    """The installer must never write n8n's SQLite DB."""
    source = (REPO / "inject_n8n_workflows.py").read_text()
    assert "sqlite3" not in source
    assert "database.sqlite" not in source
