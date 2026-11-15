"""B5: order submission is idempotent; transient GETs are retried.

A POST that times out or returns 5xx/409 after the venue accepted the order
must not be repeated blindly: the retry matches recent orders by the
deterministic client_order_id (hash of ticker, side, window start, attempt
index) before retrying, and a 409 duplicate resolves to the existing order.
GETs (only) retry on connection errors and 502/503/504 with capped backoff.
"""
import time

import pytest
import requests
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import config

# trader.py parses argv at import time; give it a clean one first.
import sys
sys.argv = ["trader.py"]

import trader as trader_mod
from kalshi_client import KalshiClient, KalshiAPIError, KalshiDuplicateOrderError


def _make_client(monkeypatch):
    monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    with patch.object(KalshiClient, "_load_private_key") as m, \
         patch.object(KalshiClient, "_verify_key"):
        m.return_value = MagicMock()
        m.return_value.sign.return_value = b"fake_sig"
        client = KalshiClient()
    client._verified = True
    return client


def _resp(status, body=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    r.text = text
    r.headers = {}

    def _raise():
        if status >= 400:
            raise requests.HTTPError(f"{status} error")
    r.raise_for_status = _raise
    return r


def _sig(ticker="KXBTC15M-TEST", side="yes"):
    return SimpleNamespace(
        market=SimpleNamespace(ticker=ticker, series="KXBTC15M"),
        side=side, contracts=1, entry_price=95, dollar_risk=1.0, reason="test")


# ── deterministic client_order_id ─────────────────────────────────────

def test_coid_deterministic_per_intent():
    w = 12345
    a = trader_mod._coid("KXBTC15M-TEST", "yes", w, 0)
    assert a == trader_mod._coid("KXBTC15M-TEST", "yes", w, 0)   # same intent -> same id
    assert a != trader_mod._coid("KXBTC15M-TEST", "no", w, 0)     # side differs
    assert a != trader_mod._coid("KXBTC15M-OTHER", "yes", w, 0)   # ticker differs
    assert a != trader_mod._coid("KXBTC15M-TEST", "yes", w + 1, 0)  # window differs
    assert a != trader_mod._coid("KXBTC15M-TEST", "yes", w, 1)    # attempt differs
    assert a.startswith("sb_")


def test_coid_default_window_is_deterministic(monkeypatch):
    monkeypatch.setattr(trader_mod.time, "time", lambda: 1_700_000_000.0)
    assert trader_mod._coid("KXBTC15M-T", "yes") == trader_mod._coid("KXBTC15M-T", "yes")


# ── POST idempotency ──────────────────────────────────────────────────

def test_timed_out_post_followed_by_retry_produces_one_order():
    """POST times out after the venue accepted it; the retry adopts the existing order."""
    state = trader_mod.BotState()
    coid = trader_mod._coid("KXBTC15M-TEST", "yes", 12345)
    calls = {"post": 0}

    class FakeClient:
        def place_order(self, **kw):
            calls["post"] += 1
            raise requests.Timeout("post timed out")
        def get_orders(self, status="resting"):
            return [{"order_id": "O-EXISTING", "client_order_id": coid, "status": status}]

    out = state._place_order(FakeClient(), _sig(), coid)
    assert out is not None and out.get("order_id") == "O-EXISTING"
    # Retry (next cycle, same deterministic coid) must adopt the same order.
    out2 = state._place_order(FakeClient(), _sig(), coid)
    assert out2.get("order_id") == "O-EXISTING"


def test_post_409_duplicate_returns_existing_order():
    """A 409 from the venue resolves to the existing order, not a new submission."""
    state = trader_mod.BotState()
    coid = trader_mod._coid("KXBTC15M-TEST", "yes", 777)
    calls = {"post": 0}

    class FakeClient:
        def place_order(self, **kw):
            calls["post"] += 1
            raise KalshiDuplicateOrderError("409 duplicate")
        def get_orders(self, status="resting"):
            return [{"order_id": "O-DUP", "client_order_id": coid, "status": status}]

    out = state._place_order(FakeClient(), _sig(), coid)
    assert out is not None and out.get("order_id") == "O-DUP"


def test_post_5xx_then_retry_single_order():
    """POST 5xx with no match found -> one retry with the same coid, then adoption."""
    state = trader_mod.BotState()
    coid = trader_mod._coid("KXBTC15M-TEST", "yes", 555)
    calls = {"post": 0}

    class FakeClient:
        def place_order(self, **kw):
            calls["post"] += 1
            if calls["post"] == 1:
                raise KalshiAPIError("503 upstream")
            raise KalshiDuplicateOrderError("409 duplicate")
        def get_orders(self, status="resting"):
            if calls["post"] == 1:
                return []                       # original never landed (yet)
            return [{"order_id": "O-LATE", "client_order_id": coid}]

    out = state._place_order(FakeClient(), _sig(), coid)
    assert out is not None and out.get("order_id") == "O-LATE"
    assert calls["post"] == 2                   # retried exactly once


def test_post_failure_no_match_retries_then_gives_up():
    """No match anywhere and a failed retry -> None, and exactly one retry."""
    state = trader_mod.BotState()
    coid = trader_mod._coid("KXBTC15M-TEST", "yes", 998)
    calls = {"post": 0}

    class FakeClient:
        def place_order(self, **kw):
            calls["post"] += 1
            raise requests.ConnectionError("down")
        def get_orders(self, status="resting"):
            return []

    out = state._place_order(FakeClient(), _sig(), coid)
    assert out is None
    assert calls["post"] == 2                   # single retry, not a loop


# ── GET retries ───────────────────────────────────────────────────────

def test_get_503_twice_then_200_succeeds(monkeypatch):
    client = _make_client(monkeypatch)
    seq = [_resp(503), _resp(503), _resp(200, {"markets": [{"ticker": "KXBTC15M"}]})]
    req = MagicMock(side_effect=seq)
    monkeypatch.setattr(client.session, "request", req)
    out = client._request("GET", "/markets")
    assert out == {"markets": [{"ticker": "KXBTC15M"}]}
    assert req.call_count == 3


def test_get_connection_error_retried(monkeypatch):
    client = _make_client(monkeypatch)
    seq = [requests.ConnectionError("boom"), _resp(200, {"markets": []})]
    req = MagicMock(side_effect=seq)
    monkeypatch.setattr(client.session, "request", req)
    out = client._request("GET", "/markets")
    assert out == {"markets": []}
    assert req.call_count == 2


def test_get_503_gives_up_after_cap(monkeypatch):
    client = _make_client(monkeypatch)
    req = MagicMock(return_value=_resp(503, text="upstream"))
    monkeypatch.setattr(client.session, "request", req)
    with pytest.raises((KalshiAPIError, Exception)):
        client._request("GET", "/markets")
    assert req.call_count == 5                   # same cap as the 429 loop, GET only


def test_post_5xx_is_not_retried(monkeypatch):
    client = _make_client(monkeypatch)
    req = MagicMock(return_value=_resp(503, text="upstream"))
    monkeypatch.setattr(client.session, "request", req)
    with pytest.raises(Exception):
        client._request("POST", "/portfolio/events/orders", json={"ticker": "T"})
    assert req.call_count == 1                   # POSTs never auto-retry (idempotency is caller's job)


def test_post_409_raises_duplicate_error(monkeypatch):
    client = _make_client(monkeypatch)
    req = MagicMock(return_value=_resp(409, text="duplicate client_order_id"))
    monkeypatch.setattr(client.session, "request", req)
    with pytest.raises(KalshiDuplicateOrderError):
        client._request("POST", "/portfolio/events/orders", json={"ticker": "T"})
    assert req.call_count == 1
