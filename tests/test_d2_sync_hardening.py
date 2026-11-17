"""Tests for the sync engine hardening: circuit breaker, per-row retry,
persisted backoff + quarantine, sync metrics.

Tests use a fake Supabase client and a LocalDB with auto_sync=False so
the background thread never interferes.
"""

import time
from unittest.mock import patch

import local_db
import pytest


class _FailAlwaysSupabase:
    """Supabase client that always fails (returns False or raises)."""

    def __init__(self, fail_mode="false"):
        self.calls = []
        self.fail_mode = fail_mode  # "false" | "raise"

    def insert_signal(self, data):
        self.calls.append(("signals", data))
        if self.fail_mode == "raise":
            raise RuntimeError("Supabase down")
        return False

    def insert_order(self, data):
        self.calls.append(("orders", data))
        if self.fail_mode == "raise":
            raise RuntimeError("Supabase down")
        return False

    def insert_pnl_ledger(self, data):
        self.calls.append(("pnl_ledger", data))
        if self.fail_mode == "raise":
            raise RuntimeError("Supabase down")
        return False

    def set_bot_state(self, key, value):
        self.calls.append(("bot_state", key, value))
        if self.fail_mode == "raise":
            raise RuntimeError("Supabase down")
        return False

    def insert_backtest_run(self, params, metrics):
        self.calls.append(("backtest_runs", params, metrics))
        if self.fail_mode == "raise":
            raise RuntimeError("Supabase down")
        return False


class _FailOnceThenSucceedSupabase:
    """Supabase client that fails once per table, then succeeds."""

    def __init__(self):
        self.calls = []
        self._failed = set()

    def _should_fail(self, table):
        if table in self._failed:
            self._failed.discard(table)
            return True
        return False

    def insert_signal(self, data):
        self.calls.append(("signals", data))
        if self._should_fail("signals"):
            return False
        return True

    def insert_order(self, data):
        self.calls.append(("orders", data))
        if self._should_fail("orders"):
            return False
        return True

    def insert_pnl_ledger(self, data):
        self.calls.append(("pnl_ledger", data))
        if self._should_fail("pnl_ledger"):
            return False
        return True

    def set_bot_state(self, key, value):
        self.calls.append(("bot_state", key, value))
        if self._should_fail("bot_state"):
            return False
        return True

    def insert_backtest_run(self, params, metrics):
        self.calls.append(("backtest_runs", params, metrics))
        if self._should_fail("backtest_runs"):
            return False
        return True


def _db(tmp_path):
    db = local_db.LocalDB(
        db_path=str(tmp_path / "t.db"), auto_sync=False
    )
    return db


def _rewind_backoff(db):
    """Simulate wall-clock passing so a backoff row is retried next cycle."""
    conn = db._get_conn()  # noqa: SLF001
    conn.execute("UPDATE orders SET next_attempt_at = NULL")
    conn.commit()


def _insert_order(db, order_id="oid1", ticker="KXBTC15M"):
    conn = db._get_conn()  # noqa: SLF001
    conn.execute(
        "INSERT INTO orders (ts, ticker, side, contracts, price_cents,"
        " order_id, status, fees_dollars, cycles) VALUES"
        " ('2026-11-12T00:00:00Z',?, 'yes',1,90,?,'resting',0.0,0)",
        (ticker, order_id),
    )
    conn.commit()


# ── Circuit breaker ──────────────────────────────────────────────────

@patch("local_db.supabase_is_enabled", return_value=True)
def test_circuit_breaker_opens_after_n_failures(mock_enabled, tmp_path):
    """Breaker opens after SYNC_BREAKER_MAX_FAILURES consecutive failures."""
    db = _db(tmp_path)
    fake = _FailAlwaysSupabase(fail_mode="false")

    # Insert a row so there's something to sync
    _insert_order(db)

    # Sync max_failures times — each sync where all rows fail trips breaker.
    # The backoff window is rewound between cycles (tests are faster than
    # wall clock; production spaces these by 30s intervals).
    for _ in range(local_db.SYNC_BREAKER_MAX_FAILURES):
        db.sync_to_supabase(fake)
        _rewind_backoff(db)

    # Breaker should now be open
    assert not db._sync_breaker_closed(), "breaker should be open"
    health = db.get_sync_health()
    assert health["breaker_open"] is True
    assert health["breaker_opens"] >= 1
    assert health["breaker_state"] == "open"


@patch("local_db.supabase_is_enabled", return_value=True)
def test_circuit_breaker_skips_sync_when_open(mock_enabled, tmp_path):
    """When breaker is open, sync_to_supabase returns all zeros without calling Supabase."""
    db = _db(tmp_path)
    fake = _FailAlwaysSupabase(fail_mode="false")
    _insert_order(db)

    # Trip the breaker
    for _ in range(local_db.SYNC_BREAKER_MAX_FAILURES):
        db.sync_to_supabase(fake)
        _rewind_backoff(db)
    call_count_before = len(fake.calls)

    # Sync again — should skip entirely
    result = db.sync_to_supabase(fake)
    assert all(v == 0 for v in result.values()), f"expected all zeros, got {result}"
    assert len(fake.calls) == call_count_before, "Supabase should not be called when breaker is open"


