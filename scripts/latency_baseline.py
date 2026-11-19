#!/usr/bin/env python3
"""latency_baseline.py — A1: latency measurement and regression gate.

Measures and tracks latency for critical paths:
- Guard tick cycle time
- Signal evaluation latency  
- Order placement latency (REST round-trip)
- Settlement check latency

Emits metrics to local DB and logs. Regression gate: fail if p99 > 2*budget.
"""

import logging
import os
import sqlite3
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger(__name__)

# Local DB for latency measurements (WAL mode for concurrent access)
_LATENCY_DB = Path(os.environ.get("SUPERBOT_LATENCY_DB") or config.SUPERBOT_DATA_DIR / "latency.db")


@dataclass
class LatencyRecorder:
    """Thread-local latency recorder that flushes to SQLite on interval."""
    
    _measurements: dict = field(default_factory=lambda: defaultdict(list))
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_flush: float = field(default_factory=time.monotonic)
    _flush_interval_s: float = 10.0
    
    def record(self, key: str, dt_ms: float) -> None:
        """Record a latency measurement in milliseconds."""
        with self._lock:
            self._measurements[key].append(dt_ms)
            # Flush periodically
            if time.monotonic() - self._last_flush > self._flush_interval_s:
                self._flush_locked()
    
    def _flush_locked(self) -> None:
        """Flush all measurements to DB (must hold lock)."""
        if not self._measurements:
            return
        try:
            conn = sqlite3.connect(_LATENCY_DB)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS latency_measurements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    dt_ms REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_latency_ts ON latency_measurements(ts_ms)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_latency_key ON latency_measurements(key)")
            
            now_ms = int(time.time() * 1000)
            rows = []
            for key, vals in self._measurements.items():
                for v in vals:
                    rows.append((now_ms, key, v))
            
            conn.executemany(
                "INSERT INTO latency_measurements (ts_ms, key, dt_ms) VALUES (?, ?, ?)",
                rows
            )
            conn.commit()
            conn.close()
            
            # Clear measurements after successful flush
            self._measurements.clear()
            self._last_flush = time.monotonic()
        except Exception as e:
            log.warning("latency flush failed: %s", e)
    
    def flush(self) -> None:
        """Manual flush."""
        with self._lock:
            self._flush_locked()
    
    def get_stats(self, key: str, window_s: float = 60.0) -> dict:
        """Get percentile stats for a key from DB."""
        try:
            conn = sqlite3.connect(_LATENCY_DB)
            conn.execute("PRAGMA journal_mode=WAL")
            cutoff_ms = int(time.time() * 1000) - int(window_s * 1000)
            cursor = conn.execute(
                "SELECT dt_ms FROM latency_measurements WHERE key = ? AND ts_ms >= ? ORDER BY dt_ms",
                (key, cutoff_ms)
            )
            vals = [row[0] for row in cursor.fetchall()]
            conn.close()
            
            if not vals:
                return {"count": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0}
            
            def percentile(sorted_vals, p):
                if not sorted_vals:
                    return 0
                k = (len(sorted_vals) - 1) * p
                f = int(k)
                c = min(f + 1, len(sorted_vals) - 1)
                if f == c:
                    return sorted_vals[f]
                return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)
            
            return {
                "count": len(vals),
                "p50": round(percentile(vals, 0.50), 2),
                "p95": round(percentile(vals, 0.95), 2),
                "p99": round(percentile(vals, 0.99), 2),
                "max": round(max(vals), 2),
            }
        except Exception as e:
            log.warning("latency stats failed: %s", e)
            return {"count": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0}
    
    def check_regression(self, key: str, budget_ms: float, window_s: float = 60.0) -> tuple[bool, str]:
        """Check if p99 exceeds 2x budget. Returns (ok, reason)."""
        stats = self.get_stats(key, window_s)
        if stats["count"] < 10:
            return True, "insufficient samples"
        p99 = stats["p99"]
        threshold = budget_ms * 2
        if p99 > threshold:
            return False, f"regression: p99={p99:.1f}ms > 2*budget={threshold:.1f}ms (n={stats['count']})"
        return True, "ok"


# Global recorder instance
_recorder: Optional[LatencyRecorder] = None
_recorder_lock = threading.Lock()


def get_recorder() -> LatencyRecorder:
    """Get or create the global latency recorder."""
    global _recorder
    with _recorder_lock:
        if _recorder is None:
            _recorder = LatencyRecorder()
        return _recorder


def record_latency(key: str, dt_ms: float) -> None:
    """Convenience function to record latency."""
    get_recorder().record(key, dt_ms)


def check_latency_regression() -> dict:
    """Check all latency budgets for regression. Returns dict of results."""
    if not config.LATENCY_REGRESSION_GATE:
        return {"enabled": False}
    
    recorder = get_recorder()
    results = {}
    
    checks = [
        ("guard_tick", config.GUARD_TICK_BUDGET_MS),
        ("signal_eval", config.SIGNAL_EVAL_BUDGET_MS),
        ("order_place", config.ORDER_PLACE_BUDGET_MS),
        ("settlement_check", config.SETTLEMENT_BUDGET_MS),
    ]
    
    all_ok = True
    for key, budget in checks:
        ok, reason = recorder.check_regression(key, budget)
        results[key] = {"ok": ok, "reason": reason, "budget_ms": budget}
        if not ok:
            all_ok = False
    
    results["all_ok"] = all_ok
    return results


def emit_latency_report() -> None:
    """Emit a latency report to logs."""
    if not config.LATENCY_REGRESSION_GATE:
        return
    recorder = get_recorder()
    checks = [
        ("guard_tick", config.GUARD_TICK_BUDGET_MS),
        ("signal_eval", config.SIGNAL_EVAL_BUDGET_MS),
        ("order_place", config.ORDER_PLACE_BUDGET_MS),
        ("settlement_check", config.SETTLEMENT_BUDGET_MS),
    ]
    for key, budget in checks:
        stats = recorder.get_stats(key)
        if stats["count"] > 0:
            log.info("latency %s: n=%d p50=%.1fms p95=%.1fms p99=%.1fms max=%.1fms budget=%.1fms",
                     key, stats["count"], stats["p50"], stats["p95"], stats["p99"],
                     stats["max"], budget)


# Context manager for measuring
class measure:
    """Context manager to measure and record latency."""
    
    def __init__(self, key: str):
        self.key = key
        self.t0 = 0.0
    
    def __enter__(self):
        self.t0 = time.monotonic()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        dt_ms = (time.monotonic() - self.t0) * 1000.0
        record_latency(self.key, dt_ms)


if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.INFO)
    r = LatencyRecorder()
    import random
    for _ in range(100):
        r.record("test_key", random.uniform(10, 50))
    r.flush()
    print(r.get_stats("test_key"))
    print(r.check_regression("test_key", 25.0))