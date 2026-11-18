"""Tests for N1 — webhook HMAC envelope, receiver-side verification, installer.

Covers:
- n8n_webhook_auth.py: sign_body / verify_request / NonceStore / static_key
- n8n_client.py: _execute_webhook_signed produces signatures a receiver
  built on the same module accepts (HTTP round-trip, incl. non-ASCII body)
- n8n_client.n8n_startup_check: loopbind + unsigned-acceptance probes
- inject_n8n_workflows.py: REST installer, idempotent, headerAuth on every
  webhook, no SQLite anywhere, unsigned probe rejected
- n8n_coordination.py: refuses unsigned stdin payloads, accepts signed
  ones, escape hatch documented

No live n8n: every server is a local HTTPServer fake. Never --live.
"""

import io
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

import config
import inject_n8n_workflows
import n8n_client
import n8n_coordination
import n8n_webhook_auth
from n8n_webhook_auth import NonceStore, sign_body, verify_request


# ── n8n_webhook_auth primitives ──────────────────────────────────────────

def _secret(monkeypatch, value="test-secret"):
    monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", value)
    monkeypatch.delenv("SUPERBOT_WEBHOOK_SECRET_FILE", raising=False)


def test_sign_and_verify_roundtrip(monkeypatch):
    _secret(monkeypatch)
    ts = str(int(time.time() * 1000))
    sig = sign_body(ts, "ab12", '{"a":1}')
    ok, reason = verify_request(ts, "ab12", '{"a":1}', sig, store=NonceStore())
    assert ok is True
    assert reason == "ok"


def test_verify_rejects_wrong_secret(monkeypatch):
    _secret(monkeypatch, "sender-secret")
    ts = str(int(time.time() * 1000))
    sig = sign_body(ts, "ab12", '{"a":1}')
    _secret(monkeypatch, "receiver-secret")
    ok, reason = verify_request(ts, "ab12", '{"a":1}', sig, store=NonceStore())
    assert ok is False
    assert "signature" in reason


def test_verify_rejects_replayed_nonce(monkeypatch):
    _secret(monkeypatch)
    ts = str(int(time.time() * 1000))
    sig = sign_body(ts, "ab12", '{"a":1}')
    store = NonceStore()
    assert verify_request(ts, "ab12", '{"a":1}', sig, store=store)[0] is True
    ok, reason = verify_request(ts, "ab12", '{"a":1}', sig, store=store)
    assert ok is False
    assert "replay" in reason


def test_verify_rejects_stale_timestamp(monkeypatch):
    _secret(monkeypatch)
    ts = str(int(time.time() * 1000) - 120_000)
    sig = sign_body(ts, "ab12", '{"a":1}')
    ok, reason = verify_request(ts, "ab12", '{"a":1}', sig)
    assert ok is False
    assert "window" in reason


def test_verify_rejects_future_timestamp(monkeypatch):
    _secret(monkeypatch)
    ts = str(int(time.time() * 1000) + 120_000)
    sig = sign_body(ts, "ab12", '{"a":1}')
    ok, reason = verify_request(ts, "ab12", '{"a":1}', sig,
                                now_ms=int(time.time() * 1000))
    assert ok is False
    assert "window" in reason


def test_verify_rejects_malformed(monkeypatch):
    _secret(monkeypatch)
    ts = str(int(time.time() * 1000))
    sig = sign_body(ts, "ab12", '{"a":1}')
    assert verify_request(None, "ab12", '{"a":1}', sig)[0] is False
    assert verify_request(ts, "ab12", '{"a":1}', None)[0] is False
    assert verify_request("not-a-number", "ab12", '{"a":1}', sig)[0] is False
    assert verify_request(ts, "zz-not-hex", '{"a":1}', sig)[0] is False


def test_nonce_store_ttl_and_cap():
    store = NonceStore(ttl_ms=1000, max_entries=3)
    assert store.check_and_mark("aa", now_ms=1000) is True
    assert store.check_and_mark("aa", now_ms=1000) is False  # replay
    assert store.check_and_mark("aa", now_ms=3000) is True   # ttl expired
    for i in range(5):
        store.check_and_mark(f"{i:02x}" * 8, now_ms=5000)
    assert len(store._seen) <= 3