@patch("local_db.supabase_is_enabled", return_value=True)
def test_circuit_breaker_allows_half_open_probe_after_cooldown(mock_enabled, tmp_path):
    """After cooldown expires, _sync_breaker_closed() allows a half-open probe."""
    db = _db(tmp_path)
    fake = _FailAlwaysSupabase(fail_mode="false")
    _insert_order(db)

    # Trip the breaker
    for _ in range(local_db.SYNC_BREAKER_MAX_FAILURES):
        db.sync_to_supabase(fake)
        _rewind_backoff(db)
    assert not db._sync_breaker_closed()

    # Simulate cooldown expired
    db._sync_breaker_open_until = time.time() - 1.0
    assert db._sync_breaker_closed()  # half-open: breaker closed, failures reset
    assert db._sync_breaker_failures == 0
    assert db._sync_breaker_open_until == 0.0
    health = db.get_sync_health()
    assert health["breaker_open"] is False


# ── Per-row retry ────────────────────────────────────────────────────

@patch("local_db.supabase_is_enabled", return_value=True)
def test_per_row_retries_after_transient_failure(mock_enabled, tmp_path):
    """A row that fails once is retried on the next sync cycle."""
    db = _db(tmp_path)
    fake = _FailOnceThenSucceedSupabase()
    fake._failed.add("orders")  # fail first orders insert

    _insert_order(db)

    # First sync: order fails (1 call), attempts incremented
    result = db.sync_to_supabase(fake)
    assert result["orders"] == 0
    assert len(fake.calls) == 1

    # Row still unsynced
    conn = db._get_conn()
    synced = conn.execute("SELECT synced FROM orders WHERE order_id = 'oid1'").fetchone()[0]
    assert synced == 0

    # Second sync: order succeeds (1 more call) once the backoff window
    # has passed (rewound here; production waits it out on the 30s cycle)
    _rewind_backoff(db)
    result = db.sync_to_supabase(fake)
    assert result["orders"] == 1
    assert len(fake.calls) == 2

    # Row now synced
    synced = conn.execute("SELECT synced FROM orders WHERE order_id = 'oid1'").fetchone()[0]
    assert synced == 1


# ── Dead letters ─────────────────────────────────────────────────────

@patch("local_db.supabase_is_enabled", return_value=True)
def test_poison_row_quarantined_after_max_attempts(mock_enabled, tmp_path, monkeypatch):
    """Rows exceeding SYNC_MAX_ROW_ATTEMPTS move to sync_quarantine."""
    # The breaker would open after 5 all-fail batches and stop selecting the
    # row before it reaches the row-attempt cap; raise it so the row's own
    # retry cap is what quarantines it.
    monkeypatch.setattr(local_db, "SYNC_BREAKER_MAX_FAILURES", 1000)
    db = _db(tmp_path)
    fake = _FailAlwaysSupabase(fail_mode="false")
    _insert_order(db)

    max_attempts = local_db.SYNC_MAX_ROW_ATTEMPTS

    # Sync max_attempts times — each time the row fails and attempts increment
    for i in range(max_attempts):
        db.sync_to_supabase(fake)
        _rewind_backoff(db)

    # After max_attempts failures the row is quarantined (moved out of the
    # source table), so later syncs never call Supabase for it again.
    conn = db._get_conn()  # noqa: SLF001
    qrow = conn.execute("SELECT pk, attempts, row_json FROM sync_quarantine").fetchone()
    assert qrow is not None, "poison row should be in sync_quarantine"
    assert qrow["attempts"] == max_attempts
    assert "oid1" in qrow["row_json"]
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    call_count_before = len(fake.calls)
    db.sync_to_supabase(fake)
    assert len(fake.calls) == call_count_before, "quarantined row should not be retried"

    # Health shows quarantined rows tracked
    health = db.get_sync_health()
    assert health["quarantined_rows"] >= 1


# ── Sync metrics ─────────────────────────────────────────────────────

def test_sync_health_returns_metrics(tmp_path):
    """get_sync_health() returns sync metrics and breaker state."""
    db = _db(tmp_path)

    health_before = db.get_sync_health()
    assert "sync_success_count" in health_before
    assert "sync_failure_count" in health_before
    assert "last_sync_ts" in health_before
    assert "breaker_opens" in health_before
    assert "breaker_state" in health_before
    assert health_before["breaker_state"] == "closed"
    assert health_before["breaker_open"] is False


@patch("local_db.supabase_is_enabled", return_value=True)
def test_sync_health_tracks_success_and_failure(mock_enabled, tmp_path):
    """Sync metrics track both successful and failed syncs."""
    db = _db(tmp_path)
    fake = _FailOnceThenSucceedSupabase()
    fake._failed.add("orders")  # fail once, then succeed
    _insert_order(db)

    # First sync: failure (breaker not open yet since 1 < max_failures)
    db.sync_to_supabase(fake)
    h1 = db.get_sync_health()
    failures_after_first = h1["sync_failure_count"]

    # Second sync: success (backoff rewound; production waits it out)
    _rewind_backoff(db)
    db.sync_to_supabase(fake)
    h2 = db.get_sync_health()

    assert h2["sync_success_count"] > h1["sync_success_count"], "success count should increase"
    assert h2["sync_failure_count"] >= failures_after_first, "failure count should not decrease"
