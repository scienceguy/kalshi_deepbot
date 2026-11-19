"""
events.py — Versioned event envelopes and event type catalog (N1/N3).

Every trader emits events through a durable outbox (local_db.event_outbox)
that a separate sender thread delivers to n8n. The envelope is versioned so
consumers can evolve independently of producers.

Envelope (N3 — one envelope for both traders):
    {
        "schema_version": 1,
        "event_id": "<uuid4, or sha256(source|mode|type|key)[:32] when a
                     deterministic key is given>",
        "ts": <unix seconds float>,
        "source": "kalshi_trader" | "poly_us_trader",
        "agent_id": "<SUPERBOT_AGENT_ID or the service identity for `source`>",
        "mode": "dry" | "live",
        "type": "order.placed" | ...,
        "severity": "debug" | "info" | "warning" | "critical",
        "payload": { ...event-specific fields... }
    }

Delivery classes (N2 lanes):
    critical  — halts, ruin, auth failures, reconciliation mismatches:
                never dropped, reserved worker, long bounded timeout
    normal    — orders, fills, settlements: persisted, retried with backoff
    telemetry — state heartbeats, metrics: coalesced (latest per key),
                drop-oldest when the backlog exceeds a cap, short timeout
"""

import hashlib
import json
import os
import socket
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


import agent_identity


def _default_origin() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


_SOURCE_AGENTS = {
    "kalshi_trader": agent_identity.KALSHI_TRADER,
    "poly_us_trader": agent_identity.POLY_TRADER,
}


def _origin_for_source(source: str) -> str:
    """M3: a service event carries that service's identity, not host:pid."""
    return _SOURCE_AGENTS.get(source, _default_origin())


# Event types emitted by both traders (N3 catalog; lanes assigned per N2).
EVENT_TYPES = frozenset({
    "order.placed",        # a resting entry order was submitted
    "order.filled",        # a fill was observed
    "order.cancelled",     # a resting order was cancelled
    "order.state",         # order moved to filled/cancelled/expired
    "fill",                # legacy fill event
    "settlement",          # market settled, P&L booked
    "position.settled",    # position closed out at settlement
    "risk.halt",           # circuit breaker engaged
    "risk.resume",         # circuit breaker lifted
    "breaker.trip",        # outbox/circuit breaker opened
    "breaker.clear",       # circuit breaker closed again
    "reconcile.mismatch",  # local vs venue state diverged
    "auth.failure",        # venue auth/signature rejected
    "sync.lag",            # local->remote sync behind
    "cycle.degraded",      # cycle could not complete fully
    "daily.summary",       # end-of-day rollup
    "calibration.alert",   # vol/model calibration drift
    "latency.alert",       # p95 exceeded budget
    "heartbeat",           # liveness ping
    "db.integrity_failed", # D4: integrity_check failed (restored | unrecoverable)
    "control.applied",     # a signed control command was applied (N4)
    "control.rejected",    # a control command was rejected (N4)
    "control.status",      # response to request_status (N4)
    "control.report",      # response to request_report (N4)
    "outbox.failed",       # I4: critical outbox delivery failed, sidecar written
    "operational.alert",   # scheduled job alerts (heartbeat, sync, position)
})

# Delivery class per event type (N2: three lanes).
CRITICAL_TYPES = frozenset({
    "risk.halt", "risk.resume", "breaker.trip", "breaker.clear",
    "reconcile.mismatch", "auth.failure", "control.rejected",
    "db.integrity_failed", "outbox.failed",
})
NORMAL_TYPES = frozenset({
    "order.placed", "order.filled", "order.cancelled", "order.state",
    "fill", "settlement", "position.settled",
    "operational.alert",
})
TELEMETRY_TYPES = frozenset({
    "heartbeat", "latency.alert", "sync.lag", "calibration.alert",
    "daily.summary", "cycle.degraded",
    "control.applied", "control.status", "control.report",
})

LANES = ("critical", "normal", "telemetry")

