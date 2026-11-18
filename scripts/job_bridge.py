#!/usr/bin/env python3
"""H5 — loopback HTTP bridge so n8n never needs the Execute Command node.

Every n8n workflow that used to shell out now POSTs here instead. The route
picks a fixed argv from an allowlist; request data reaches a script only on
its stdin, never a shell string. Bound to 127.0.0.1 — that is the whole
trust boundary, and the command/coordinate routes re-verify the HMAC
themselves (send_command.py --stdin, n8n_coordination.py).

    POST /command     -> scripts/send_command.py --stdin   (signed body)
    POST /coordinate  -> scripts/n8n_coordination.py       (signed body)
    POST /backtest    -> backtest.py --quick ...           (no input, HMAC)
    POST /collect     -> legacy/data_collector.py ...      (no input, HMAC)
    GET  /healthz
"""

import json
import logging
import os
import subprocess
import sys
import hmac
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = int(os.environ.get("SUPERBOT_JOB_BRIDGE_PORT", "18711"))
TIMEOUT_SECONDS = float(os.environ.get("SUPERBOT_JOB_TIMEOUT_SECONDS", "600"))

# B1: require HMAC on backtest/collect so n8n callers can't trigger them
# without the shared secret. command/coordinate already verify inside their
# scripts (send_command.py --stdin, n8n_coordination.py).
REQUIRE_HMAC = {"backtest", "collect"}


def _verify_hmac(request: BaseHTTPRequestHandler, body: bytes) -> bool:
    secret = os.environ.get("SUPERBOT_WEBHOOK_SECRET") or os.environ.get(
        "SUPERBOT_JOB_BRIDGE_HMAC_SECRET")
    if not secret:
        # No secret configured: reject in production, allow only if explicitly dev
        if os.environ.get("SUPERBOT_ALLOW_NO_HMAC") == "1":
            log.warning("HMAC verification disabled (SUPERBOT_ALLOW_NO_HMAC=1)")
            return True
        log.error("HMAC secret not configured - rejecting request")
        return False
    ts = request.headers.get("X-Superbot-Timestamp")
    nonce = request.headers.get("X-Superbot-Nonce")
    sig = request.headers.get("X-Superbot-Signature")
    if not (ts and nonce and sig):
        return False
    try:
        expected = hmac.new(
            secret.encode(),
            f"{int(ts)}.{nonce}.{body.decode()}".encode(),
            hashlib.sha256
        ).hexdigest()
    except Exception:
        return False
    return hmac.compare_digest(expected, sig)


# route -> (argv, wants_stdin, requires_hmac)
ROUTES = {
    "command": (["scripts/send_command.py", "--stdin"], True, False),
    "coordinate": (["scripts/n8n_coordination.py"], True, False),
    "backtest": (["backtest.py", "--quick", "--limit", "50", "--top", "10",
                  "--jobs", "2"], False, True),
    "collect": (["legacy/data_collector.py", "--series", "KXBTC15M", "KXETH15M",
                 "KXSOL15M", "KXXRP15M", "KXDOGE15M", "--limit", "100"], False, True),
}


def run_route(route: str, body: bytes, timeout: float = TIMEOUT_SECONDS) -> dict:
    """Run one allowlisted route's script. Raises KeyError for unknown routes."""
    argv, wants_stdin, _ = ROUTES[route]
    proc = subprocess.run(
        [sys.executable, *argv], cwd=BASE_DIR,
        input=body if wants_stdin else None,
        capture_output=True, timeout=timeout,
        env={**os.environ, "PYTHONPATH": str(BASE_DIR)})
    out = proc.stdout.decode(errors="replace").strip()
    try:
        result = json.loads(out) if out else {}
    except ValueError:
        result = {"stdout": out[-2000:]}
    return {"ok": proc.returncode == 0, "exit": proc.returncode,
            "result": result,
            "stderr": proc.stderr.decode(errors="replace")[-1000:]}


class _Handler(BaseHTTPRequestHandler):
    server_version = "SuperBotJobBridge/1.0"

    def do_POST(self):  # noqa: N802 (http.server API)
        route = self.path.split("?")[0].strip("/")
        if route not in ROUTES:
            return self._json(404, {"ok": False, "reason": "unknown route"})
        _, _, needs_hmac = ROUTES[route]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(min(length, 1 << 20))  # 1 MiB cap
        if needs_hmac and not _verify_hmac(self, body):
            return self._json(401, {"ok": False, "reason": "invalid HMAC"})
        try:
            return self._json(200, run_route(route, body))
        except subprocess.TimeoutExpired:
            return self._json(504, {"ok": False, "reason": "timeout"})
        except Exception as e:
            return self._json(500, {"ok": False, "reason": str(e)})

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            return self._json(200, {"ok": True, "routes": sorted(ROUTES)})
        return self._json(404, {"ok": False, "reason": "unknown path"})

    def _json(self, code: int, payload: dict):
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass  # journald already has the scripts' own output


def make_server(port: int = PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((HOST, int(port)), _Handler)


def main() -> int:
    httpd = make_server()
    print(f"job bridge on http://{HOST}:{httpd.server_address[1]} "
          f"routes={sorted(ROUTES)}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
