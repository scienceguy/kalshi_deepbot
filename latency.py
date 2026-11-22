import json, logging, math, os, queue, threading, time
from collections import deque
from pathlib import Path
from typing import Optional
import config

def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)

log = logging.getLogger(__name__)
STAGES = ("tick_recv","book_update","decision","gate_eval","risk_check","order_build","order_submit","order_ack","fill_confirm")
DEFAULT_MAX_QUEUE = 1000
DEFAULT_MAX_SAMPLES = 10000
DECISION_SUBSTAGES = ("signal","gate_eval","risk_check","order_build","order_submit")
E2E_STAGE = "tick_to_submit"
TICK_TO_SUBMIT_P95_MS = _env_float("TICK_TO_SUBMIT_P95_MS", 500.0)
TICK_TO_SUBMIT_P99_MS = _env_float("TICK_TO_SUBMIT_P99_MS", 1000.0)

class LatencySpan:
    def __init__(self, stage, recorder):
        self.stage = stage; self._recorder = recorder; self._start = None; self._end = None
    def __enter__(self):
        self._start = time.monotonic(); return self
    def __exit__(self, *exc):
        self._end = time.monotonic(); self._recorder.record_span(self.stage, self._start, self._end); return False
    @property
    def duration_ms(self):
        if self._start is None: return 0.0
        end = self._end if self._end is not None else time.monotonic()
        return (end - self._start) * 1000.0

