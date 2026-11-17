"""
events.py — Versioned event envelopes and event type catalog (N1).

Every trader emits events through a durable outbox (local_db.event_outbox)
that a separate sender thread delivers to n8n. The envelope is versioned so
consumers can evolve independently of producers.

Envelope:
    {
        "schema": 1,
        "id": "<uuid4>",
        "ts": <unix seconds float>,
        "venue": "kalshi" | "polymarket_us",
        "mode": "dry" | "live",
        "origin": "<host:pid>",
        "type": "order.placed" | ...,
        "seq": <per-venue monotonic int>,
        "payload": { ...event-specific fields... }
    }

Delivery classes (N2 lanes):
    critical  — halts, ruin, auth failures, reconciliation mismatches:
                never dropped, reserved worker, long bounded timeout
    normal    — orders, fills, settlements: persisted, retried with backoff
    telemetry — state heartbeats, metrics: coalesced (latest per key),
                drop-oldest when the backlog exceeds a cap, short timeout
"""

import os
import socket
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


def _default_origin() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


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
})

# Delivery class per event type (N2: three lanes).
CRITICAL_TYPES = frozenset({
    "risk.halt", "risk.resume", "breaker.trip", "breaker.clear",
    "reconcile.mismatch", "auth.failure",
})
NORMAL_TYPES = frozenset({
    "order.placed", "order.filled", "order.cancelled", "order.state",
    "fill", "settlement", "position.settled",
})
TELEMETRY_TYPES = frozenset({
    "heartbeat", "latency.alert", "sync.lag", "calibration.alert",
    "daily.summary", "cycle.degraded",
})

LANES = ("critical", "normal", "telemetry")


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


@dataclass
class Event:
    """Versioned event envelope."""

    schema: int = 1
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: float = field(default_factory=time.time)
    venue: str = ""
    mode: str = ""
    origin: str = field(default_factory=_default_origin)
    type: str = ""
    seq: int = 0
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def make_event(venue: str, mode: str, event_type: str, payload: dict,
               seq: int = 0, origin: Optional[str] = None) -> Event:
    """Build an Event with the given fields."""
    return Event(
        venue=venue,
        mode=mode,
        origin=origin or _default_origin(),
        type=event_type,
        seq=seq,
        payload=dict(payload),
    )