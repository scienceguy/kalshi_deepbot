"""
Tests for poly_us_stream.py — Polymarket US WebSocket streaming client.

Tests use fakes and direct message-handler invocation; no real WebSocket
connections are made. Credentials are mocked so the stream never tries to
connect to the venue.
"""

from unittest.mock import patch

import pytest

import poly_us_stream


class _FakePolyStream(poly_us_stream.PolyStream):
    """PolyStream that skips the background thread for tests."""

    def start(self) -> None:
        self._running = True
        self._connected.set()

    def stop(self) -> None:
        self._running = False
        self._connected.clear()


# ── Initialization ──────────────────────────────────────────────────


def test_stream_init_no_credentials():
    """Stream without credentials is disabled (has_credentials False)."""
    s = poly_us_stream.PolyStream(key_id="", secret_key="")
    assert s.has_credentials() is False
    assert s.is_connected() is False


def test_stream_init_with_credentials():
    """Stream with credentials tracks them."""
    s = poly_us_stream.PolyStream(key_id="kid", secret_key="secret")
    assert s.has_credentials() is True


# ── BBO cache via message handler ───────────────────────────────────


def test_on_market_data_lite_updates_bbo():
    """_on_market_data_lite stores best bid/ask per slug."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    msg = {
        "marketDataLite": {
            "marketSlug": "cpc-btc-updown-15m-test",
            "bestBid": {"value": "0.92"},
            "bestAsk": {"value": "0.94"},
        }
    }
    s._on_market_data_lite(msg)
    bbo = s.get_bbo("cpc-btc-updown-15m-test")
    assert bbo is not None
    assert bbo.slug == "cpc-btc-updown-15m-test"
    assert bbo.yes_bid == 0.92
    assert bbo.yes_ask == 0.94
    assert bbo.updated_at > 0


def test_on_market_data_lite_ignores_missing_slug():
    """Messages without marketSlug are ignored."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    s._on_market_data_lite({"marketDataLite": {"bestBid": {"value": "0.92"}}})
    assert s.get_all_bbos() == {}


def test_get_bbo_returns_none_when_no_data():
    """get_bbo returns None for an untracked slug."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    assert s.get_bbo("unknown-slug") is None


def test_amount_float_parses_value():
    """_amount_float extracts the numeric value from an Amount dict."""
    assert poly_us_stream.PolyStream._amount_float({"value": "0.92"}) == 0.92
    assert poly_us_stream.PolyStream._amount_float({"value": "0"}) == 0.0


def test_amount_float_returns_none_on_bad_input():
    """_amount_float returns None for missing/malformed input."""
    assert poly_us_stream.PolyStream._amount_float(None) is None
    assert poly_us_stream.PolyStream._amount_float({}) is None
    assert poly_us_stream.PolyStream._amount_float({"value": "not-a-number"}) is None


# ── track / subscription ────────────────────────────────────────────


def test_track_adds_slug():
    """track() adds a slug to the subscription set without raising."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    s.track("slug-a")
    s.track("slug-b")
    # track() with no active loop should not raise (slugs queued for next connect)
    with s._tracked_slugs_lock:
        assert "slug-a" in s._tracked_slugs
        assert "slug-b" in s._tracked_slugs


# ── Order / position / balance handlers ─────────────────────────────


def test_on_order_update_caches_execution():
    """_on_order_update stores order execution in the cache."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    msg = {
        "orderSubscriptionUpdate": {
            "execution": {
                "orderId": "ord-123",
                "marketSlug": "slug-x",
                "status": "FILLED",
                "filledQuantity": 5,
            }
        }
    }
    s._on_order_update(msg)
    with s._private_lock:
        cached = s._order_cache["ord-123"]
    assert cached["order_id"] == "ord-123"
    assert cached["market_slug"] == "slug-x"
    assert cached["status"] == "FILLED"
    assert cached["filled_quantity"] == 5


def test_on_position_update_caches_after_position():
    """_on_position_update stores the after position keyed by securityId."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    msg = {
        "positionSubscription": {
            "afterPosition": {"securityId": "sec-1", "qty": "10"},
            "beforePosition": {"securityId": "sec-1", "qty": "5"},
        }
    }
    s._on_position_update(msg)
    with s._private_lock:
        cached = s._position_cache["sec-1"]
    assert cached["qty"] == "10"


def test_on_balance_update_caches_balance():
    """_on_balance_update stores balance changes keyed by currency."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    msg = {
        "accountBalancesUpdate": {
            "balanceChange": {
                "afterBalance": {"currency": "USD", "currentBalance": "100.00"}
            }
        }
    }
    s._on_balance_update(msg)
    with s._private_lock:
        cached = s._balance_cache["USD"]
    assert cached["currency"] == "USD"
    assert cached["currentBalance"] == "100.00"


# ── get_poly_stream graceful degradation ────────────────────────────


@patch("poly_us_stream.polymarket_us_client._load_key_id", return_value="")
@patch("poly_us_stream.polymarket_us_client._load_secret_key", return_value="")
def test_get_poly_stream_no_creds_returns_disabled_stream(mock_key, mock_secret):
    """get_poly_stream returns a stream without credentials (disabled)."""
    poly_us_stream._poly_stream = None
    stream = poly_us_stream.get_poly_stream()
    assert stream is not None
    assert stream.has_credentials() is False
    assert stream.is_connected() is False


@patch("poly_us_stream.polymarket_us_client._load_key_id", return_value="kid")
@patch("poly_us_stream.polymarket_us_client._load_secret_key", return_value="secret")
def test_get_poly_stream_creates_stream(mock_key, mock_secret):
    """get_poly_stream creates a stream with credentials."""
    poly_us_stream._poly_stream = None
    stream = poly_us_stream.get_poly_stream()
    assert stream is not None
    assert stream.has_credentials() is True
    poly_us_stream._poly_stream = None
