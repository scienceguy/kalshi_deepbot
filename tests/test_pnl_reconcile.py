"""Issue #15 - session_pnl reconciliation across local state, n8n and Supabase.

Covers pnl_reconcile.reconcile_session_pnl / maybe_reconcile_session_pnl:
local-vs-reference repair, remote drift detection (Supabase bot_state,
Supabase pnl_ledger, n8n-reported orders.pnl_dollars), the ambiguous
zero-ledger guard, the batched-write repair path and the emit-on-drift alert.
"""

import json
import logging
from types import SimpleNamespace

import pytest

import config
import pnl_reconcile


class FakeLocalDB:
    def __init__(self, ledger_total=0.0):
        self.ledger_total = ledger_total
        self.batch_writes = []

    def get_total_realized_pnl(self, venue=""):
        return self.ledger_total

    def set_bot_state_batch(self, states):
        self.batch_writes.append(dict(states))
        return True


class FakeSupabase:
    def __init__(self, bot_state=None, ledger=None, orders=None):
        self._bot_state = bot_state
        self._ledger = ledger
        self._orders = orders

    def get_bot_state(self, key):
        return self._bot_state

    def get_pnl_ledger_total(self):
        return self._ledger

    def get_orders_pnl_total(self):
        return self._orders


class RaisingSupabase:
    def get_bot_state(self, key):
        raise RuntimeError("supabase down")

    def get_pnl_ledger_total(self):
        raise RuntimeError("supabase down")

    def get_orders_pnl_total(self):
        raise RuntimeError("supabase down")


class EmitRecorder:
    def __init__(self):
        self.events = []

    def __call__(self, source, event_type, payload, **kwargs):
        self.events.append({"source": source, "event_type": event_type,
                            "payload": payload, "kwargs": kwargs})


def _state(pnl):
    return SimpleNamespace(session_pnl=pnl, realized_pnl=pnl)


