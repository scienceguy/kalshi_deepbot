"""
latency.py — Tick-to-decision-to-order latency instrumentation (L1).

Records named spans with monotonic clocks and writes one JSON line per
decision to a background queue (never blocks the caller). Rolling p50/p95/p99
are computed from a ring buffer of recent durations.

Stages (in tick-to-decision-to-order order):
    tick_recv, book_update, decision, gate_eval, risk_check,
    order_build, order_submit, order_ack, fill_confirm

Usage:
    recorder = LatencyRecorder()
    with recorder.span("decision"):
        ...do work...
    # or manually:
    recorder.record("gate_eval", 12.5)  # ms
    stats = recorder.percentiles()
"""

import json
import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger(__name__)

# Stages in tick-to-decision-to-order order (L1).
STAGES = (
    "tick_recv",
    "book_update",
    "decision",
    "gate_eval",
    "risk_check",
    "order_build",
    "order_submit",
    "order_ack",
    "fill_confirm",
)

DEFAULT_MAX_QUEUE = 1000
DEFAULT_MAX_SAMPLES = 10000


class LatencySpan:
    """A single timed stage. Finishes on context exit."""
    def __init__(self, stage: str, recorder):
        self.stage = stage
        self._recorder = recorder
        self._start: Optional[float] = None
        self._end: Optional[float] = None

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc):
        self._end = time.monotonic()
        self._recorder.record_span(self.stage, self._start, self._end)
        return False

    @property
    def duration_ms(self) -> float:
        if self._start is None:
            return 0.0
        end = self._end if self._end is not None else time.monotonic()
        return (end - self._start) * 1000.0


class LatencyRecorder:
    """Records latency spans and writes them to a background queue.

    The public API (record, span, percentiles) never blocks the caller:
    records are handed to a bounded queue and a daemon writer thread flushes
    them to disk. When the queue is full the oldest telemetry line is dropped
    and a counter is incremented.
    """

    def __init__(
        self,
        log_file: Path = None,
        max_queue: int = DEFAULT_MAX_QUEUE,
        max_samples: int = DEFAULT_MAX_SAMPLES,
    ):
        self.log_file = log_file or config.SUPERBOT_DATA_DIR / "latency.jsonl"
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=max_queue)
        self._samples: deque = deque(maxlen=max_samples)
        self._lock = threading.Lock()
        self._dropped = 0
        self._writer_started = False
        self._writer_lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------

    def _start_writer(self) -> None:
        with self._writer_lock:
            if not self._writer_started:
                thread = threading.Thread(
                    target=self._writer_loop,
                    daemon=True,
                    name="latency-writer",
                )
                thread.start()
                self._writer_started = True

    def _writer_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=1.0)
                if item is None:  # shutdown signal
                    break
                self._write_line(item)
            except queue.Empty:
                continue
            except Exception as e:  # pragma: no cover - defensive
                log.error("Latency writer error: %s", e)

    def _write_line(self, entry: dict) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def close(self) -> None:
        """Signal the writer to stop (best-effort; daemon thread)."""
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    # -- public API ------------------------------------------------------

    def record_span(self, stage: str, start: float, end: float) -> None:
        """Record a completed span from monotonic timestamps."""
        duration_ms = (end - start) * 1000.0
        self.record(stage, duration_ms)

    def record(self, stage: str, duration_ms: float) -> None:
        """Record a latency sample in milliseconds. Never blocks the caller."""
        with self._lock:
            self._samples.append((stage, float(duration_ms)))
            entry = {
                "schema": 1,
                "ts": time.time(),
                "stage": stage,
                "duration_ms": round(float(duration_ms), 3),
            }
        try:
            self._queue.put_nowait(entry)
            self._start_writer()
        except queue.Full:
            with self._lock:
                self._dropped += 1

    def span(self, stage: str) -> LatencySpan:
        """Return a context manager that times a block and records it as a stage."""
        return LatencySpan(stage, self)

    def percentiles(self) -> dict:
        """Return rolling p50/p95/p99 across all recorded durations."""
        with self._lock:
            durations = sorted(d for _, d in self._samples)
            dropped = self._dropped
        if not durations:
            return {
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0,
                "n": 0,
                "dropped": dropped,
            }
        n = len(durations)
        # Nearest-rank percentile: index = floor((n-1) * p), 0-based.
        return {
            "p50_ms": round(durations[int((n - 1) * 0.50)], 3),
            "p95_ms": round(durations[int((n - 1) * 0.95)], 3),
            "p99_ms": round(durations[int((n - 1) * 0.99)], 3),
            "n": n,
            "dropped": dropped,
        }

    def stage_stats(self) -> dict:
        """Return per-stage counts and avg duration."""
        with self._lock:
            samples = list(self._samples)
        stats: dict = {}
        for stage, dur in samples:
            s = stats.setdefault(stage, {"n": 0, "total_ms": 0.0})
            s["n"] += 1
            s["total_ms"] += dur
        for stage, s in stats.items():
            s["avg_ms"] = round(s["total_ms"] / s["n"], 3)
        return stats


# Module-level default recorder (import and use directly).
default_recorder = LatencyRecorder()
