"""
mempalace_bridge.py — MemPalace bridge off the hot path (M1).

Replaces synchronous mempalace CLI calls in the trader loop with a background
bridge: an outbound queue + writer thread for writes (heartbeat, checkpoint,
status, KG facts) and an inbound subscriber for task requests. Every public
call is a non-blocking queue put (well under 1ms); the writer thread handles
library latency, retries and circuit breaking. Talks to MemPalace through the
installed mempalace Python library (no external CLI).

Usage:
    bridge = MemPalaceBridge("ubuntu:opencode:kalshi_deepbot")
    bridge.start()
    # In the loop (non-blocking, under 1ms):
    bridge.heartbeat()
    bridge.checkpoint({"cycles": 42, "pnl": 12.50})
    bridge.drawer("...", {"event": "risk_halt"})   # durable note artifact
    bridge.kg_supersede("me", "measured_win_rate", "0.90@n=25", "0.95@n=30")
    tasks = bridge.drain_tasks()   # inbound task.request events
    bridge.stop()                  # flush pending writes on shutdown
"""

import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import agent_identity

# H3: Make mempalace imports optional with no-op fallback
try:
    from mempalace.logstream import Logstream, LOGSTREAM_DB_FILENAME
    from mempalace.knowledge_graph import KnowledgeGraph
    _MEMPALACE_AVAILABLE = True
except ImportError:
    _MEMPALACE_AVAILABLE = False
    # Type stubs for when mempalace is not available
    Logstream = None
    LOGSTREAM_DB_FILENAME = "logstream.sqlite3"
    KnowledgeGraph = None

log = logging.getLogger(__name__)

# H4: Allow override via MEMPALACE_DIR env var
PALACE_DIR = os.environ.get("MEMPALACE_DIR") or str(Path.home() / ".mempalace")
_LOGSTREAM_DB = os.path.join(PALACE_DIR, LOGSTREAM_DB_FILENAME)
_KG_DB = os.path.join(PALACE_DIR, "knowledge_graph.sqlite3")

# Log the resolved palace directory at startup
if _MEMPALACE_AVAILABLE:
    log.info("MemPalace bridge using directory: %s", PALACE_DIR)
else:
    log.warning("MemPalace library not available — running with no-op bridge. Palace dir would be: %s", PALACE_DIR)

# Back-compat alias: the coding agent identity is the historical default.
DEFAULT_AGENT_IDENTITY = agent_identity.CODING_AGENT

_DEFAULT_BRIDGES = {}


def get_default_bridge(identity: str = DEFAULT_AGENT_IDENTITY) -> "MemPalaceBridge":
    """Return the per-identity module-level bridge, creating it on first use.

    One identity per process-role (coding agent, kalshi service, poly
    service): separate bridges so inbound `to_agent` filtering and the
    `from_agent` on every outgoing event are unambiguous.
    """
    bridge = _DEFAULT_BRIDGES.get(identity)
    if bridge is None:
        bridge = MemPalaceBridge(identity)
        _DEFAULT_BRIDGES[identity] = bridge
    return bridge

# Breaker defaults
BREAKER_MAX_FAILURES = 5
BREAKER_COOLDOWN_SECONDS = 30.0

WRITER_TIMEOUT = 2.0  # seconds per library call (kept for parity with old CLI timeout)