class LatencyRecorder:
    def __init__(self, log_file=None, max_queue=DEFAULT_MAX_QUEUE, max_samples=DEFAULT_MAX_SAMPLES):
        self.log_file = log_file or config.SUPERBOT_DATA_DIR / "latency.jsonl"
        self._queue = queue.Queue(maxsize=max_queue)
        self._samples = deque(maxlen=max_samples)
        self._lock = threading.Lock()
        self._dropped = 0; self._writer_started = False; self._writer_lock = threading.Lock()
    def _start_writer(self):
        with self._writer_lock:
            if not self._writer_started:
                threading.Thread(target=self._writer_loop, daemon=True, name="latency-writer").start()
                self._writer_started = True
    def _writer_loop(self):
        while True:
            try:
                item = self._queue.get(timeout=1.0)
                if item is None: break
                self._write_line(item)
            except queue.Empty: continue
            except Exception as e: log.error("Latency writer error: %s", e)
    def _write_line(self, entry):
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_file, "a") as f: f.write(json.dumps(entry) + "\n")
    def close(self):
        try: self._queue.put_nowait(None)
        except queue.Full: pass
    def record_span(self, stage, start, end):
        self.record(stage, (end - start) * 1000.0)
    def record(self, stage, duration_ms):
        with self._lock:
            self._samples.append((stage, float(duration_ms)))
            entry = {"schema": 1, "ts": time.time(), "stage": stage, "duration_ms": round(float(duration_ms), 3)}
        try:
            self._queue.put_nowait(entry); self._start_writer()
        except queue.Full:
            with self._lock: self._dropped += 1
    def span(self, stage): return LatencySpan(stage, self)
    def _pct(self, durations):
        if not durations: return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0}
        s = sorted(durations); n = len(s)
        return {"count": n, "p50_ms": round(s[int((n-1)*0.50)], 3), "p95_ms": round(s[int((n-1)*0.95)], 3), "p99_ms": round(s[int((n-1)*0.99)], 3)}
    def percentiles(self):
        with self._lock:
            durations = [d for _, d in self._samples]; dropped = self._dropped
        if not durations: return {"p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "n": 0, "dropped": dropped}
        s = sorted(durations); n = len(s)
        return {"p50_ms": round(s[int((n-1)*0.50)], 3), "p95_ms": round(s[int((n-1)*0.95)], 3), "p99_ms": round(s[int((n-1)*0.99)], 3), "n": n, "dropped": dropped}
    def stage_percentiles(self, stage):
        with self._lock:
            durations = [d for st, d in self._samples if st == stage]
        return self._pct(durations)
    def all_stage_percentiles(self):
        with self._lock:
            by = {}
            for st, d in self._samples: by.setdefault(st, []).append(d)
        return {st: self._pct(ds) for st, ds in by.items()}
    def stage_stats(self):
        with self._lock: samples = list(self._samples)
        stats = {}
        for stage, dur in samples:
            s = stats.setdefault(stage, {"n": 0, "total_ms": 0.0}); s["n"] += 1; s["total_ms"] += dur
        for stage, s in stats.items(): s["avg_ms"] = round(s["total_ms"] / s["n"], 3)
        return stats
    def budget_breaches(self):
        out = []; sp = self.stage_percentiles(E2E_STAGE)
        if sp["count"] > 0:
            if sp["p95_ms"] > TICK_TO_SUBMIT_P95_MS: out.append({"stage": E2E_STAGE, "p95_ms": sp["p95_ms"], "budget_ms": TICK_TO_SUBMIT_P95_MS, "breach": True})
            if sp["p99_ms"] > TICK_TO_SUBMIT_P99_MS: out.append({"stage": E2E_STAGE, "p99_ms": sp["p99_ms"], "budget_ms": TICK_TO_SUBMIT_P99_MS, "breach": True})
        return out
    def over_budget(self, stage_or_e2e, ms):
        try:
            budget = TICK_TO_SUBMIT_P95_MS if stage_or_e2e == E2E_STAGE else None
            if budget is None: return False
            if ms > budget:
                log.warning("latency over budget: %s %.2fms > %.2fms", stage_or_e2e, ms, budget); return True
            return False
        except Exception: return False

class DecisionTimer:
    def __init__(self, recorder):
        self.recorder = recorder; self._start = None; self._sub = {}
    def begin(self, stage): self._sub[stage] = time.monotonic()
    def end(self, stage):
        s = self._sub.pop(stage, None)
        if s is None: return 0.0
        dur = (time.monotonic() - s) * 1000.0
        name = stage if (stage in STAGES or stage in DECISION_SUBSTAGES) else "decision"
        self.recorder.record(name, dur); return dur
    def __enter__(self):
        self._start = time.monotonic(); return self
    def __exit__(self, *exc):
        if self._start is not None: self.recorder.record(E2E_STAGE, (time.monotonic() - self._start) * 1000.0)
        return False

default_recorder = LatencyRecorder()


def percentile_with_support(samples, p):
    """Nearest-rank percentile with a support count (nanochron pattern).

    Every percentile ships with its support count: how many observations back
    the estimate. Nearest-rank method: index = ceil(p / 100 * n) (1-based) on
    the sorted sample, clamped to [1, n]. Returns (value, support_count)
    where support_count is the number of samples at or below the returned
    value. Returns (None, 0) for empty input.
    """
    if not samples:
        return (None, 0)
    s = sorted(float(x) for x in samples)
    n = len(s)
    p = max(0.0, min(100.0, float(p)))
    rank = int(math.ceil(p / 100.0 * n))
    if rank < 1:
        rank = 1
    if rank > n:
        rank = n
    value = s[rank - 1]
    support = sum(1 for x in s if x <= value)
    return (value, support)


def report_percentile(histogram, p):
    """Percentile report with a support grade.

    histogram: dict of value -> count (a frequency histogram). Expands to
    samples, computes the nearest-rank percentile with support, then assigns a
    grade: support >= 10 is "normal", 3-9 is "provisional", 1-2 is
    "insufficient", and an empty histogram is "no_data".

    Returns {"p", "value", "support", "grade"}.
    """
    samples = []
    for value, count in histogram.items():
        samples.extend([float(value)] * int(count))
    value, support = percentile_with_support(samples, p)
    if value is None:
        grade = "no_data"
    elif support >= 10:
        grade = "normal"
    elif support >= 3:
        grade = "provisional"
    else:
        grade = "insufficient"
    return {"p": float(p), "value": value, "support": support, "grade": grade}