@pytest.fixture
def live_mode(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", False)


@pytest.fixture
def emit_recorder(monkeypatch):
    rec = EmitRecorder()
    monkeypatch.setattr("emit.emit_event", rec)
    return rec


def test_consistent_sources_no_repair(live_mode, emit_recorder):
    db = FakeLocalDB(ledger_total=10.0)
    sb = FakeSupabase(bot_state=10.0, ledger=10.0, orders=10.0)
    st = _state(10.0)
    res = pnl_reconcile.reconcile_session_pnl(st, local_db=db, supabase_client=sb)
    assert res.consistent is True
    assert res.corrected is False
    assert st.session_pnl == 10.0
    assert db.batch_writes == []
    assert emit_recorder.events == []


def test_local_state_drift_corrected_from_ledger(live_mode, emit_recorder):
    db = FakeLocalDB(ledger_total=3.5)
    st = _state(5.0)
    res = pnl_reconcile.reconcile_session_pnl(
        st, local_db=db, supabase_client=FakeSupabase(None, None, None))
    assert st.session_pnl == 3.5
    assert st.realized_pnl == 3.5
    assert res.corrected is True
    # corrected value is re-pushed through the batched write path
    assert len(db.batch_writes) == 1
    assert db.batch_writes[0]["session_pnl"] == pytest.approx(3.5)
    assert "pnl_reconcile_ts" in db.batch_writes[0]


def test_remote_bot_state_drift_triggers_batch_rewrite_and_alert(live_mode, emit_recorder):
    db = FakeLocalDB(ledger_total=10.0)
    sb = FakeSupabase(bot_state=8.0, ledger=10.0, orders=10.0)
    st = _state(10.0)
    res = pnl_reconcile.reconcile_session_pnl(st, local_db=db, supabase_client=sb)
    assert res.consistent is False
    assert db.batch_writes[0]["session_pnl"] == 10.0
    assert st.session_pnl == 10.0  # local state was already correct - not clobbered
    assert any(e["event_type"] == "reconcile.mismatch" for e in emit_recorder.events)


def test_n8n_orders_pnl_drift_alerts_but_keeps_local_state(live_mode, emit_recorder):
    db = FakeLocalDB(ledger_total=10.0)
    sb = FakeSupabase(bot_state=10.0, ledger=10.0, orders=7.25)
    st = _state(10.0)
    res = pnl_reconcile.reconcile_session_pnl(st, local_db=db, supabase_client=sb)
    assert res.consistent is False
    assert res.drift["n8n_orders_pnl"] == pytest.approx(-2.75)
    assert st.session_pnl == 10.0
    mismatch = [e for e in emit_recorder.events if e["event_type"] == "reconcile.mismatch"]
    assert mismatch and mismatch[0]["payload"]["context"] == "session_pnl"


def test_ambiguous_zero_ledger_does_not_zero_local_state(live_mode):
    db = FakeLocalDB(ledger_total=0.0)
    st = _state(4.0)
    res = pnl_reconcile.reconcile_session_pnl(
        st, local_db=db, supabase_client=FakeSupabase(None, None, None))
    assert st.session_pnl == 4.0
    assert res.corrected is False
    assert any("not auto-corrected" in n for n in res.notes)


def test_remote_errors_are_treated_as_unknown_not_zero(live_mode):
    db = FakeLocalDB(ledger_total=10.0)
    st = _state(10.0)
    res = pnl_reconcile.reconcile_session_pnl(st, local_db=db, supabase_client=RaisingSupabase())
    assert res.remote_bot_state is None
    assert res.remote_ledger is None
    assert res.remote_orders is None
    assert res.consistent is True


def test_local_reference_db_error_skips_correction(live_mode, emit_recorder):
    """A transient ledger read failure must not zero session P&L."""

    class BrokenDB(FakeLocalDB):
        def get_total_realized_pnl(self, venue=""):
            raise RuntimeError("db down")

    st = _state(4.0)
    res = pnl_reconcile.reconcile_session_pnl(
        st, local_db=BrokenDB(), supabase_client=FakeSupabase(5.0, 5.0, 5.0))
    assert res.reference is None
    assert st.session_pnl == 4.0        # left untouched
    assert res.corrected is False
    assert res.consistent is True     # unknown is not a mismatch
    assert any("reference unavailable" in n for n in res.notes)
    assert emit_recorder.events == []  # no spurious drift alert


def test_drift_emits_one_event_per_source(live_mode, emit_recorder):
    """Each drifting remote source gets its own source-keyed mismatch event."""
    db = FakeLocalDB(ledger_total=10.0)
    sb = FakeSupabase(bot_state=8.0, ledger=10.0, orders=7.25)
    st = _state(10.0)
    pnl_reconcile.reconcile_session_pnl(st, local_db=db, supabase_client=sb)
    mismatch = [e for e in emit_recorder.events if e["event_type"] == "reconcile.mismatch"]
    sources = {e["payload"]["source"] for e in mismatch}
    assert sources == {"supabase_bot_state", "n8n_orders_pnl"}
    assert all(e["payload"]["context"] == "session_pnl" for e in mismatch)
    keys = [e["kwargs"]["key"] for e in mismatch]
    assert any("supabase_bot_state" in k for k in keys)
    assert any("n8n_orders_pnl" in k for k in keys)


def test_dry_run_reference_uses_paper_mode(monkeypatch, emit_recorder):
    monkeypatch.setattr(config, "DRY_RUN", True)
    import paper_mode
    monkeypatch.setattr(paper_mode, "pnl", lambda: 2.5)
    db = FakeLocalDB(ledger_total=99.0)  # ledger must be ignored in dry-run
    st = _state(0.0)
    res = pnl_reconcile.reconcile_session_pnl(
        st, local_db=db, supabase_client=FakeSupabase(None, None, None))
    assert res.reference == 2.5
    assert st.session_pnl == 2.5


def test_emit_failure_does_not_raise(live_mode, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("outbox down")
    monkeypatch.setattr("emit.emit_event", _boom)
    db = FakeLocalDB(ledger_total=10.0)
    sb = FakeSupabase(bot_state=None, ledger=5.0, orders=None)
    st = _state(10.0)
    res = pnl_reconcile.reconcile_session_pnl(st, local_db=db, supabase_client=sb)
    assert res.consistent is False  # drift still reported despite emit failure


def test_maybe_reconcile_throttles(live_mode):
    db = FakeLocalDB(ledger_total=1.0)
    st = _state(1.0)
    kw = dict(local_db=db, supabase_client=FakeSupabase(None, None, None),
              interval_s=600.0, emit=False)
    first = pnl_reconcile.maybe_reconcile_session_pnl(st, now=1000.0, **kw)
    assert first is not None
    assert pnl_reconcile.maybe_reconcile_session_pnl(st, now=1300.0, **kw) is None
    assert pnl_reconcile.maybe_reconcile_session_pnl(st, now=1700.0, **kw) is not None


def test_dry_run_ambiguous_zero_does_not_zero_local_state(monkeypatch):
    """A zero paper ledger must not overwrite a non-zero dry-run state."""
    monkeypatch.setattr(config, "DRY_RUN", True)
    import paper_mode
    monkeypatch.setattr(paper_mode, "pnl", lambda: 0.0)
    db = FakeLocalDB(ledger_total=0.0)
    st = _state(4.0)
    res = pnl_reconcile.reconcile_session_pnl(
        st, local_db=db, supabase_client=FakeSupabase(None, None, None))
    assert st.session_pnl == 4.0
    assert res.corrected is False
    assert any("not auto-corrected" in n for n in res.notes)


def test_reconcile_due_gates_hot_path_and_never_raises():
    st = _state(1.0)
    assert pnl_reconcile.reconcile_due(st, interval_s=600.0, now=1000.0) is True
    st._last_pnl_reconcile_ts = 1000.0
    assert pnl_reconcile.reconcile_due(st, interval_s=600.0, now=1300.0) is False
    assert pnl_reconcile.reconcile_due(st, interval_s=600.0, now=1601.0) is True
    # junk timestamp / junk state must not raise - hot loop depends on it
    st._last_pnl_reconcile_ts = "junk"
    assert pnl_reconcile.reconcile_due(st, interval_s=600.0, now=1601.0) is True
    assert pnl_reconcile.reconcile_due(object(), interval_s=600.0, now=1601.0) is True


def test_remote_client_warns_when_configured_but_disabled(monkeypatch, caplog):
    import supabase_client
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)

    class DisabledClient:
        enabled = False

    monkeypatch.setattr(supabase_client, "get_supabase", lambda: DisabledClient())
    with caplog.at_level(logging.WARNING, logger="pnl_reconcile"):
        client = pnl_reconcile._remote_client(None)
    assert client is None
    assert any("disabled" in r.message for r in caplog.records)


def test_remote_client_warns_when_construction_fails(monkeypatch, caplog):
    import supabase_client
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: True)

    def _boom():
        raise RuntimeError("key file unreadable")

    monkeypatch.setattr(supabase_client, "get_supabase", _boom)
    with caplog.at_level(logging.WARNING, logger="pnl_reconcile"):
        client = pnl_reconcile._remote_client(None)
    assert client is None
    assert any("creation failed" in r.message for r in caplog.records)


