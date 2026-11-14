"""
Tests for polymarket_ws.py — auto-reconnecting Polymarket US WebSocket worker.

All tests fake the websockets connection and auth signer; no real network or
credentials are used.
"""

import asyncio
import json
import types
from pathlib import Path
from unittest.mock import patch

import pytest

import polymarket_ws
from polymarket_ws import (
    DEFAULT_SUB_TYPE,
    FULL_SUB_TYPE,
    PolymarketWSError,
    PolymarketWSWorker,
    _extract_quote,
)


class FakeConnectionClosed(Exception):
    def __init__(self, code, reason):
        self.code = code
        self.reason = reason
        super().__init__(reason)


class FakeInvalidStatus(Exception):
    def __init__(self, status_code, message=""):
        self.status_code = status_code
        self.message = message
        super().__init__(message or str(status_code))


class FakeConnection:
    """Fake async-context-manager websocket connection."""

    def __init__(self, messages=None, raise_on_enter=None):
        self.messages = list(messages or [])
        self.raise_on_enter = raise_on_enter
        self.sent = []  # raw JSON strings sent
        self.closed = False

    async def __aenter__(self):
        if self.raise_on_enter is not None:
            raise self.raise_on_enter
        return self

    async def __aexit__(self, *a):
        self.closed = True
        return False

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        raise FakeConnectionClosed(1000, "closed")

    async def send(self, data):
        self.sent.append(data)

    async def close(self, *a, **k):
        self.closed = True


class FakeWebsockets:
    """Fake `websockets` module; `connect()` pops connections from a queue."""

    ConnectionClosed = FakeConnectionClosed
    InvalidStatus = FakeInvalidStatus

    def __init__(self):
        self.queue = []
        self.connect_kwargs = None

    def connect(self, url, **kwargs):
        self.connect_kwargs = kwargs
        if self.queue:
            return self.queue.pop(0)
        return FakeConnection()


# ── LITE market ─────────────────────────────────────────────────────────


LITE_MSG = json.dumps({
    "requestId": "bbo-1",
    "subscriptionType": DEFAULT_SUB_TYPE,
    "marketDataLite": {
        "marketSlug": "cpc-btc-updown-15m-xyz",
        "currentPx": {"value": "0.55", "currency": "USD"},
        "lastTradePx": {"value": "0.55", "currency": "USD"},
        "bestBid": {"value": "0.54", "currency": "USD"},
        "bestAsk": {"value": "0.56", "currency": "USD"},
        "bidDepth": 5,
        "askDepth": 4,
        "sharesTraded": "150000",
        "openInterest": "500000",
    },
})

FULL_MSG = json.dumps({
    "requestId": "md-1",
    "subscriptionType": FULL_SUB_TYPE,
    "marketData": {
        "marketSlug": "cpc-btc-updown-15m-xyz",
        "bids": [{"px": {"value": "0.545", "currency": "USD"}, "qty": "2.50"}],
        "offers": [{"px": {"value": "0.555", "currency": "USD"}, "qty": "1.50"}],
        "state": "MARKET_STATE_OPEN",
        "stats": {
            "lastTradePx": {"value": "0.55", "currency": "USD"},
            "sharesTraded": "150000",
            "openInterest": "500000",
            "highPx": {"value": "0.58", "currency": "USD"},
            "lowPx": {"value": "0.52", "currency": "USD"},
        },
        "transactTime": "2026-11-14T10:30:00Z",
    },
})


def make_worker(key_id="kid", secret_key="secret", **kwargs):
    return PolymarketWSWorker(
        key_id=key_id, secret_key=secret_key, market_slugs=["m1"], **kwargs
    )


# ── credential resolution ──────────────────────────────────────────────


def test_credentials_from_env(monkeypatch):
    monkeypatch.setenv("POLYMARKET_KEY_ID", "env-key")
    monkeypatch.setenv("POLYMARKET_SECRET_KEY", "env-secret")
    w = make_worker(key_id=None, secret_key=None)
    assert w._key_id == "env-key"
    assert w._secret_key == "env-secret"


def test_credentials_explicit_beat_env(monkeypatch):
    monkeypatch.setenv("POLYMARKET_KEY_ID", "env-key")
    monkeypatch.setenv("POLYMARKET_SECRET_KEY", "env-secret")
    w = make_worker(key_id="explicit-key", secret_key="explicit-secret")
    assert w._key_id == "explicit-key"
    assert w._secret_key == "explicit-secret"


# ── message extraction ─────────────────────────────────────────────────


def test_extract_quote_lite():
    q = _extract_quote(json.loads(LITE_MSG), DEFAULT_SUB_TYPE)
    assert q is not None
    assert q.market_slug == "cpc-btc-updown-15m-xyz"
    assert q.best_bid == 0.54
    assert q.best_ask == 0.56
    assert q.last_trade_px == 0.55
    assert q.current_px == 0.55
    assert q.bid_depth == 5
    assert q.ask_depth == 4
    assert q.currency == "USD"
    assert q.state is None
    assert q.received_at > 0


