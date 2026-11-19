"""Issue #15 - session_pnl reconciliation across local state, n8n and Supabase.

Covers pnl_reconcile.reconcile_session_pnl / maybe_reconcile_session_pnl:
local-vs-reference repair, remote drift detection (Supabase bot_state,
Supabase pnl_ledger, n8n-reported orders.pnl_dollars), the ambiguous
zero-ledger guard, the batched-write repair path and the emit-on-drift alert.
"""

import json
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


def test_to_dict_is_json_serializable(live_mode):
    db = FakeLocalDB(ledger_total=1.5)
    st = _state(1.5)
    res = pnl_reconcile.reconcile_session_pnl(
        st, local_db=db, supabase_client=FakeSupabase(1.5, 1.5, 1.5), emit=False)
    json.dumps(res.to_dict())


def test_supabase_totals_handle_disabled_client():
    """A disabled/None _request result must mean unknown, not $0.00."""

    class DisabledClient:
        url = "http://localhost"
        key = "x"

        def _request(self, *a, **kw):
            return None

    import supabase_client
    client = DisabledClient()
    assert supabase_client.SupabaseClient.get_pnl_ledger_total(client) is None
    assert supabase_client.SupabaseClient.get_orders_pnl_total(client) is None


def test_supabase_totals_sum_rows():
    import supabase_client

    class FakeClient(supabase_client.SupabaseClient):
        def __init__(self, rows):
            self._rows = rows

        def _request(self, method, path, *a, **kw):  # noqa: A002
            return self._rows

    client = FakeClient([{"realized_pnl_dollars": 1.5}, {"realized_pnl_dollars": "2.25"},
                         {"realized_pnl_dollars": None}])
    assert client.get_pnl_ledger_total() == 3.75
    client = FakeClient([{"pnl_dollars": 10}, {"pnl_dollars": -4.5}])
    assert client.get_orders_pnl_total() == 5.5
