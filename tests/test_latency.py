"""Tests for latency.py  L1 latency instrumentation."""

import json
import time


import latency
from latency import LatencyRecorder, LatencySpan, STAGES, DecisionTimer


#  Stages 


def test_stages_are_ordered():
    """Stages are in tick-to-decision-to-order order."""
    assert STAGES == (
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


#  LatencySpan 


def test_span_context_manager_records_duration():
    """A span context manager records duration on exit."""
    calls = []
    recorder = LatencyRecorder.__new__(LatencyRecorder)
    recorder.record = lambda stage, ms: calls.append((stage, ms))

    with LatencySpan("decision", recorder) as span:
        time.sleep(0.01)  # ~10ms

    assert span.duration_ms > 0
    assert len(calls) == 1
    assert calls[0][0] == "decision"
    assert calls[0][1] > 0


def test_span_nesting_records_each_stage():
    """Nested spans each record their own duration."""
    calls = []
    recorder = LatencyRecorder.__new__(LatencyRecorder)
    recorder.record = lambda stage, ms: calls.append((stage, ms))

    with LatencySpan("decision", recorder):
        with LatencySpan("gate_eval", recorder):
            time.sleep(0.005)

    assert len(calls) == 2
    stages = [c[0] for c in calls]
    assert "decision" in stages
    assert "gate_eval" in stages


def test_span_duration_ms_before_finish_is_zero():
    """duration_ms is 0.0 before the span starts."""
    recorder = LatencyRecorder.__new__(LatencyRecorder)
    span = LatencySpan("decision", recorder)
    assert span.duration_ms == 0.0


#  LatencyRecorder.record 


def test_record_appends_sample():
    """record() appends to the internal sample ring buffer."""
    recorder = LatencyRecorder(max_samples=100)
    recorder.record("decision", 12.5)
    stats = recorder.stage_stats()
    assert stats["decision"]["n"] == 1
    assert stats["decision"]["avg_ms"] == 12.5


def test_record_never_blocks_when_queue_full(tmp_path):
    """When the queue is full, the oldest telemetry is dropped and counted."""
    log_file = tmp_path / "latency.jsonl"
    recorder = LatencyRecorder(log_file=log_file, max_queue=2, max_samples=1000)
    # Prevent the writer thread from draining the queue between records.
    recorder._start_writer = lambda: None

    # Fill the queue (writer not started, so nothing drains).
    recorder.record("a", 1.0)
    recorder.record("b", 2.0)
    # Queue is now full (maxsize=2).
    recorder.record("c", 3.0)  # should drop oldest, increment counter

    assert recorder._dropped == 1
    # percentiles still work (samples are kept in ring buffer).
    p = recorder.percentiles()
    assert p["n"] == 3
    assert p["dropped"] == 1


def test_record_does_not_block_caller(tmp_path):
    """record() returns immediately even if writer is slow."""
    log_file = tmp_path / "latency.jsonl"
    recorder = LatencyRecorder(log_file=log_file, max_queue=100, max_samples=1000)

    start = time.monotonic()
    for i in range(50):
        recorder.record("decision", float(i))
    elapsed = time.monotonic() - start

    # 50 non-blocking puts should complete in well under 1 second.
    assert elapsed < 1.0, f"record() blocked: {elapsed:.3f}s for 50 calls"


def test_percentiles_empty():
    """percentiles() on an empty recorder returns zeros."""
    recorder = LatencyRecorder(max_samples=100)
    p = recorder.percentiles()
    assert p["p50_ms"] == 0.0
    assert p["p95_ms"] == 0.0
    assert p["p99_ms"] == 0.0
    assert p["n"] == 0


def test_percentiles_computes_correctly():
    """percentiles() returns p50/p95/p99 from recorded samples."""
    recorder = LatencyRecorder(max_samples=1000)
    for i in range(1, 101):  # 1..100 ms
        recorder.record("decision", float(i))

    p = recorder.percentiles()
    assert p["n"] == 100
    assert p["p50_ms"] == 50.0
    assert p["p95_ms"] == 95.0
    assert p["p99_ms"] == 99.0


#  LatencyRecorder.span context manager 


def test_span_context_manager(tmp_path):
    """recorder.span() records through the background queue to disk."""
    log_file = tmp_path / "latency.jsonl"
    recorder = LatencyRecorder(log_file=log_file, max_queue=100, max_samples=1000)

    with recorder.span("decision"):
        time.sleep(0.01)

    # Give the writer thread a moment to flush.
    time.sleep(0.2)

    assert log_file.exists()
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["schema"] == 1
    assert entry["stage"] == "decision"
    assert entry["duration_ms"] > 0
    assert "ts" in entry


#  stage_stats 


def test_stage_stats_aggregates_per_stage():
    """stage_stats() returns per-stage count and average."""
    recorder = LatencyRecorder(max_samples=1000)
    recorder.record("decision", 10.0)
    recorder.record("decision", 20.0)
    recorder.record("gate_eval", 5.0)

    stats = recorder.stage_stats()
    assert stats["decision"]["n"] == 2
    assert stats["decision"]["avg_ms"] == 15.0
    assert stats["gate_eval"]["n"] == 1
    assert stats["gate_eval"]["avg_ms"] == 5.0


#  default_recorder 


def test_default_recorder_exists():
    """The module exposes a default recorder instance."""
    assert latency.default_recorder is not None
    assert isinstance(latency.default_recorder, latency.LatencyRecorder)


def test_span_writes_to_superbot_data_dir_by_default(tmp_path, monkeypatch):
    """Default log_file lives under SUPERBOT_DATA_DIR (V1).

    Reloads the latency module to pick up the new env var; placed last in the
    file because reload() creates a new LatencyRecorder class that would break
    isinstance() checks in tests that run after it.
    """
    data_dir = tmp_path / "my_data"
    monkeypatch.setenv("SUPERBOT_DATA_DIR", str(data_dir))
    import importlib
    import config as config_module
    importlib.reload(config_module)
    importlib.reload(latency)

    recorder = latency.LatencyRecorder()
    assert recorder.log_file == data_dir / "latency.jsonl"


#  per-stage percentiles 

def test_stage_percentiles_empty():
    r = LatencyRecorder(max_samples=100)
    p = r.stage_percentiles("decision")
    assert p == {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0}


def test_stage_percentiles_single():
    r = LatencyRecorder(max_samples=100)
    r.record("decision", 42.0)
    p = r.stage_percentiles("decision")
    assert p["count"] == 1
    assert p["p50_ms"] == 42.0
    assert p["p95_ms"] == 42.0
    assert p["p99_ms"] == 42.0


def test_stage_percentiles_known():
    r = LatencyRecorder(max_samples=1000)
    for i in range(1, 101):
        r.record("decision", float(i))
    p = r.stage_percentiles("decision")
    assert p["count"] == 100
    assert p["p50_ms"] == 50.0
    assert p["p95_ms"] == 95.0
    assert p["p99_ms"] == 99.0


def test_all_stage_percentiles():
    r = LatencyRecorder(max_samples=1000)
    r.record("decision", 10.0)
    r.record("gate_eval", 5.0)
    out = r.all_stage_percentiles()
    assert set(out.keys()) == {"decision", "gate_eval"}
    assert out["decision"]["count"] == 1


#  DecisionTimer 

def test_decision_timer_records_substages_and_e2e():
    r = LatencyRecorder(max_samples=1000)
    timer = DecisionTimer(r)
    with timer:
        timer.begin("signal")
        time.sleep(0.005)
        timer.end("signal")
        timer.begin("gate_eval")
        time.sleep(0.002)
        timer.end("gate_eval")
    # e2e span recorded
    sp = r.stage_percentiles("tick_to_submit")
    assert sp["count"] == 1
    # substage recorded
    assert r.stage_percentiles("signal")["count"] == 1
    assert r.stage_percentiles("gate_eval")["count"] == 1
    e2e = r._samples and [d for st, d in r._samples if st == "tick_to_submit"][0]
    subs = [d for st, d in r._samples if st in ("signal", "gate_eval")]
    assert e2e >= max(subs)


def test_decision_timer_unknown_stage_generic():
    r = LatencyRecorder(max_samples=1000)
    timer = DecisionTimer(r)
    with timer:
        timer.begin("foo")
        timer.end("foo")
    # recorded under generic "decision"
    assert r.stage_percentiles("decision")["count"] == 1


#  budget helpers 

def test_budget_breaches_flags_slow_e2e(monkeypatch):
    import latency
    monkeypatch.setattr(latency, "TICK_TO_SUBMIT_P95_MS", 1.0)
    monkeypatch.setattr(latency, "TICK_TO_SUBMIT_P99_MS", 1.0)
    r = LatencyRecorder(max_samples=1000)
    r.record("tick_to_submit", 100.0)
    breaches = r.budget_breaches()
    assert len(breaches) >= 1
    assert all(b["breach"] for b in breaches)


def test_budget_breaches_quiet_under_budget(monkeypatch):
    import latency
    monkeypatch.setattr(latency, "TICK_TO_SUBMIT_P95_MS", 10000.0)
    monkeypatch.setattr(latency, "TICK_TO_SUBMIT_P99_MS", 10000.0)
    r = LatencyRecorder(max_samples=1000)
    r.record("tick_to_submit", 5.0)
    assert r.budget_breaches() == []


def test_over_budget_never_raises():
    r = LatencyRecorder(max_samples=100)
    assert r.over_budget("tick_to_submit", 999999.0) in (True, False)
    assert r.over_budget("other", 5.0) is False


#  thread safety 

def test_thread_safety_no_lost_samples():
    import threading
    r = LatencyRecorder(max_samples=100000)
    r._start_writer = lambda: None
    n_threads = 8
    per = 100
    def worker():
        for i in range(per):
            r.record("decision", float(i))
    ts = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert r.stage_percentiles("decision")["count"] == n_threads * per