def test_remote_client_not_configured_is_quiet(monkeypatch, caplog):
    import supabase_client
    monkeypatch.setattr(supabase_client, "is_enabled", lambda: False)
    with caplog.at_level(logging.WARNING, logger="pnl_reconcile"):
        client = pnl_reconcile._remote_client(None)
    assert client is None
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_all_remote_reads_failing_warns(live_mode, caplog):
    db = FakeLocalDB(ledger_total=10.0)
    st = _state(10.0)
    with caplog.at_level(logging.WARNING, logger="pnl_reconcile"):
        res = pnl_reconcile.reconcile_session_pnl(
            st, local_db=db, supabase_client=RaisingSupabase())
    assert res.consistent is True  # unknown is not drift
    assert any("all remote reads failed" in r.message for r in caplog.records)


def test_supabase_totals_paginate_beyond_one_page():
    import supabase_client

    class PagedClient(supabase_client.SupabaseClient):
        def __init__(self):
            self.paths = []

        def _request(self, method, path, *a, **kw):  # noqa: A002
            self.paths.append(path)
            if path.endswith("offset=0"):
                return [{"realized_pnl_dollars": 1.0}, {"realized_pnl_dollars": 2.0}]
            if path.endswith("offset=2"):
                return [{"realized_pnl_dollars": 3.5}]
            return []

    client = PagedClient()
    rows = client._select_all_rows("pnl_ledger?select=realized_pnl_dollars",
                                   page_size=2, max_rows=100)
    assert [r["realized_pnl_dollars"] for r in rows] == [1.0, 2.0, 3.5]
    assert len(client.paths) == 2


