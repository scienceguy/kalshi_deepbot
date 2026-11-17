"""agent_bus.py — C1: push-based localhost message bus with acks and presence.

The logstream stays the durable audit trail and the fallback poll; this
bus is the low-latency path between processes on the same host.

Design (spec AGENT_TASKS_v7.md C1):

- Localhost-only HTTP receiver (``127.0.0.1``) running in a thread inside
  each trader. The n8n coordination webhook appends to the logstream AND
  POSTs the same envelope to the target's receiver.
- Envelope: ``message_id``, ``correlation_id``, ``from_agent``,
  ``to_agent``, ``type``, ``topic``, ``ttl_s``, ``body``, ``ts_ms``,
  ``nonce``, ``signature``. Signed with HMAC-SHA256 via
  ``n8n_webhook_auth`` over the canonical JSON of the envelope minus the
  signature — same key, same 60-second window as the webhook path. No new
  secret, no new crypto.
- Receiver order: ``to_agent`` check → HMAC verify → TTL drop (goes to
  ``dead_letters``) → de-dup on ``message_id`` (processed once) → handler
  → ``event.ack`` through the bridge when the envelope carries an
  ``event_id``. Every failure path is non-raising: the bus never breaks
  the trading loop.
- Presence: ``peer_status()`` reads the logstream ``agent-heartbeat``
  topic and compares the newest event's age against ``max_age_s``. A
  *known stale* peer is refused routing; an *unknown* peer (DB
  unreachable / no heartbeats yet) is attempted anyway — advisory only.
- The logstream remains the fallback: if the bus is down or refuses, the
  message is still delivered by the caller's existing poll.
"""

import json
import logging
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as _urlrequest

import agent_identity
import n8n_webhook_auth

log = logging.getLogger(__name__)

BUS_HOST = "127.0.0.1"
BUS_PORTS = {
    agent_identity.CODING_AGENT: 41700,
    agent_identity.KALSHI_TRADER: 41701,
    agent_identity.POLY_TRADER: 41702,
}
BUS_PORT_OVERRIDES_ENV = "AGENT_BUS_PORT_OVERRIDES"  # JSON {"identity": port}

DEFAULT_TTL_S = 60.0
DEFAULT_PRESENCE_MAX_AGE_S = 60.0
MAX_DEAD_LETTERS = 64
MAX_REJECTED = 64
MAX_SEEN = 4096
MAX_RECEIVED = 64

SIGNATURE_WINDOW_MS = n8n_webhook_auth.SIGNATURE_WINDOW_MS


def canonical_envelope(env: dict) -> str:
    """The exact bytes the HMAC covers: envelope minus ``signature``."""
    signed = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(signed, separators=(",", ":"), ensure_ascii=False)


def envelope(*, to_agent: str, body, from_agent: str = agent_identity.CODING_AGENT,
             type: str = "task.request", topic: str = "kalshi-superbot",
             correlation_id=None, ttl_s: float = DEFAULT_TTL_S,
             message_id=None, event_id=None, now_ms=None) -> dict:
    """Build and sign one bus envelope. The sender's one entry point."""
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    env = {
        "message_id": str(message_id or uuid.uuid4()),
        "correlation_id": correlation_id if correlation_id is not None else str(uuid.uuid4()),
        "from_agent": str(from_agent),
        "to_agent": str(to_agent),
        "type": str(type),
        "topic": str(topic),
        "ttl_s": float(ttl_s),
        "body": body,
        "ts_ms": ts_ms,
        "nonce": secrets.token_hex(16),
    }
    if event_id:
        env["event_id"] = str(event_id)
    env["signature"] = n8n_webhook_auth.sign_body(ts_ms, env["nonce"], canonical_envelope(env))
    return env


def _port_overrides() -> dict:
    raw = os.environ.get(BUS_PORT_OVERRIDES_ENV)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("%s is not valid JSON — ignoring bus port overrides", BUS_PORT_OVERRIDES_ENV)
        return {}
    out = {}
    for k, v in (data or {}).items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            log.warning("%s: port for %s is not an integer — ignoring", BUS_PORT_OVERRIDES_ENV, k)
    return out