def test_extract_quote_full():
    q = _extract_quote(json.loads(FULL_MSG), FULL_SUB_TYPE)
    assert q is not None
    assert q.best_bid == 0.545
    assert q.best_ask == 0.555
    assert q.state == "MARKET_STATE_OPEN"
    assert q.shares_traded == "150000"


def test_extract_quote_unknown_returns_none():
    assert _extract_quote({"subscriptionType": "nope"}, DEFAULT_SUB_TYPE) is None


# ── message handling routes to callbacks ───────────────────────────────


def test_handle_lite_invokes_on_quote():
    quotes = []
    w = make_worker(on_quote=lambda q: quotes.append(q))
    asyncio.run(w._handle(LITE_MSG))
    assert len(quotes) == 1
    assert quotes[0].market_slug == "cpc-btc-updown-15m-xyz"
    assert quotes[0].best_bid == 0.54
    assert w.stats()["quotes_emitted"] == 1
    assert w.stats()["messages_received"] == 1


def test_handle_heartbeat_updates_stats():
    w = make_worker()
    asyncio.run(w._handle(json.dumps({"heartbeat": {}})))
    assert w.stats()["heartbeats"] == 1
    assert w.stats()["messages_received"] == 1


def test_handle_error_counts_and_flags_auth():
    w = make_worker()
    asyncio.run(
        w._handle(json.dumps({"error": "invalid signature", "requestId": "r1"}))
    )
    assert w.stats()["errors"] == 1
    assert w._auth_failures == 1


def test_handle_non_auth_error_does_not_flag_auth():
    w = make_worker()
    asyncio.run(
        w._handle(json.dumps({"error": "market not found", "requestId": "r1"}))
    )
    assert w._auth_failures == 0


def test_dispatch_async_and_sync_callbacks():
    results = []

    async def async_cb(payload):
        results.append(("async", payload))

    def sync_cb(payload):
        results.append(("sync", payload))

    w = make_worker()
    asyncio.run(w._dispatch(async_cb, {"a": 1}))
    asyncio.run(w._dispatch(sync_cb, {"b": 2}))
    assert results == [("async", {"a": 1}), ("sync", {"b": 2})]


def test_callback_exception_does_not_kill_worker():
    calls = []

    def bad_cb(payload):
        calls.append(payload)
        raise RuntimeError("cb boom")

    w = make_worker(on_quote=bad_cb)
    asyncio.run(w._handle(LITE_MSG))
    assert calls  # callback was invoked
    assert w.stats()["quotes_emitted"] == 1  # worker kept counting


# ── connect: auth headers + subscription framing ───────────────────────


def test_connect_sends_auth_headers_and_subscription():
    fake_ws = FakeWebsockets()
    captured = {}

    def fake_auth(key_id, secret_key, method, path):
        captured["args"] = (key_id, secret_key, method, path)
        return {
            "X-PM-Access-Key": key_id,
            "X-PM-Timestamp": "1234567890",
            "X-PM-Signature": "sig",
        }

    conn = FakeConnection(messages=[LITE_MSG])
    fake_ws.queue.append(conn)

    with patch.object(polymarket_ws, "websockets", fake_ws), patch.object(
        polymarket_ws, "create_auth_headers", fake_auth
    ):
        w = make_worker(heartbeat_timeout=5, reconnect_max_attempts=1)
        quotes = []
        w._on_quote = lambda q: quotes.append(q)
        asyncio.run(w._run_loop())

    assert fake_ws.connect_kwargs["additional_headers"]["X-PM-Access-Key"] == "kid"
    assert (
        fake_ws.connect_kwargs["additional_headers"]["X-PM-Timestamp"]
        == "1234567890"
    )
    assert captured["args"][2] == "GET"
    assert captured["args"][3] == "/v1/ws/markets"

    # Subscription frame sent on connect.
    sub_frames = [
        json.loads(raw)["subscribe"]
        for raw in conn.sent
        if raw.startswith("{") and "subscribe" in json.loads(raw)
    ]
    assert sub_frames, "expected at least one subscribe frame"
    frame = sub_frames[0]
    assert frame["subscriptionType"] == DEFAULT_SUB_TYPE
    assert frame["marketSlugs"] == ["m1"]
    assert "requestId" in frame

    # One quote emitted from the single message; then one reconnect.
    assert len(quotes) == 1
    assert quotes[0].best_bid == 0.54
    assert w.stats()["connects"] == 2
    assert w.stats()["reconnects"] == 1


# ── reconnect resubscribes ─────────────────────────────────────────────