def test_static_key_empty_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", "")
    monkeypatch.delenv("SUPERBOT_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("SUPERBOT_WEBHOOK_SECRET_FILE", "/nonexistent/superbot_secret")
    assert n8n_webhook_auth.static_key() == ""
    with pytest.raises(RuntimeError):
        sign_body("12345", "ab12", '{"a":1}')


# ── sender → receiver HTTP round-trip ────────────────────────────────────

class _CapturingHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.captured = {
            "path": self.path,
            "body": self.rfile.read(length).decode(),
            "headers": dict(self.headers),
        }
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def _roundtrip(monkeypatch, payload):
    _secret(monkeypatch, "roundtrip-secret")
    server = HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = n8n_client.N8nClient(
            base_url=f"http://127.0.0.1:{server.server_address[1]}", api_key="x")
        resp = client._execute_webhook_signed("probe", payload, max_retries=1)
        cap = server.captured
    finally:
        server.shutdown()
    assert resp == {}
    ok, reason = verify_request(
        cap["headers"]["X-Superbot-Timestamp"],
        cap["headers"]["X-Superbot-Nonce"],
        cap["body"],
        cap["headers"]["X-Superbot-Signature"],
    )
    assert ok is True, reason
    return cap


def test_signed_webhook_roundtrip_ascii(monkeypatch):
    cap = _roundtrip(monkeypatch, {"order_id": "abc", "pnl": 1.5})
    assert cap["path"] == "/webhook/probe"
    # H2: the header carries the gate key, never the HMAC secret.
    assert cap["headers"]["X-Superbot-Key"] == n8n_webhook_auth.gate_key()
    assert cap["headers"]["X-Superbot-Key"] != "roundtrip-secret"


def test_signed_webhook_roundtrip_non_ascii(monkeypatch):
    """Non-ASCII bodies must verify too (sender uses ensure_ascii=False)."""
    # CJK chars as escapes: verify_source treats literal non-ASCII as corruption
    cap = _roundtrip(monkeypatch, {"message": "\u68c0\u67e5\u65b0\u95fb — note"})
    assert json.loads(cap["body"])["message"].startswith("\u68c0\u67e5")


# ── n8n_startup_check ────────────────────────────────────────────────────

def test_startup_check_ok_on_loopback_with_rejected_probe(monkeypatch):
    fake = SimpleNamespace(
        base_url="http://127.0.0.1:5678",
        session=SimpleNamespace(post=lambda **k: SimpleNamespace(status_code=401)))
    monkeypatch.setattr(n8n_client, "get_client", lambda: fake)
    monkeypatch.setenv("N8N_HOST", "")
    assert n8n_client.n8n_startup_check() is True


def test_startup_check_flags_non_loopback_or_unsigned(monkeypatch):
    fake = SimpleNamespace(
        base_url="http://0.0.0.0:5678",
        session=SimpleNamespace(post=lambda **k: SimpleNamespace(status_code=200)))
    monkeypatch.setattr(n8n_client, "get_client", lambda: fake)
    monkeypatch.setenv("N8N_HOST", "0.0.0.0")
    assert n8n_client.n8n_startup_check() is False


# ── REST installer (no SQLite, idempotent, headerAuth everywhere) ────────

class _FakeN8nHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _read(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        st = self.server.state
        if path == "/":
            return self._json({"ok": True})
        if path == "/api/v1/credentials":
            return self._json({"data": list(st["credentials"])})
        if path == "/api/v1/workflows":
            return self._json({"data": list(st["workflows"].values())})
        if path.startswith("/api/v1/workflows/"):
            wid = path.rsplit("/", 1)[1]
            wf = st["workflows"].get(wid)
            if not wf:
                return self._json({"message": "not found"}, 404)
            out = dict(wf)
            out["active"] = st["active"].get(wid, False)
            return self._json(out)
        return self._json({"message": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read()
        st = self.server.state
        if path == "/api/v1/credentials":
            cred = {"id": "cred-1", "name": data.get("name"), "type": data.get("type")}
            st["credentials"].append(cred)
            return self._json(cred)
        if path == "/api/v1/workflows":
            wid = f"wf-{len(st['workflows']) + 1}"
            data = dict(data)
            data["id"] = wid
            st["workflows"][wid] = data
            return self._json(data)
        if path.startswith("/api/v1/workflows/") and path.endswith("/activate"):
            wid = path.split("/")[-2]
            st["active"][wid] = True
            st["activations"].append(wid)
            return self._json({"id": wid, "active": True})
        if path == "/webhook/superbot-monitor":
            # headerAuth gate: only requests carrying the static key pass
            if self.headers.get("X-Superbot-Key"):
                return self._json({"ok": True})
            return self._json({"message": "Unauthorized"}, 401)
        return self._json({"message": "not found"}, 404)

    def do_PUT(self):
        path = urlparse(self.path).path
        data = self._read()
        st = self.server.state
        if path.startswith("/api/v1/workflows/"):
            wid = path.rsplit("/", 1)[1]
            if wid not in st["workflows"]:
                return self._json({"message": "not found"}, 404)
            data = dict(data)
            data["id"] = wid
            st["workflows"][wid] = data
            st["updates"] += 1
            return self._json(data)
        return self._json({"message": "not found"}, 404)


class _FakeN8n:
    def __init__(self):
        self.state = {"credentials": [], "workflows": {}, "active": {},
                      "activations": [], "updates": 0}
        self._server = HTTPServer(("127.0.0.1", 0), _FakeN8nHandler)
        self._server.state = self.state
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._server.shutdown()


def test_installer_rest_idempotent_no_db(monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    fake = _FakeN8n()
    try:
        url = f"http://127.0.0.1:{fake.port}"
        monkeypatch.setattr(inject_n8n_workflows, "N8nClient",
                            lambda api_key: n8n_client.N8nClient(base_url=url, api_key=api_key))
        monkeypatch.setattr(inject_n8n_workflows, "load_api_key", lambda: "test-key")
        monkeypatch.setattr(inject_n8n_workflows.n8n_webhook_auth, "static_key",
                            lambda: "test-secret")
        monkeypatch.setattr(inject_n8n_workflows, "n8n_health_check", lambda: True)

        rc1 = inject_n8n_workflows.main()
        rc2 = inject_n8n_workflows.main()
    finally:
        fake.stop()

    out = capsys.readouterr().out
    assert rc1 == 0 and rc2 == 0, out

    st = fake.state
    names = sorted(w["name"] for w in st["workflows"].values())
    # H5: the digest + watchdog workflows moved to systemd timers, so only the
    # webhook lanes remain installed in n8n.
    assert names == [
        "SuperBot Backtest", "SuperBot Control", "SuperBot Coordinate",
        "SuperBot Critical Alert", "SuperBot Data Collect", "SuperBot Monitor",
    ]
    # settlement processor skipped without Supabase creds; six workflows remain
    assert len(st["workflows"]) == 6
    assert st["updates"] >= 4                   # second run updated, never duplicated
    assert len(st["credentials"]) == 1          # credential created once

    # every webhook node (if any) is headerAuth-gated with the shared credential;
    # H5: nothing in the installed set may carry the Execute Command node.
    for w in st["workflows"].values():
        webhooks = [n for n in w["nodes"] if n["type"] == "n8n-nodes-base.webhook"]
        for wh in webhooks:
            assert wh["parameters"]["authentication"] == "headerAuth"
            assert wh["credentials"]["httpHeaderAuth"]["id"] == "cred-1"
    hooked = sorted(
        w["name"] for w in st["workflows"].values()
        if any(n["type"] == "n8n-nodes-base.webhook" for n in w["nodes"]))
    assert hooked == ["SuperBot Backtest", "SuperBot Control",
                      "SuperBot Coordinate",
                      "SuperBot Critical Alert", "SuperBot Data Collect",
                      "SuperBot Monitor"]

    # coordination workflow verifies the HMAC in a Code node before side effects
    coord = next(w for w in st["workflows"].values() if w["name"] == "SuperBot Coordinate")
    code_nodes = [n for n in coord["nodes"] if n["type"] == "n8n-nodes-base.code"]
    assert code_nodes and "createHmac" in code_nodes[0]["parameters"]["jsCode"]

    # the installer never touches n8n's SQLite database
    source = Path(inject_n8n_workflows.__file__).read_text()
    assert "sqlite3" not in source
    assert "database.sqlite" not in source


def test_installer_refuses_without_secret(monkeypatch, capsys):
    monkeypatch.setattr(inject_n8n_workflows.n8n_webhook_auth, "static_key", lambda: "")
    monkeypatch.setattr(inject_n8n_workflows, "load_api_key", lambda: "test-key")
    assert inject_n8n_workflows.main() == 1
    assert "SUPERBOT_WEBHOOK_SECRET" in capsys.readouterr().out


# ── coordination receiver ────────────────────────────────────────────────

def _run_handler(monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    called = []

    class _Result:
        returncode = 0
        stderr = ""

    def _fake_run(*args, **kwargs):
        called.append(args)
        return _Result()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return n8n_coordination.main(), called


def test_coordination_refuses_unsigned(monkeypatch, capsys):
    monkeypatch.delenv("INSECURE_COORDINATION_UNVERIFIED", raising=False)
    _secret(monkeypatch)
    msg = {"to_agent": "*", "type": "status", "body": "hello"}
    rc, called = _run_handler(monkeypatch, {"body": msg})
    out = capsys.readouterr().out
    assert rc == 1
    assert "refused" in out
    assert called == []


def test_coordination_accepts_signed(monkeypatch, capsys):
    _secret(monkeypatch, "handler-secret")
    ts = str(int(time.time() * 1000))
    nonce = "ab" * 16
    msg = {"to_agent": "ubuntu:opencode:home", "type": "status", "body": "hello"}
    body_str = json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
    sig = sign_body(ts, nonce, body_str)
    payload = {"body": msg, "headers": {
        "x-superbot-timestamp": ts, "x-superbot-nonce": nonce,
        "x-superbot-signature": sig}}
    rc, called = _run_handler(monkeypatch, payload)
    assert rc == 0
    assert len(called) == 1
    assert "logstream" in " ".join(called[0][0])
    assert "refused" not in capsys.readouterr().out


def test_coordination_escape_hatch(monkeypatch, capsys):
    monkeypatch.setenv("INSECURE_COORDINATION_UNVERIFIED", "1")
    _secret(monkeypatch)
    msg = {"type": "status", "body": "hello"}
    rc, called = _run_handler(monkeypatch, {"body": msg})
    assert rc == 0
    assert len(called) == 1
