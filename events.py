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

Delivery classes:
    critical  — settlement, halt, fill: never dropped, in-order per venue/key
    telemetry — heartbeat, latency: drop-oldest when backlog exceeds cap
"""

import os
import socket
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


def _default_origin() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


# Event types emitted by both traders.
EVENT_TYPES = frozenset({
    "order.placed",      # a resting entry order was submitted
    "order.state",       # order moved to filled/cancelled/expired
    "fill",              # a fill was observed
    "settlement",        # market settled, P&L booked
    "risk.halt",         # circuit breaker engaged
    "risk.resume",       # circuit breaker lifted
    "calibration.alert", # vol/model calibration drift
    "latency.alert",     # p95 exceeded budget
    "sync.lag",          # local->remote sync behind
    "heartbeat",         # liveness ping
})

# Delivery class per event type.
CRITICAL_TYPES = frozenset({"settlement", "risk.halt", "risk.resume", "fill"})
TELEMETRY_TYPES = frozenset({"heartbeat", "latency.alert", "sync.lag", "calibration.alert"})


def delivery_class(event_type: str) -> str:
    """Return 'critical' or 'telemetry' for an event type."""
    if event_type in CRITICAL_TYPES:
        return "critical"
    if event_type in TELEMETRY_TYPES:
        return "telemetry"
    return "critical"  # unknown types are safe-by-default


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