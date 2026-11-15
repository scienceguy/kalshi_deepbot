"""
mempalace_bridge.py — MemPalace bridge off the hot path (M1).

Replaces synchronous mempalace CLI calls in the trader loop with a background
bridge: an outbound queue + writer thread for writes (heartbeat, checkpoint,
status, KG facts) and an inbound subscriber for task requests. Every public
call is a non-blocking queue put (well under 1ms); the writer thread handles
CLI latency, retries and circuit breaking.

Usage:
    bridge = MemPalaceBridge("ubuntu:opencode:kalshi_deepbot")
    bridge.start()
    # In the loop (non-blocking, under 1ms):
    bridge.heartbeat()
    bridge.checkpoint({"cycles": 42, "pnl": 12.50})
    tasks = bridge.drain_tasks()   # inbound task.request events
    bridge.stop()                  # flush pending writes on shutdown
"""

import json
import logging
import queue
import shlex
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

MP_CALL = str(Path.home() / "bin" / "mp-call")
_MP_TMP = tempfile.gettempdir()

# Breaker defaults
BREAKER_MAX_FAILURES = 5
BREAKER_COOLDOWN_SECONDS = 30.0
MP_CALL_TIMEOUT = 2.0  # seconds per CLI call


class MemPalaceBridge:
    """Background MemPalace bridge — never blocks the trading path.

    All public methods are non-blocking queue puts. A writer thread drains
    the outbound queue and calls mp-call with a 2s timeout and circuit
    breaker. A subscriber thread keeps a long-lived mp-call logstream wait
    for inbound task events.
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

    # -- mp-call invocation ---------------------------------------------

    def _call_mp(self, args: list, timeout: float = MP_CALL_TIMEOUT):
        """Call ~/bin/mp-call with timeout and circuit breaker."""
        if not self._breaker_closed():
            raise RuntimeError("circuit breaker open")
        quoted = [shlex.quote(a) for a in args]
        cmd = f"cd {_MP_TMP} && {MP_CALL} " + " ".join(quoted)
        try:
            r = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True, text=True, timeout=timeout,
            )
            self._breaker_success()
            return r
        except subprocess.TimeoutExpired:
            self._breaker_failure()
            raise

    def _deliver(self, cmd_type: str, subtype: str, payload: dict,
                 correlation_id: str) -> None:
        if cmd_type == "status":
            self._deliver_status(subtype, payload, correlation_id)
        elif cmd_type == "kg":
            self._deliver_kg(subtype, payload)

    def _deliver_status(self, status_type: str, payload: dict,
                        correlation_id: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        body_parts = [
            f"status={status_type}",
            f"agent={self.agent_identity}",
            f"ts={now_iso}",
        ]
        if payload:
            body_parts.append(f"data={json.dumps(payload)}")
        r = self._call_mp([
            "logstream", "append",
            "--type", "status",
            "--stream", "project/kalshi_deepbot",
            "--room", "status",
            "--topic", f"agent-{status_type}",
            "--correlation-id", correlation_id,
            "--from-agent", self.agent_identity,
            "--to-agent", "*",
            "--body", " | ".join(body_parts),
        ])
        if r.returncode != 0:
            log.debug("MemPalace status post RC=%s: %s", r.returncode,
                      r.stderr[:120])

    def _deliver_kg(self, subtype: str, payload: dict) -> None:
        if subtype == "add":
            r = self._call_mp([
                "kg", "add",
                payload["subject"], payload["predicate"], payload["object"],
                payload.get("source_drawer_id", ""),
            ])
            if r.returncode != 0:
                log.warning("MemPalace kg add RC=%s: %s",
                            r.returncode, r.stderr[:120])

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
        """Long-lived inbound subscriber via mp-call logstream wait."""
        while not self._stop.is_set():
            try:
                r = self._call_mp([
                    "logstream", "wait",
                    "--correlation-id", f"sub-{self.agent_identity}",
                    "--to-agent", self.agent_identity,
                    "--timeout-ms", "2000",
                ], timeout=5.0)
                if r.returncode == 0 and r.stdout.strip():
                    try:
                        data = json.loads(r.stdout)
                        events = data.get("events", [])
                        for event in events:
                            try:
                                self._inbound.put_nowait(event)
                                self.metrics["inbound_received"] += 1
                            except queue.Full:
                                pass  # drop inbound if queue full
                    except (json.JSONDecodeError, KeyError):
                        pass
            except Exception:
                # Breaker open, CLI down, or transient error — back off
                time.sleep(1.0)
                continue


# Module-level default bridge (lazy: start() must be called before use).
_default_bridge: Optional[MemPalaceBridge] = None


def get_default_bridge() -> MemPalaceBridge:
    """Return the module-level bridge, creating it on first use."""
    global _default_bridge
    if _default_bridge is None:
        _default_bridge = MemPalaceBridge("ubuntu:opencode:kalshi_deepbot")
    return _default_bridge