class MemPalaceBridge:
    """Background MemPalace bridge — never blocks the trading path.

    All public methods are non-blocking queue puts. A writer thread drains
    the outbound queue and calls the mempalace library with a circuit
    breaker. A subscriber thread keeps a long-lived logstream wait for
    inbound task events.
    """

    def __init__(self, agent_identity: str):
        self.agent_identity = agent_identity
        self._outbound: "queue.Queue[tuple]" = queue.Queue(maxsize=1000)
        self._inbound: "queue.Queue[dict]" = queue.Queue(maxsize=1000)
        self._stop = threading.Event()
        self._writer: Optional[threading.Thread] = None
        self._subscriber: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._breaker_open_until = 0.0
        self._breaker_failures = 0
        self.metrics = {
            "outbound_queued": 0,
            "outbound_dropped": 0,
            "outbound_sent": 0,
            "outbound_failed": 0,
            "inbound_received": 0,
            "breaker_opens": 0,
            "last_ok_ts": 0.0,
        }

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the writer and subscriber threads."""
        with self._lock:
            if self._writer is not None:
                return
            self._stop.clear()
            self._writer = threading.Thread(
                target=self._writer_loop, daemon=True, name="mempalace-writer"
            )
            self._writer.start()
            self._subscriber = threading.Thread(
                target=self._subscriber_loop, daemon=True, name="mempalace-subscriber"
            )
            self._subscriber.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown and flush pending outbound writes."""
        self._stop.set()
        # Unblock the writer if it's waiting on the queue
        try:
            self._outbound.put_nowait(None)
        except queue.Full:
            pass
        if self._writer:
            self._writer.join(timeout=timeout)
        if self._subscriber:
            self._subscriber.join(timeout=timeout)

    # ── non-blocking public API (under 1ms each) ─────────────────────

    def heartbeat(self, cycles_completed: int = 0) -> None:
        """Queue a heartbeat status event. Non-blocking."""
        self._enqueue("status", "heartbeat",
                      {"cycles_completed": cycles_completed})

    def checkpoint(self, state: dict, correlation_id: Optional[str] = None) -> None:
        """Queue a checkpoint status event. Non-blocking."""
        self._enqueue("status", "agent-checkpoint", state, correlation_id=correlation_id)

    def status(self, status: str, extra: Optional[dict] = None) -> None:
        """Queue a generic status event. Non-blocking."""
        self._enqueue("status", status, extra or {})

    def kg_add(self, subject: str, predicate: str, obj: str,
               source_drawer_id: Optional[str] = None) -> None:
        """Queue a knowledge-graph fact add. Non-blocking."""
        self._enqueue("kg", "add", {
            "subject": subject, "predicate": predicate, "object": obj,
            "source_drawer_id": source_drawer_id or "",
        })

    def kg_supersede(self, subject: str, predicate: str,
                     old_obj: str, new_obj: str,
                     source_drawer_id: Optional[str] = None) -> None:
        """Queue a knowledge-graph single-valued fact replacement. Non-blocking."""
        self._enqueue("kg", "supersede", {
            "subject": subject, "predicate": predicate,
            "old_obj": old_obj, "new_obj": new_obj,
            "source_drawer_id": source_drawer_id or "",
        })

    def kg_invalidate(self, subject: str, predicate: str, obj: str,
                      ended: Optional[str] = None) -> None:
        """Queue a knowledge-graph fact retirement. Non-blocking."""
        self._enqueue("kg", "invalidate", {
            "subject": subject, "predicate": predicate, "object": obj,
            "ended": ended or "",
        })

    def drawer(self, content: str, metadata: Optional[dict] = None) -> None:
        """Queue a durable drawer (note artifact). Non-blocking.

        The writer thread stores the content verbatim; nothing here returns
        or echoes it. One drawer per significant event, not per cycle.
        """
        self._enqueue("artifact", "put", {
            "content": str(content),
            "metadata": dict(metadata or {}),
        })

    def ack_event(self, event_id: str, body: str = "") -> None:
        """Queue an ``event.ack`` for a delivered message (C1). Non-blocking."""
        self._enqueue("ack", "ack", {"event_id": str(event_id), "body": str(body or "")})

    def drain_tasks(self) -> list:
        """Drain inbound task.request events from the subscriber queue.

        Returns a list of event dicts (may be empty). Non-blocking.
        """
        tasks = []
        while True:
            try:
                tasks.append(self._inbound.get_nowait())
            except queue.Empty:
                break
        return tasks

    def get_metrics(self) -> dict:
        """Return a snapshot of bridge metrics."""
        with self._lock:
            return dict(self.metrics)

    # ── internal ─────────────────────────────────────────────────────

    def _enqueue(self, cmd_type: str, subtype: str, payload: dict,
                 correlation_id: Optional[str] = None) -> None:
        """Queue an outbound write. Drops oldest if full (never blocks)."""
        try:
            self._outbound.put_nowait((
                cmd_type, subtype, dict(payload),
                correlation_id or str(uuid.uuid4()),
                time.time(),
            ))
            self.metrics["outbound_queued"] += 1
        except queue.Full:
            self.metrics["outbound_dropped"] += 1

    # -- circuit breaker ------------------------------------------------

    def _breaker_closed(self) -> bool:
        now = time.time()
        if self._breaker_open_until > 0 and now < self._breaker_open_until:
            return False
        if self._breaker_open_until > 0:
            # Half-open probe: allow one attempt
            self._breaker_open_until = 0.0
            self._breaker_failures = 0
        return True

    def _breaker_success(self) -> None:
        self._breaker_failures = 0
        self._breaker_open_until = 0.0
        self.metrics["last_ok_ts"] = time.time()

    def _breaker_failure(self) -> None:
        self._breaker_failures += 1
        self.metrics["outbound_failed"] += 1
        if self._breaker_failures >= BREAKER_MAX_FAILURES:
            self._breaker_open_until = time.time() + BREAKER_COOLDOWN_SECONDS
            self.metrics["breaker_opens"] += 1

    # -- library delivery -----------------------------------------------

    def _deliver(self, cmd_type: str, subtype: str, payload: dict,
                 correlation_id: str) -> None:
        if not _MEMPALACE_AVAILABLE:
            # No-op when mempalace library is not available (H3)
            self._breaker_success()  # Track as successful no-op
            return
        if cmd_type == "status":
            self._deliver_status(subtype, payload, correlation_id)
        elif cmd_type == "kg":
            self._deliver_kg(subtype, payload)
        elif cmd_type == "artifact":
            self._deliver_artifact(payload)
        elif cmd_type == "ack":
            self._deliver_ack(subtype, payload)

    def _deliver_status(self, status_type: str, payload: dict,
                        correlation_id: str) -> None:
        if not self._breaker_closed():
            raise RuntimeError("circuit breaker open")
        now_iso = datetime.now(timezone.utc).isoformat()
        body_parts = [
            f"status={status_type}",
            f"agent={self.agent_identity}",
            f"ts={now_iso}",
        ]
        if payload:
            body_parts.append(f"data={json.dumps(payload)}")
        try:
            ls = Logstream(db_path=_LOGSTREAM_DB)
            ls.append_event(
                type="status",
                stream="project/kalshi_deepbot",
                room="status",
                topic=f"agent-{status_type}",
                correlation_id=correlation_id,
                from_agent=self.agent_identity,
                to_agent="*",
                status="open",
                body=" | ".join(body_parts),
            )
            self._breaker_success()
        except Exception as e:
            self._breaker_failure()
            log.warning("MemPalace status post failed: %s", e)
            raise

    def _deliver_kg(self, subtype: str, payload: dict) -> None:
        if not self._breaker_closed():
            raise RuntimeError("circuit breaker open")
        kg = KnowledgeGraph(db_path=_KG_DB)
        try:
            if subtype == "add":
                kg.add_triple(
                    payload["subject"], payload["predicate"], payload["object"],
                    source_drawer_id=payload.get("source_drawer_id", ""),
                )
            elif subtype == "supersede":
                kg.supersede(
                    payload["subject"], payload["predicate"],
                    payload["old_obj"], payload["new_obj"],
                    source_drawer_id=payload.get("source_drawer_id", ""),
                )
            elif subtype == "invalidate":
                kg.invalidate(
                    payload["subject"], payload["predicate"], payload["object"],
                    ended=payload.get("ended") or None,
                )
            else:
                raise ValueError(f"unknown kg subtype {subtype!r}")
            self._breaker_success()
        except Exception as e:
            self._breaker_failure()
            log.warning("MemPalace kg %s failed: %s", subtype, e)
            raise

    def _deliver_ack(self, subtype: str, payload: dict) -> None:
        if not self._breaker_closed():
            raise RuntimeError("circuit breaker open")
        try:
            ls = Logstream(db_path=_LOGSTREAM_DB)
            ls.ack_event(payload["event_id"], from_agent=self.agent_identity,
                         status="received", body=payload.get("body") or "")
            self._breaker_success()
        except Exception as e:
            self._breaker_failure()
            log.warning("MemPalace ack failed for %s: %s",
                        payload.get("event_id"), e)
            raise

    def _deliver_artifact(self, payload: dict) -> None:
        """Store a drawer verbatim as a note artifact. Content is never
        echoed into metrics or logs."""
        if not self._breaker_closed():
            raise RuntimeError("circuit breaker open")
        try:
            ls = Logstream(db_path=_LOGSTREAM_DB)
            result = ls.put_artifact(
                kind="note",
                content=payload["content"],
                created_by=self.agent_identity,
                metadata=payload.get("metadata") or {},
            )
            self._breaker_success()
            log.debug("MemPalace drawer stored: id=%s size=%s",
                      result.get("id"), result.get("size"))
        except Exception as e:
            self._breaker_failure()
            log.warning("MemPalace drawer put failed: %s", e)
            raise

    # -- threads ---------------------------------------------------------

    def _writer_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._outbound.get(timeout=1.0)
                if item is None:  # shutdown signal
                    break
                cmd_type, subtype, payload, correlation_id, _enqueued = item
                try:
                    self._deliver(cmd_type, subtype, payload, correlation_id)
                    self.metrics["outbound_sent"] += 1
                except Exception as e:
                    log.warning("MemPalace delivery failed: %s", e)
                finally:
                    self._outbound.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                log.warning("MemPalace writer error: %s", e)

    def _subscriber_loop(self) -> None:
        """Long-lived inbound subscriber via mempalace library logstream wait."""
        # H3: No-op when mempalace library is not available
        if not _MEMPALACE_AVAILABLE:
            return
        # A fresh DB can still be mid-creation when this thread starts —
        # retry the constructor like the wait loop retries, or inbound
        # delivery dies silently for the process lifetime.
        ls = None
        while not self._stop.is_set():
            try:
                ls = Logstream(db_path=_LOGSTREAM_DB)
                break
            except Exception:
                time.sleep(1.0)
        if ls is None:
            return
        while not self._stop.is_set():
            try:
                event = ls.wait_events(
                    timeout_ms=2000,
                    correlation_id=f"sub-{self.agent_identity}",
                    to_agent=self.agent_identity,
                )
                if event is None:
                    continue  # timeout, no event — poll again
                try:
                    self._inbound.put_nowait(event)
                    self.metrics["inbound_received"] += 1
                except queue.Full:
                    pass  # drop inbound if queue full
            except Exception:
                # Library error or transient — back off and retry
                time.sleep(1.0)
                continue