def port_for(identity: str):
    """Bus port for an identity: env override first, then the built-in map."""
    overrides = _port_overrides()
    if identity in overrides:
        return overrides[identity]
    return BUS_PORTS.get(identity)


def peer_status(identity: str, *, ls=None, now=None,
                max_age_s: float = DEFAULT_PRESENCE_MAX_AGE_S) -> str:
    """``alive`` / ``stale`` / ``unknown`` for a peer, from heartbeat age.

    ``unknown`` (logstream unreachable or no heartbeat yet) is treated as
    attempt-anyway by callers; only a *known stale* peer is refused.
    """
    if ls is None:
        try:
            from mempalace.logstream import Logstream, LOGSTREAM_DB_FILENAME
            import mempalace_bridge  # same DB the service heartbeats land in
            ls = Logstream(db_path=os.path.join(
                mempalace_bridge.PALACE_DIR, LOGSTREAM_DB_FILENAME))
        except Exception:
            return "unknown"
    try:
        evs = ls.list_events(topic="agent-heartbeat", from_agent=identity,
                             order="desc", limit=1)
    except Exception:
        return "unknown"
    if not evs:
        return "unknown"
    created = evs[0].get("created_at")
    ts = _parse_iso(created)
    if ts is None:
        return "unknown"
    now = float(now if now is not None else time.time())
    age = now - ts
    if 0 <= age <= max_age_s:
        return "alive"
    if age < 0:
        return "unknown"  # clock skew / bad data — never refuse on it
    return "stale"


def peer_alive(identity: str, *, ls=None, now=None,
               max_age_s: float = DEFAULT_PRESENCE_MAX_AGE_S) -> bool:
    return peer_status(identity, ls=ls, now=now, max_age_s=max_age_s) == "alive"


