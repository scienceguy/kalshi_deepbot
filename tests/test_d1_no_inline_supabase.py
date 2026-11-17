"""D1 — no inline Supabase writes in the trading cycle.

Acceptance (AGENT_TASKS_v7.md D1):
- cycle() with a Supabase client that raises or sleeps 5 seconds does not
  slow or fail the cycle (assert elapsed under a small bound);
- each order produces exactly one remote upsert after sync;
- no direct Supabase call remains in trader.py outside the sync module
  (grep test).
"""

import inspect
import time
from unittest.mock import MagicMock

import pytest

import local_db
import supabase_client
import trader
from local_db import LocalDB


def test_trader_source_has_no_inline_supabase_calls():
    """grep test: trader.py holds no direct Supabase client call sites."""
    src = inspect.getsource(trader)
    assert "get_supabase_client()" not in src
    for banned in ("supabase_client.insert_", "supabase_client.set_bot_state",
                   "supabase_client._request", "sb.enabled", "sb.health_check()"):
        assert banned not in src, banned
    # the only sanctioned module use: enabled check + startup probe + raw reads
    assert "supabase_client.is_enabled()" in src
    assert "supabase_client.validate_at_startup()" in src


def test_order_recorded_exactly_once_locally():
    """Order recording writes the order row exactly once (no duplicate insert)."""
    src = inspect.getsource(trader.BotState._record_filled_order)
    assert src.count(".insert_order(") == 1
    assert "get_supabase_client" not in src


@pytest.fixture
def cycle_env(monkeypatch):
    from test_trader import _mkts, _reset_cycle_state
    monkeypatch.setattr(trader, "find_active_markets", lambda c: _mkts())
    monkeypatch.setattr(trader, "get_local_db", lambda: MagicMock())
    monkeypatch.setattr(trader, "get_n8n_client", lambda: MagicMock())
    monkeypatch.setattr(supabase_client.SupabaseClient, "_request",
                        lambda self, *a, **kw: (_ for _ in ()).throw(
                            AssertionError("remote Supabase call in cycle")))
    monkeypatch.setattr(trader.report_state, "_get_state_db", lambda: MagicMock())
    monkeypatch.setattr(trader.report_state, "_get_n8n", lambda: None)
    monkeypatch.setattr(trader.config, "LLM_EVALUATION_ENABLED", False)
    _reset_cycle_state(monkeypatch)


def test_cycle_unaffected_by_sleeping_supabase_client(cycle_env, monkeypatch):
    """A Supabase client that sleeps 5s per call cannot slow the cycle."""
    def _sleeping(self, *a, **kw):
        time.sleep(5)
        return {}
    monkeypatch.setattr(supabase_client.SupabaseClient, "_request", _sleeping)
    client = MagicMock()
    client.get_balance.return_value = 100.0
    client.get_positions.return_value = []
    client.get_orders.return_value = []
    client.invalidate_markets.return_value = None
    client.get_market.return_value = {"expiration_value": 1.0}
    t0 = time.perf_counter()
    trader.cycle(client)
    assert time.perf_counter() - t0 < 3.0


def test_cycle_unaffected_by_raising_supabase_client(cycle_env, monkeypatch):
    """A Supabase client that raises on every call cannot fail the cycle."""
    def _boom(self, *a, **kw):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(supabase_client.SupabaseClient, "_request", _boom)
    client = MagicMock()
    client.get_balance.return_value = 100.0
    client.get_positions.return_value = []
    client.get_orders.return_value = []
    client.invalidate_markets.return_value = None
    client.get_market.return_value = {"expiration_value": 1.0}
    trader.cycle(client)  # must not raise


def test_order_syncs_to_remote_exactly_once(tmp_path, monkeypatch):
    """One local order row -> one remote upsert; a second sync sends nothing."""
    db = LocalDB(db_path=str(tmp_path / "d1.db"), auto_sync=False)
    payload = {"ts": "2026-11-17T00:00:00+00:00", "ticker": "KXBTC15M-1",
               "side": "yes", "action": "buy", "contracts": 2,
               "price_cents": 86, "order_id": "ord-d1", "status": "resting",
               "fees_dollars": 0.01, "cycles": 1}
    assert db.insert_order(payload) is True

    fake = MagicMock()
    fake.insert_order.return_value = True
    fake.insert_signal.return_value = True
    fake.set_bot_state.return_value = True
    fake.insert_pnl_ledger.return_value = True
    fake.insert_backtest_run.return_value = True
    monkeypatch.setattr(local_db, "supabase_is_enabled", lambda: True)

    stats = db.sync_to_supabase(fake)
    assert stats["orders"] == 1
    assert fake.insert_order.call_count == 1
    sent = fake.insert_order.call_args[0][0]
    assert sent["order_id"] == "ord-d1"

    # row is marked synced: a second sync round trips nothing
    assert db.sync_to_supabase(fake)["orders"] == 0
    assert fake.insert_order.call_count == 1


def test_signal_upsert_uses_deterministic_id(monkeypatch):
    """insert_signal computes a stable signal_id and upserts on it."""
    client = supabase_client.SupabaseClient()
    calls = []

    def fake_request(method, path, data=None, params=None,
                     extra_headers=None, max_retries=3):
        calls.append({"method": method, "path": path, "data": data,
                      "params": params, "extra_headers": extra_headers})
        return {}

    client._request = fake_request
    payload = {"ts": "2026-11-17T00:00:00+00:00", "ticker": "KXBTC15M-1",
               "side": "yes", "price_cents": 86, "contracts": 2, "type": "taker"}
    assert client.insert_signal(payload) is True
    first = calls[-1]
    assert first["params"] == {"on_conflict": "signal_id"}
    assert first["extra_headers"] == {"Prefer": "resolution=merge-duplicates"}
    sig_id = first["data"]["signal_id"]
    assert len(sig_id) == 32

    # deterministic: same ticker/ts/type -> same id
    calls.clear()
    client.insert_signal(dict(payload))
    assert calls[-1]["data"]["signal_id"] == sig_id
    # different type -> different id
    calls.clear()
    other = dict(payload, type="maker")
    client.insert_signal(other)
    assert calls[-1]["data"]["signal_id"] != sig_id


def test_signal_upsert_falls_back_when_remote_lacks_column(monkeypatch):
    """Before the signal_id migration is applied remotely, insert still works."""
    client = supabase_client.SupabaseClient()
    calls = []

    def fake_request(method, path, data=None, params=None,
                     extra_headers=None, max_retries=3):
        calls.append({"data": data, "params": params})
        # first call behaves like the unmigrated remote: 400 -> None
        return None if len(calls) == 1 else {}

    client._request = fake_request
    payload = {"ts": "2026-11-17T00:00:00+00:00", "ticker": "KXBTC15M-2",
               "side": "no", "price_cents": 15, "contracts": 1, "type": "taker"}
    assert client.insert_signal(payload) is True
    assert len(calls) == 2
    assert calls[0]["params"] == {"on_conflict": "signal_id"}
    assert "signal_id" not in calls[1]["data"]          # plain fallback insert
    assert calls[1]["params"] is None


def test_signal_missing_required_fields_still_rejected(monkeypatch):
    client = supabase_client.SupabaseClient()
    calls = []
    client._request = lambda *a, **kw: calls.append(1) or {}
    assert client.insert_signal({"ticker": "T", "side": "yes"}) is False
    assert calls == []