# N3 envelope: one JSON Schema, checked in tests, shared by both traders.
EVENT_SCHEMA_VERSION = 1
EVENT_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SuperBotEvent",
    "type": "object",
    "required": ["schema_version", "event_id", "ts", "source", "agent_id",
                 "mode", "type", "severity", "payload"],
    "properties": {
        "schema_version": {"const": EVENT_SCHEMA_VERSION},
        "event_id": {"type": "string", "minLength": 1},
        "ts": {"type": "number"},
        # n8n_outbox carries infra events from the delivery path itself
        # (breaker.trip/breaker.clear) — never trader business events.
        "source": {"enum": ["kalshi_trader", "poly_us_trader", "n8n_outbox"]},
        "agent_id": {"type": "string", "minLength": 1},
        "mode": {"enum": ["dry", "live"]},
        "type": {"type": "string", "minLength": 1},
        "severity": {"enum": ["debug", "info", "warning", "critical"]},
        "payload": {"type": "object"},
    },
    "additionalProperties": False,
}

# Severity per lane when the emitter does not specify one.
_LANE_SEVERITY = {"critical": "critical", "normal": "info", "telemetry": "debug"}


def delivery_class(event_type: str) -> str:
    """Return the N2 lane for an event type ('critical' | 'normal' | 'telemetry').

    Unknown types default to critical — safe-by-default.
    """
    if event_type in CRITICAL_TYPES:
        return "critical"
    if event_type in NORMAL_TYPES:
        return "normal"
    if event_type in TELEMETRY_TYPES:
        return "telemetry"
    return "critical"


def severity_for_lane(lane: str) -> str:
    return _LANE_SEVERITY.get(lane, "info")


def deterministic_event_id(source: str, mode: str, event_type: str, key: str) -> str:
    """Stable id for (source, mode, type, key) — replay-safe dedupe."""
    raw = f"{source}|{mode}|{event_type}|{key}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


@dataclass
class Event:
    schema_version: int = EVENT_SCHEMA_VERSION
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: float = field(default_factory=time.time)
    source: str = ""
    agent_id: str = field(default_factory=_default_origin)
    mode: str = ""
    type: str = ""
    severity: str = "info"
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def make_event(source: str, mode: str, event_type: str, payload: dict,
               key: Optional[str] = None, severity: Optional[str] = None,
               agent_id: Optional[str] = None, ts: Optional[float] = None,
               event_id: Optional[str] = None) -> Event:
    """Build an N3 envelope. `key` makes the event_id deterministic."""
    lane = delivery_class(event_type)
    return Event(
        schema_version=EVENT_SCHEMA_VERSION,
        event_id=event_id
        or (deterministic_event_id(source, mode, event_type, key) if key
            else str(uuid.uuid4())),
        ts=ts if ts is not None else time.time(),
        source=source,
        agent_id=agent_id or _origin_for_source(source),
        mode=mode,
        type=event_type,
        severity=severity or severity_for_lane(lane),
        payload=dict(payload),
    )


def validate_event(envelope: Any) -> None:
    """Raise jsonschema.ValidationError if `envelope` is not a valid N3 event.

    Falls back to the structural checks above when jsonschema is unavailable
    (the test environment pins jsonschema; production never validates its own
    output — this is a test/acceptance helper).
    """
    try:
        from jsonschema import validate
    except ImportError:  # pragma: no cover - jsonschema is a test dep
        validate = None
    if validate is not None:
        validate(instance=envelope, schema=EVENT_SCHEMA)
        return
    # Minimal fallback: required keys present, no unknown keys.
    required = {"schema_version", "event_id", "ts", "source", "agent_id",
                "mode", "type", "severity", "payload"}
    missing = required - set(envelope)
    if missing:
        raise ValueError(f"missing envelope fields: {sorted(missing)}")
    extra = set(envelope) - required
    if extra:
        raise ValueError(f"unknown envelope fields: {sorted(extra)}")
    json.dumps(envelope)  # must serialize