def _parse_iso(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _trim(items: list, cap: int) -> list:
    if len(items) > cap:
        del items[: len(items) - cap]
    return items


class BusReceiver:
    """Socket-free receiver core. The HTTP layer is a thin wrapper."""

    def __init__(self, identity: str, handler, *, bridge=None):
        self.identity = str(identity)
        self.handler = handler          # callable(env) -> None; must be non-raising
        self.bridge = bridge           # optional; ack(event_id) when set
        self.seen = {}                 # message_id -> True, FIFO-capped at MAX_SEEN
        self.dead_letters = []         # expired envelopes, capped
        self.rejected = []             # verify failures, capped
        self.received = []             # last MAX_RECEIVED processed envelopes
        self.now_ms = lambda: int(time.time() * 1000)

    def receive(self, env) -> dict:
        if not isinstance(env, dict):
            return {"ok": False, "reason": "envelope must be a JSON object"}

        to = env.get("to_agent")
        if to not in (self.identity, "*"):
            return {"ok": False, "reason": "not addressed to me", "to_agent": to}

        now = self.now_ms()
        ok, reason = n8n_webhook_auth.verify_request(
            env.get("ts_ms"), env.get("nonce"), canonical_envelope(env),
            env.get("signature"), now_ms=now)
        if not ok:
            self.rejected.append({"message_id": env.get("message_id"), "reason": reason})
            _trim(self.rejected, MAX_REJECTED)
            return {"ok": False, "reason": f"verify: {reason}"}

        try:
            ttl_ms = float(env.get("ttl_s") or DEFAULT_TTL_S) * 1000.0
            ts_ms = int(env.get("ts_ms"))
        except (TypeError, ValueError):
            return {"ok": False, "reason": "bad ttl_s or ts_ms"}
        if now - ts_ms > ttl_ms:
            self.dead_letters.append(env)
            _trim(self.dead_letters, MAX_DEAD_LETTERS)
            return {"ok": False, "reason": "expired", "dead_letter": True}

        mid = env.get("message_id")
        if mid and mid in self.seen:
            return {"ok": True, "duplicate": True, "message_id": mid}
        if mid:
            self.seen[mid] = True
            while len(self.seen) > MAX_SEEN:
                self.seen.pop(next(iter(self.seen)))

        try:
            self.handler(env)
        except Exception as e:  # log-only doctrine: never raise into the trader
            log.warning("bus handler failed (logged, not fatal): %s", e)

        event_id = env.get("event_id")
        if event_id and self.bridge is not None:
            try:
                self.bridge.ack_event(event_id, body=f"received by {self.identity}")
            except Exception as e:
                log.warning("bus ack for %s failed (logged, not fatal): %s", event_id, e)

        self.received.append(env)
        _trim(self.received, MAX_RECEIVED)
        return {"ok": True, "message_id": mid}

    def stats(self) -> dict:
        return {
            "identity": self.identity,
            "received": len(self.received),
            "rejected": len(self.rejected),
            "dead_letters": len(self.dead_letters),
            "seen": len(self.seen),
        }


class _BusHTTPHandler(BaseHTTPRequestHandler):
    server_version = "SuperBotBus/1.0"

    def do_POST(self):  # noqa: N802 (http.server API)
        if self.path != "/message":
            return self._json(404, {"ok": False, "reason": "unknown path"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(min(length, 1 << 20))  # 1 MiB cap
        try:
            env = json.loads(raw)
        except ValueError:
            return self._json(400, {"ok": False, "reason": "invalid JSON"})
        return self._json(200, self.server.receiver.receive(env))

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            return self._json(200, {"ok": True})
        if self.path == "/status":
            return self._json(200, self.server.receiver.stats())
        return self._json(404, {"ok": False, "reason": "unknown path"})

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # keep trader logs clean
        pass


class BusServer:
    """Runs one BusReceiver on 127.0.0.1 in a daemon thread."""

    def __init__(self, receiver: BusReceiver, *, host: str = BUS_HOST, port=None):
        self.receiver = receiver
        self.host = host
        self.port = port if port is not None else port_for(receiver.identity) or 0
        self._httpd = None
        self._thread = None

    def start(self) -> int:
        self._httpd = ThreadingHTTPServer((self.host, int(self.port or 0)), _BusHTTPHandler)
        self._httpd.receiver = self.receiver
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True,
            name=f"bus-{self.receiver.identity}")
        self._thread.start()
        return self.port

    def stop(self, timeout: float = 2.0) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            finally:
                self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


def post_envelope(env: dict, *, port=None, timeout: float = 1.0,
                  transporter=None) -> dict:
    """POST one envelope to the target's bus. Never raises.

    ``transporter(url, data: bytes, timeout) -> dict`` is injectable for
    tests; the default speaks HTTP via urllib.
    """
    to_agent = (env or {}).get("to_agent", "")
    if to_agent == "*":
        return {"ok": False, "reason": "broadcast: no push target"}
    if port is None:
        port = port_for(to_agent)
    if port is None:
        return {"ok": False, "reason": f"no bus port for {to_agent}"}
    url = f"http://{BUS_HOST}:{int(port)}/message"
    data = json.dumps(env, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if transporter is not None:
        try:
            return transporter(url, data, timeout) or {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        req = _urlrequest.Request(url, data=data, method="POST",
                                  headers={"Content-Type": "application/json"})
        with _urlrequest.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8", "replace")
            try:
                return json.loads(payload or "{}")
            except ValueError:
                return {"ok": True, "unparsed_response": payload[:200]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _log_only_handler(env: dict) -> None:
    log.info("BUS message id=%s from=%s type=%s topic=%s body=%.200s",
             env.get("message_id", ""), env.get("from_agent", ""),
             env.get("type", ""), env.get("topic", ""), str(env.get("body", "")))


if __name__ == "__main__":  # coding-agent tooling: python -m agent_bus
    logging.basicConfig(level=logging.INFO)
    port = port_for(agent_identity.CODING_AGENT) or 41700
    server = BusServer(BusReceiver(agent_identity.CODING_AGENT, _log_only_handler),
                       port=port)
    started = server.start()
    log.info("agent bus listening on %s:%s for %s", BUS_HOST, started,
             agent_identity.CODING_AGENT)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()