def test_supabase_totals_warn_at_row_cap(caplog):
    import supabase_client

    class AlwaysFullClient(supabase_client.SupabaseClient):
        def _request(self, method, path, *a, **kw):  # noqa: A002
            return [{"realized_pnl_dollars": 1.0}, {"realized_pnl_dollars": 1.0}]

    client = AlwaysFullClient()
    with caplog.at_level(logging.WARNING, logger="supabase_client"):
        rows = client._select_all_rows("pnl_ledger?select=realized_pnl_dollars",
                                       page_size=2, max_rows=4)
    assert len(rows) == 4  # capped, not infinite
    assert any("cap" in r.message for r in caplog.records)


def test_supabase_totals_handle_read_failure_after_first_page():
    import supabase_client

    class FailsOnSecondPage(supabase_client.SupabaseClient):
        def __init__(self):
            self.calls = 0

        def _request(self, method, path, *a, **kw):  # noqa: A002
            self.calls += 1
            if self.calls == 1:
                return [{"realized_pnl_dollars": 1.0}, {"realized_pnl_dollars": 2.0}]
            return None  # read failure mid-pagination

    client = FailsOnSecondPage()
    assert client._select_all_rows("pnl_ledger?select=realized_pnl_dollars",
                                   page_size=2, max_rows=100) is None


def test_to_dict_is_json_serializable(live_mode):
    db = FakeLocalDB(ledger_total=1.5)
    st = _state(1.5)
    res = pnl_reconcile.reconcile_session_pnl(
        st, local_db=db, supabase_client=FakeSupabase(1.5, 1.5, 1.5), emit=False)
    json.dumps(res.to_dict())


def test_supabase_totals_handle_disabled_client(monkeypatch):
    """A disabled client (no credentials) must mean unknown, not $0.00."""

    import supabase_client

    _strip_supabase_credentials(monkeypatch)
    client = supabase_client.SupabaseClient()
    assert client.enabled is False
    assert client.get_pnl_ledger_total() is None
    assert client.get_orders_pnl_total() is None


class _FakeResponse:
    def __init__(self, rows):
        self.status_code = 200
        self.content = json.dumps(rows).encode()
        self.text = self.content.decode()

    def json(self):
        return json.loads(self.content)


def _strip_supabase_credentials(monkeypatch):
    """Force _load_credentials() to find nothing (module-level key files)."""
    import supabase_client
    monkeypatch.setattr(supabase_client, "SUPABASE_KEY_FILE", "")
    monkeypatch.setattr(supabase_client, "SUPABASE_SERVICE_KEY_FILE", "")
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_supabase_totals_sum_rows(monkeypatch):
    """Totals sum the remote rows and read the expected endpoints."""
    import supabase_client

    _strip_supabase_credentials(monkeypatch)
    # A fresh client would be disabled without credentials; enable it and
    # stub the transport so no network is touched.
    monkeypatch.setattr(supabase_client, "_SUPABASE_SKIP_UNTIL", 0.0)
    monkeypatch.setattr(supabase_client, "_SUPABASE_FAILURES", 0)
    client = supabase_client.SupabaseClient()
    client.url = "http://supabase.test"
    client.key = "test-key"
    client.enabled = True

    calls = []

    def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):  # noqa: A002
        calls.append({"method": method, "url": url, "params": params})
        # Pagination adds order, limit=1000, offset=0
        if url.startswith("http://supabase.test/rest/v1/pnl_ledger?select=realized_pnl_dollars"):
            return _FakeResponse([{"realized_pnl_dollars": 1.5},
                                  {"realized_pnl_dollars": "2.25"},
                                  {"realized_pnl_dollars": None}])
        if url.startswith("http://supabase.test/rest/v1/orders?select=pnl_dollars&pnl_dollars=not.is.null"):
            return _FakeResponse([{"pnl_dollars": 10}, {"pnl_dollars": -4.5}])
        raise AssertionError(f"unexpected request URL: {url}")

    monkeypatch.setattr(client.session, "request", _fake_request)

    assert client.get_pnl_ledger_total() == 3.75
    assert client.get_orders_pnl_total() == 5.5
    assert [c["method"] for c in calls] == ["GET", "GET"]
    assert calls[0]["url"].startswith("http://supabase.test/rest/v1/pnl_ledger?")
    assert calls[1]["url"].startswith("http://supabase.test/rest/v1/orders?")