def test_reconnect_resubscribes_all_markets():
    fake_ws = FakeWebsockets()

    def fake_auth(key_id, secret_key, method, path):
        return {
            "X-PM-Access-Key": key_id,
            "X-PM-Timestamp": "1",
            "X-PM-Signature": "sig",
        }

    conn1 = FakeConnection(messages=[LITE_MSG])
    conn2 = FakeConnection()  # closes immediately on first recv
    fake_ws.queue.extend([conn1, conn2])

    with patch.object(polymarket_ws, "websockets", fake_ws), patch.object(
        polymarket_ws, "create_auth_headers", fake_auth
    ):
        w = make_worker(heartbeat_timeout=5, reconnect_max_attempts=1)
        quotes = []
        w._on_quote = lambda q: quotes.append(q)
        asyncio.run(w._run_loop())

    assert len(fake_ws.queue) == 0  # both connections consumed
    assert w.stats()["connects"] == 2
    assert w.stats()["reconnects"] == 1
    assert len(quotes) == 1

    # Subscription re-sent on both connections.
    subs = []
    for conn in (conn1, conn2):
        for raw in conn.sent:
            if raw.startswith("{") and "subscribe" in json.loads(raw):
                subs.append(json.loads(raw)["subscribe"])
    assert len(subs) == 2, f"expected 2 re-subscriptions, got {subs}"
    for s in subs:
        assert s["marketSlugs"] == ["m1"]


# ── heartbeat timeout forces reconnect ─────────────────────────────────


def test_heartbeat_timeout_reconnects():
    fake_ws = FakeWebsockets()

    def fake_auth(key_id, secret_key, method, path):
        return {
            "X-PM-Access-Key": key_id,
            "X-PM-Timestamp": "1",
            "X-PM-Signature": "sig",
        }

    async def hang_recv():
        await asyncio.Event().wait()  # never returns

    with patch.object(polymarket_ws, "websockets", fake_ws), patch.object(
        polymarket_ws, "create_auth_headers", fake_auth
    ):
        conn = FakeConnection()
        conn.recv = hang_recv
        fake_ws.queue.append(conn)

        w = make_worker(heartbeat_timeout=0.1, reconnect_max_attempts=1)
        asyncio.run(w._run_loop())

    assert w.stats()["connects"] == 2
    assert w.stats()["reconnects"] == 1


# ── auth failures stop retrying ────────────────────────────────────────


def test_auth_failures_stop_after_max():
    fake_ws = FakeWebsockets()

    def fake_auth(key_id, secret_key, method, path):
        return {
            "X-PM-Access-Key": key_id,
            "X-PM-Timestamp": "1",
            "X-PM-Signature": "sig",
        }

    with patch.object(polymarket_ws, "websockets", fake_ws), patch.object(
        polymarket_ws, "create_auth_headers", fake_auth
    ):
        fake_ws.queue.append(
            FakeConnection(raise_on_enter=FakeInvalidStatus(401, "unauthorized"))
        )

        w = make_worker(heartbeat_timeout=5, auth_max_failures=2)
        asyncio.run(w._run_loop())

    assert w.stats()["connects"] == 2
    assert w._auth_failures == 2
    assert w.stats()["stopped_auth"] is True
    assert w.stats()["stopped"] is True


# ── start / stop lifecycle ─────────────────────────────────────────────


def test_start_stop_is_clean():
    fake_ws = FakeWebsockets()

    def fake_auth(key_id, secret_key, method, path):
        return {
            "X-PM-Access-Key": key_id,
            "X-PM-Timestamp": "1",
            "X-PM-Signature": "sig",
        }

    async def hang_recv():
        await asyncio.Event().wait()

    with patch.object(polymarket_ws, "websockets", fake_ws), patch.object(
        polymarket_ws, "create_auth_headers", fake_auth
    ):
        conn = FakeConnection()
        conn.recv = hang_recv
        fake_ws.queue.append(conn)

        w = make_worker(heartbeat_timeout=5)

        async def go():
            await w.start()
            await asyncio.sleep(0.05)  # let the connect task run
            assert w.is_connected
            await w.stop()
            assert w._task is None
            assert w.stats()["stopped"] is True

        asyncio.run(go())


def test_start_without_creds_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("POLYMARKET_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_SECRET_KEY", raising=False)
    monkeypatch.setenv("BW_DIR", str(tmp_path / "bw"))
    w = PolymarketWSWorker(market_slugs=["m1"])
    with pytest.raises(PolymarketWSError):
        asyncio.run(w.start())


# ── subscription management ────────────────────────────────────────────


def test_subscribe_registers_without_connection():
    w = make_worker()
    assert len(w._subscriptions) == 1
    asyncio.run(w.subscribe(["m2", "m3"]))
    # slugs are chunked into subscriptions of up to 100, so m2+m3 share one.
    assert len(w._subscriptions) == 2
    slugs = [s for sub in w._subscriptions for s in sub.slugs]
    assert slugs == ["m1", "m2", "m3"]


def test_unsubscribe_removes_by_request_id():
    w = make_worker()
    rid = w._subscriptions[0].request_id
    asyncio.run(w.unsubscribe(rid))
    assert len(w._subscriptions) == 0
