"""
Tests for poly_us_stream.py — Polymarket US WebSocket streaming client.

Tests use fakes and direct message-handler invocation; no real WebSocket
connections are made. Credentials are mocked so the stream never tries to
connect to the venue.
"""

from unittest.mock import patch


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
    """_on_order_update stores order execution in the cache using SDK shapes."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    # New SDK format: execution has id (execution id) and nested order object
    msg = {
        "orderSubscriptionUpdate": {
            "execution": {
                "id": "exec-123",
                "order": {
                    "id": "ord-123",
                    "marketSlug": "slug-x",
                    "state": "FILLED",
                    "cumQuantity": 5,
                    "leavesQuantity": 0,
                    "avgPx": "0.95",
                },
                "lastShares": 5,
                "lastPx": "0.95",
                "type": "FILL",
            }
        }
    }
    s._on_order_update(msg)
    with s._private_lock:
        cached = s._order_cache["ord-123"]
    assert cached["order_id"] == "ord-123"
    assert cached["execution_id"] == "exec-123"
    assert cached["market_slug"] == "slug-x"
    assert cached["state"] == "FILLED"
    assert cached["cum_quantity"] == 5
    assert cached["leaves_quantity"] == 0
    assert cached["avg_price"] == "0.95"
    assert cached["last_shares"] == 5
    assert cached["last_price"] == "0.95"
    assert cached["execution_type"] == "FILL"


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


# ── Full orderbook (market_data) handler ──────────────────────────────

def test_on_market_data_updates_orderbook():
    """_on_market_data parses full orderbook and updates cache."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    msg = {
        "marketData": {
            "marketSlug": "cpc-btc-updown-15m-test",
            "bids": [
                {"px": {"value": "0.92"}, "qty": "100"},
                {"px": {"value": "0.91"}, "qty": "200"},
            ],
            "offers": [
                {"px": {"value": "0.94"}, "qty": "150"},
                {"px": {"value": "0.95"}, "qty": "250"},
            ],
        }
    }
    s._on_market_data(msg)

    ob = s.get_orderbook("cpc-btc-updown-15m-test")
    assert ob is not None
    assert ob.slug == "cpc-btc-updown-15m-test"
    assert len(ob.yes_bids) == 2
    assert len(ob.yes_asks) == 2
    # Bids sorted descending (highest first)
    assert ob.yes_bids[0].price == 0.92
    assert ob.yes_bids[0].size == 100
    assert ob.yes_bids[1].price == 0.91
    # Asks sorted ascending (lowest first)
    assert ob.yes_asks[0].price == 0.94
    assert ob.yes_asks[0].size == 150
    assert ob.yes_asks[1].price == 0.95
    assert ob.updated_at > 0


def test_on_market_data_updates_bbo_from_full_book():
    """_on_market_data also updates BBO from the full orderbook."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    msg = {
        "marketData": {
            "marketSlug": "cpc-btc-updown-15m-test",
            "bids": [
                {"px": {"value": "0.92"}, "qty": "100"},
            ],
            "offers": [
                {"px": {"value": "0.94"}, "qty": "150"},
            ],
        }
    }
    s._on_market_data(msg)

    bbo = s.get_bbo("cpc-btc-updown-15m-test")
    assert bbo is not None
    assert bbo.yes_bid == 0.92
    assert bbo.yes_ask == 0.94


def test_on_market_data_ignores_missing_slug():
    """_on_market_data ignores messages without marketSlug."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    s._on_market_data({"marketData": {"bids": [{"px": {"value": "0.92"}, "qty": "100"}]}})
    assert s.get_orderbook("any-slug") is None


def test_on_market_data_handles_empty_levels():
    """_on_market_data handles empty bids/offers gracefully."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    msg = {
        "marketData": {
            "marketSlug": "cpc-btc-updown-15m-test",
            "bids": [],
            "offers": [],
        }
    }
    s._on_market_data(msg)

    ob = s.get_orderbook("cpc-btc-updown-15m-test")
    assert ob is not None
    assert ob.yes_bids == []
    assert ob.yes_asks == []


def test_get_orderbook_returns_none_when_no_data():
    """get_orderbook returns None for an untracked slug."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    assert s.get_orderbook("unknown-slug") is None


def test_get_orderbook_returns_copy_not_reference():
    """get_orderbook returns a copy to prevent external mutation."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    msg = {
        "marketData": {
            "marketSlug": "cpc-btc-updown-15m-test",
            "bids": [{"px": {"value": "0.92"}, "qty": "100"}],
            "offers": [{"px": {"value": "0.94"}, "qty": "150"}],
        }
    }
    s._on_market_data(msg)

    ob1 = s.get_orderbook("cpc-btc-updown-15m-test")
    ob2 = s.get_orderbook("cpc-btc-updown-15m-test")
    assert ob1 is not ob2  # Different objects (copies)
    assert ob1.yes_bids[0].price == ob2.yes_bids[0].price


def test_get_orderbook_depth_returns_summary():
    """get_orderbook_depth returns summarized depth with spread and mid."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    msg = {
        "marketData": {
            "marketSlug": "cpc-btc-updown-15m-test",
            "bids": [
                {"px": {"value": "0.92"}, "qty": "100"},
                {"px": {"value": "0.91"}, "qty": "200"},
            ],
            "offers": [
                {"px": {"value": "0.94"}, "qty": "150"},
                {"px": {"value": "0.95"}, "qty": "250"},
            ],
        }
    }
    s._on_market_data(msg)

    depth = s.get_orderbook_depth("cpc-btc-updown-15m-test", levels=2)
    assert depth is not None
    assert depth["slug"] == "cpc-btc-updown-15m-test"
    assert len(depth["yes_bids"]) == 2
    assert len(depth["yes_asks"]) == 2
    assert depth["yes_bids"][0] == (0.92, 100.0)
    assert depth["yes_asks"][0] == (0.94, 150.0)
    assert abs(depth["spread"] - 0.02) < 1e-10
    assert abs(depth["mid"] - 0.93) < 1e-10


def test_get_orderbook_depth_returns_none_when_no_data():
    """get_orderbook_depth returns None for untracked slug."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    assert s.get_orderbook_depth("unknown-slug") is None


def test_orderbook_levels_sorted_correctly():
    """Bids sorted descending, asks ascending regardless of input order."""
    s = _FakePolyStream(key_id="kid", secret_key="secret")
    msg = {
        "marketData": {
            "marketSlug": "cpc-btc-updown-15m-test",
            "bids": [
                {"px": {"value": "0.91"}, "qty": "100"},
                {"px": {"value": "0.93"}, "qty": "200"},
                {"px": {"value": "0.92"}, "qty": "150"},
            ],
            "offers": [
                {"px": {"value": "0.95"}, "qty": "100"},
                {"px": {"value": "0.93"}, "qty": "200"},
                {"px": {"value": "0.94"}, "qty": "150"},
            ],
        }
    }
    s._on_market_data(msg)

    ob = s.get_orderbook("cpc-btc-updown-15m-test")
    # Bids: 0.93, 0.92, 0.91 (descending)
    assert [lvl.price for lvl in ob.yes_bids] == [0.93, 0.92, 0.91]
    # Asks: 0.93, 0.94, 0.95 (ascending)
    assert [lvl.price for lvl in ob.yes_asks] == [0.93, 0.94, 0.95]
