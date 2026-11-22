"""Tests for kalshi_ws.py - Kalshi WebSocket client."""

import json
import logging
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kalshi_ws
from kalshi_ws import (
    KalshiWebSocket,
    MarketData,
    FillData,
    OrderUpdate,
    OrderbookSnapshot,
    OrderbookDelta,
    CFB_INDEX_IDS,
    BASE_ASSET_TO_CFB_INDEX,
    WS_MSG_CFB_VALUE,
    WS_MSG_CFB_VALUE_5HZ,
)


def _make_ws(monkeypatch):
    """Build a KalshiWebSocket with mocked config/key loading (no real PEM)."""
    monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
    monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
    monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)
    with patch("kalshi_ws.Path.exists", return_value=False):
        return KalshiWebSocket()


class TestKalshiWebSocketInit:
    """Test WebSocket client initialization."""

    def test_init_defaults(self, monkeypatch):
        """Test initialization with defaults from config."""
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)

        with patch("kalshi_ws.Path.exists", return_value=False):
            ws = KalshiWebSocket(key_id="explicit-id", key_file="/tmp/explicit.pem")

        assert ws.key_id == "explicit-id"
        assert ws.key_file == "/tmp/explicit.pem"
        assert ws.environment == "production"
        assert ws.ws_url == "wss://external-api-ws.kalshi.com/trade-api/ws/v2"

    def test_init_demo_environment(self, monkeypatch):
        """Test initialization with demo environment."""
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)

        with patch("kalshi_ws.Path.exists", return_value=False):
            ws = KalshiWebSocket(environment="demo")

        assert ws.environment == "demo"
        assert ws.ws_url == "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"

    def test_init_invalid_environment_falls_back(self, monkeypatch):
        """Test invalid environment falls back to production."""
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)

        with patch("kalshi_ws.Path.exists", return_value=False):
            ws = KalshiWebSocket(environment="invalid")

        assert ws.ws_url == "wss://external-api-ws.kalshi.com/trade-api/ws/v2"


class TestKalshiWebSocketAuth:
    """Test authentication header generation."""

    def test_auth_headers_format(self, monkeypatch):
        """Test auth headers have correct format."""
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)

        with patch("kalshi_ws.Path.exists", return_value=False):
            ws = KalshiWebSocket(key_id="test-key-id", key_file="/tmp/test.pem")

        # Mock private key
        mock_key = MagicMock()
        mock_key.sign.return_value = b"fake-signature"
        ws._privkey = mock_key

        headers = ws._auth_headers()

        assert "KALSHI-ACCESS-KEY" in headers
        assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
        assert "KALSHI-ACCESS-TIMESTAMP" in headers
        assert "KALSHI-ACCESS-SIGNATURE" in headers
        # Verify timestamp is recent (within last 10 seconds)
        ts = int(headers["KALSHI-ACCESS-TIMESTAMP"])
        assert abs(ts - int(time.time() * 1000)) < 10000


class TestKalshiWebSocketSubscriptionMessages:
    """Test subscription message format matches Kalshi API."""

    def test_subscribe_ticker_message_format(self, monkeypatch):
        """Test subscribe message uses correct Kalshi API format."""
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)

        with patch("kalshi_ws.Path.exists", return_value=False):
            ws = KalshiWebSocket()

        # Mock WebSocket
        mock_ws = AsyncMock()
        ws._ws = mock_ws
        ws._connected = True
        ws._loop = AsyncMock()

        ws.subscribe_ticker(["KXBTC15M-26DEC3123-BTC"])

        # Verify the message sent matches Kalshi API format
        call_args = mock_ws.send.call_args[0][0]
        msg = json.loads(call_args)
        assert msg["cmd"] == "subscribe"
        assert msg["params"]["channels"] == ["ticker"]
        assert msg["params"]["market_tickers"] == ["KXBTC15M-26DEC3123-BTC"]

    def test_subscribe_fills_message_format(self, monkeypatch):
        """Test subscribe fills uses correct format."""
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)

        with patch("kalshi_ws.Path.exists", return_value=False):
            ws = KalshiWebSocket()

        mock_ws = AsyncMock()
        ws._ws = mock_ws
        ws._connected = True
        ws._loop = AsyncMock()

        ws.subscribe_fills()

        call_args = mock_ws.send.call_args[0][0]
        msg = json.loads(call_args)
        assert msg["cmd"] == "subscribe"
        assert msg["params"]["channels"] == ["fill"]
        assert msg["params"]["market_tickers"] == ["*"]

    def test_subscribe_orders_message_format(self, monkeypatch):
        """Test subscribe orders uses correct format."""
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)

        with patch("kalshi_ws.Path.exists", return_value=False):
            ws = KalshiWebSocket()

        mock_ws = AsyncMock()
        ws._ws = mock_ws
        ws._connected = True
        ws._loop = AsyncMock()

        ws.subscribe_orders()

        call_args = mock_ws.send.call_args[0][0]
        msg = json.loads(call_args)
        assert msg["cmd"] == "subscribe"
        assert msg["params"]["channels"] == ["order"]
        assert msg["params"]["market_tickers"] == ["*"]

    def test_unsubscribe_message_format(self, monkeypatch):
        """Test unsubscribe message format."""
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)

        with patch("kalshi_ws.Path.exists", return_value=False):
            ws = KalshiWebSocket()

        mock_ws = AsyncMock()
        ws._ws = mock_ws
        ws._connected = True
        ws._loop = AsyncMock()

        ws.unsubscribe("ticker", ["KXBTC15M-26DEC3123-BTC"])

        call_args = mock_ws.send.call_args[0][0]
        msg = json.loads(call_args)
        assert msg["cmd"] == "unsubscribe"
        assert msg["params"]["channels"] == ["ticker"]
        assert msg["params"]["market_tickers"] == ["KXBTC15M-26DEC3123-BTC"]


class TestKalshiWebSocketCFBSubscription:
    """Test CF Benchmarks subscription payloads (subscribe via index_ids)."""

    def test_subscribe_cfb_value_message_format(self, monkeypatch):
        """Test subscribe message uses index_ids for the 1 Hz CFB channel."""
        ws = _make_ws(monkeypatch)
        mock_ws = AsyncMock()
        ws._ws = mock_ws
        ws._connected = True
        ws._loop = AsyncMock()

        ws.subscribe_cfb_value(["BRTI", "ETHUSD_RTI"])

        call_args = mock_ws.send.call_args[0][0]
        msg = json.loads(call_args)
        assert msg["cmd"] == "subscribe"
        assert msg["params"]["channels"] == ["cfbenchmarks_value"]
        assert msg["params"]["index_ids"] == ["BRTI", "ETHUSD_RTI"]
        # CFB channels must NOT send market_tickers
        assert "market_tickers" not in msg["params"]

    def test_subscribe_cfb_value_5hz_message_format(self, monkeypatch):
        """Test subscribe message uses index_ids for the 5 Hz CFB channel."""
        ws = _make_ws(monkeypatch)
        mock_ws = AsyncMock()
        ws._ws = mock_ws
        ws._connected = True
        ws._loop = AsyncMock()

        ws.subscribe_cfb_value_5hz(["SOLUSD_RTI"])

        call_args = mock_ws.send.call_args[0][0]
        msg = json.loads(call_args)
        assert msg["cmd"] == "subscribe"
        assert msg["params"]["channels"] == ["cfbenchmarks_value_5hz"]
        assert msg["params"]["index_ids"] == ["SOLUSD_RTI"]
        assert "market_tickers" not in msg["params"]

    def test_subscribe_cfb_value_defaults_to_all_index_ids(self, monkeypatch):
        """Test subscribe with no args sends the full index_ids list."""
        ws = _make_ws(monkeypatch)
        mock_ws = AsyncMock()
        ws._ws = mock_ws
        ws._connected = True
        ws._loop = AsyncMock()

        ws.subscribe_cfb_value()

        call_args = mock_ws.send.call_args[0][0]
        msg = json.loads(call_args)
        assert msg["params"]["channels"] == ["cfbenchmarks_value"]
        assert msg["params"]["index_ids"] == CFB_INDEX_IDS

    def test_subscribe_cfb_value_5hz_defaults_to_all_index_ids(self, monkeypatch):
        """Test 5 Hz subscribe with no args sends the full index_ids list."""
        ws = _make_ws(monkeypatch)
        mock_ws = AsyncMock()
        ws._ws = mock_ws
        ws._connected = True
        ws._loop = AsyncMock()

        ws.subscribe_cfb_value_5hz()

        call_args = mock_ws.send.call_args[0][0]
        msg = json.loads(call_args)
        assert msg["params"]["channels"] == ["cfbenchmarks_value_5hz"]
        assert msg["params"]["index_ids"] == CFB_INDEX_IDS

    def test_cfb_subscriptions_tracked_when_disconnected(self, monkeypatch):
        """Test CFB index_ids are recorded for resubscribe while not connected."""
        ws = _make_ws(monkeypatch)
        ws._connected = False

        ws.subscribe_cfb_value(["BRTI"])
        ws.subscribe_cfb_value_5hz(["ETHUSD_RTI"])

        assert ws._subscriptions["cfbenchmarks_value"] == {"BRTI"}
        assert ws._subscriptions["cfbenchmarks_value_5hz"] == {"ETHUSD_RTI"}


class TestKalshiWebSocketCFBMessageHandling:
    """Test CF Benchmarks message parsing (data field is a JSON string)."""

    @pytest.fixture
    def ws_client(self, monkeypatch):
        ws = _make_ws(monkeypatch)
        ws._privkey = MagicMock()
        ws._privkey.sign.return_value = b"fake-sig"
        return ws

    def test_handle_cfb_value_with_60s_average(self, ws_client):
        """Parse a realistic cfbenchmarks_value frame carrying the 60s average."""
        import asyncio

        ws_client._cfb_cache["BRTI"] = (62000.0, time.time())
        raw_frame = json.dumps({
            "symbol": "BRTI",
            "timestamp": "2026-11-22T12:34:56Z",
            "value": 62000.0,
            "average_60s": 61950.5,
        })
        message = {"type": WS_MSG_CFB_VALUE, "msg": {"data": raw_frame}}

        asyncio.run(ws_client._handle_message(json.dumps(message)))

        assert ws_client.get_cfb_value("BRTI") == 62000.0
        value, age = ws_client.get_cfb_with_age("BRTI")
        assert value == 62000.0
        assert age is not None and age < 1.0

    def test_handle_cfb_value_5hz_lean_frame(self, ws_client):
        """Parse a lean cfbenchmarks_value_5hz frame (no averages)."""
        import asyncio

        ws_client._cfb_cache["ETHUSD_RTI"] = (3000.25, time.time())
        raw_frame = json.dumps({
            "symbol": "ETHUSD_RTI",
            "timestamp": "2026-11-22T12:34:56Z",
            "value": 3000.25,
        })
        message = {"type": WS_MSG_CFB_VALUE_5HZ, "msg": {"data": raw_frame}}

        asyncio.run(ws_client._handle_message(json.dumps(message)))

        assert ws_client.get_cfb_value("ETHUSD_RTI") == 3000.25

    def test_handle_cfb_value_index_id_field(self, ws_client):
        """Payloads using index_id instead of symbol are parsed too."""
        import asyncio

        ws_client._cfb_cache["SOLUSD_RTI"] = (150.0, time.time())
        raw_frame = json.dumps({"index_id": "SOLUSD_RTI", "current": 150.0})
        message = {"type": WS_MSG_CFB_VALUE, "msg": {"data": raw_frame}}

        asyncio.run(ws_client._handle_message(json.dumps(message)))

        assert ws_client.get_cfb_value("SOLUSD_RTI") == 150.0

    def test_handle_cfb_malformed_data_logs_warning(self, ws_client, caplog):
        """A malformed data string logs a warning and does not raise."""
        import asyncio

        caplog.set_level(logging.WARNING)
        message = {"type": WS_MSG_CFB_VALUE, "msg": {"data": "not-json{"}}

        # Must not raise
        asyncio.run(ws_client._handle_message(json.dumps(message)))

        # Should log a JSON parsing warning
        assert any("Failed to parse cfbenchmarks_value data:" in record.message for record in caplog.records)
        assert ws_client.get_cfb_index_ids() == []

    def test_handle_cfb_missing_symbol_ignored(self, ws_client):
        """A frame with no index id is ignored, no cache entry created."""
        import asyncio

        raw_frame = json.dumps({"value": 100.0})
        message = {"type": WS_MSG_CFB_VALUE, "msg": {"data": raw_frame}}

        asyncio.run(ws_client._handle_message(json.dumps(message)))

        assert ws_client.get_cfb_index_ids() == []


class TestKalshiWebSocketCFBCache:
    """Test CF Benchmarks cache getters and staleness."""

    def test_get_cfb_value_from_cache(self, monkeypatch):
        ws = _make_ws(monkeypatch)
        ws._cfb_cache["BRTI"] = (62000.0, time.time())

        assert ws.get_cfb_value("BRTI") == 62000.0
        assert ws.get_cfb_value("NONEXISTENT") is None

    def test_get_cfb_with_age(self, monkeypatch):
        ws = _make_ws(monkeypatch)
        ws._cfb_cache["ETHUSD_RTI"] = (3000.0, time.time())

        value, age = ws.get_cfb_with_age("ETHUSD_RTI")
        assert value == 3000.0
        assert age is not None and age < 1.0

        assert ws.get_cfb_with_age("NONEXISTENT") == (None, None)

    def test_get_cfb_index_ids(self, monkeypatch):
        ws = _make_ws(monkeypatch)
        ws._cfb_cache["BRTI"] = (62000.0, time.time())
        ws._cfb_cache["ETHUSD_RTI"] = (3000.0, time.time())

        assert set(ws.get_cfb_index_ids()) == {"BRTI", "ETHUSD_RTI"}

    def test_cfb_is_stale(self, monkeypatch):
        ws = _make_ws(monkeypatch)
        ws._cfb_cache["BRTI"] = (62000.0, time.time())  # fresh
        ws._cfb_cache["OLD_INDEX"] = (100.0, time.time() - 1000.0)  # very old

        assert not ws.cfb_is_stale("BRTI", stale_seconds=1.0)
        assert ws.cfb_is_stale("OLD_INDEX", stale_seconds=1.0)

    def test_cfb_is_stale_no_value(self, monkeypatch):
        ws = _make_ws(monkeypatch)

        assert ws.cfb_is_stale("NONEXISTENT", stale_seconds=1.0)


class TestCFBIndexMapping:
    """Test CF Benchmarks index id constants and base-asset map."""

    def test_base_asset_to_cfb_index_mapping(self):
        assert BASE_ASSET_TO_CFB_INDEX["BTC"] == "BRTI"
        assert BASE_ASSET_TO_CFB_INDEX["ETH"] == "ETHUSD_RTI"
        assert BASE_ASSET_TO_CFB_INDEX["SOL"] == "SOLUSD_RTI"
        assert BASE_ASSET_TO_CFB_INDEX["XRP"] == "XRPUSD_RSI"
        assert BASE_ASSET_TO_CFB_INDEX["DOGE"] == "DOGEUSD_RSI"

    def test_cfb_index_ids_match_map_values(self):
        assert set(CFB_INDEX_IDS) == set(BASE_ASSET_TO_CFB_INDEX.values())


class TestKalshiWebSocketMessageHandling:
    """Test message parsing and handling."""

    @pytest.fixture
    def ws_client(self, monkeypatch):
        """Create a WebSocket client with mocked key loading."""
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)

        with patch("kalshi_ws.Path.exists", return_value=False):
            ws = KalshiWebSocket()
        ws._privkey = MagicMock()
        ws._privkey.sign.return_value = b"fake-sig"
        return ws

    def test_handle_ticker_message(self, ws_client):
        """Test parsing ticker message from Kalshi API."""
        message = {
            "type": "ticker",
            "msg": {
                "market_ticker": "KXBTC15M-26DEC3123-BTC",
                "yes_bid_dollars": 0.95,
                "yes_ask_dollars": 0.96,
                "no_bid_dollars": 0.04,
                "no_ask_dollars": 0.05,
                "last_price_dollars": 0.955,
                "volume": 10000,
                "open_interest": 50000,
            }
        }

        received = []

        def callback(md: MarketData):
            received.append(md)

        ws_client.set_ticker_callback(callback)

        # Run the async handler
        import asyncio
        asyncio.run(ws_client._handle_ticker(message))

        assert len(received) == 1
        md = received[0]
        assert md.ticker == "KXBTC15M-26DEC3123-BTC"
        assert md.yes_bid == 0.95
        assert md.yes_ask == 0.96
        assert md.no_bid == 0.04
        assert md.no_ask == 0.05
        assert md.last_price == 0.955
        assert md.volume == 10000
        assert md.open_interest == 50000

    def test_handle_fill_message(self, ws_client):
        """Test parsing fill message."""
        message = {
            "type": "fill",
            "msg": {
                "fill_id": "fill-123",
                "order_id": "order-456",
                "market_ticker": "KXBTC15M-26DEC3123-BTC",
                "side": "yes",
                "price": 0.95,
                "size": 10,
            }
        }

        received = []

        def callback(fd: FillData):
            received.append(fd)

        ws_client.set_fill_callback(callback)

        import asyncio
        asyncio.run(ws_client._handle_fill(message))

        assert len(received) == 1
        fd = received[0]
        assert fd.fill_id == "fill-123"
        assert fd.order_id == "order-456"
        assert fd.market_ticker == "KXBTC15M-26DEC3123-BTC"
        assert fd.side == "yes"
        assert fd.price == 0.95
        assert fd.size == 10

    def test_handle_order_message(self, ws_client):
        """Test parsing order update message."""
        message = {
            "type": "order",
            "msg": {
                "order_id": "order-456",
                "market_ticker": "KXBTC15M-26DEC3123-BTC",
                "side": "yes",
                "price": 0.95,
                "size": 10,
                "status": "filled",
                "filled_size": 10,
            }
        }

        received = []

        def callback(ou: OrderUpdate):
            received.append(ou)

        ws_client.set_order_callback(callback)

        import asyncio
        asyncio.run(ws_client._handle_order(message))

        assert len(received) == 1
        ou = received[0]
        assert ou.order_id == "order-456"
        assert ou.market_ticker == "KXBTC15M-26DEC3123-BTC"
        assert ou.side == "yes"
        assert ou.price == 0.95
        assert ou.size == 10
        assert ou.status == "filled"
        assert ou.filled_size == 10

    def test_handle_orderbook_snapshot_message(self, ws_client):
        """Test parsing orderbook snapshot."""
        message = {
            "type": "orderbook_snapshot",
            "msg": {
                "market_ticker": "KXBTC15M-26DEC3123-BTC",
                "yes": [{"price": 0.95, "size": 100}, {"price": 0.94, "size": 200}],
                "no": [{"price": 0.05, "size": 150}, {"price": 0.06, "size": 250}],
            }
        }

        received = []

        def callback(ob: OrderbookSnapshot):
            received.append(ob)

        ws_client.set_orderbook_snapshot_callback(callback)

        import asyncio
        asyncio.run(ws_client._handle_orderbook_snapshot(message))

        assert len(received) == 1
        ob = received[0]
        assert ob.ticker == "KXBTC15M-26DEC3123-BTC"
        assert len(ob.yes_levels) == 2
        assert len(ob.no_levels) == 2
        assert ob.yes_levels[0]["price"] == 0.95
        assert ob.yes_levels[0]["size"] == 100

    def test_handle_orderbook_delta_message(self, ws_client):
        """Test parsing orderbook delta."""
        message = {
            "type": "orderbook_delta",
            "msg": {
                "market_ticker": "KXBTC15M-26DEC3123-BTC",
                "yes": [{"price": 0.95, "size": 50, "side": "bid"}],
                "no": [{"price": 0.05, "size": 0, "side": "ask"}],  # size 0 = remove
            }
        }

        received = []

        def callback(ob: OrderbookDelta):
            received.append(ob)

        ws_client.set_orderbook_delta_callback(callback)

        import asyncio
        asyncio.run(ws_client._handle_orderbook_delta(message))

        assert len(received) == 1
        ob = received[0]
        assert ob.ticker == "KXBTC15M-26DEC3123-BTC"
        assert len(ob.yes_changes) == 1
        assert ob.yes_changes[0]["price"] == 0.95
        assert ob.yes_changes[0]["side"] == "bid"

    def test_handle_market_lifecycle_message(self, ws_client):
        """Test parsing market lifecycle event."""
        message = {
            "type": "market_lifecycle_v2",
            "msg": {
                "market_ticker": "KXBTC15M-26DEC3123-BTC",
                "status": "closed",
                "event": "settlement",
            }
        }

        received = []

        def callback(msg):
            received.append(msg)

        ws_client.set_market_lifecycle_callback(callback)

        import asyncio
        asyncio.run(ws_client._handle_market_lifecycle(message))

        assert len(received) == 1
        assert received[0]["market_ticker"] == "KXBTC15M-26DEC3123-BTC"
        assert received[0]["status"] == "closed"

    def test_unknown_message_type_logged(self, ws_client, caplog):
        """Test unknown message types are logged at debug level."""
        caplog.set_level(logging.DEBUG)

        message = {"type": "unknown_type", "msg": {}}

        import asyncio
        asyncio.run(ws_client._handle_message(json.dumps(message)))

        assert "Unknown WS message type: unknown_type" in caplog.text


class TestKalshiWebSocketCache:
    """Test cache getters."""

    def test_get_ticker_from_cache(self, monkeypatch):
        """Test getting ticker data from cache."""
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)

        with patch("kalshi_ws.Path.exists", return_value=False):
            ws = KalshiWebSocket()

        md = MarketData(ticker="TEST", yes_bid=0.5, yes_ask=0.51)
        ws._ticker_cache["TEST"] = md

        result = ws.get_ticker("TEST")
        assert result is md

        result = ws.get_ticker("NONEXISTENT")
        assert result is None

    def test_get_fills_from_cache(self, monkeypatch):
        """Test getting fills from cache."""
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)

        with patch("kalshi_ws.Path.exists", return_value=False):
            ws = KalshiWebSocket()

        fd1 = FillData(fill_id="1", order_id="o1", market_ticker="TEST", side="yes", price=0.5, size=10, timestamp=1000)
        fd2 = FillData(fill_id="2", order_id="o2", market_ticker="TEST", side="no", price=0.5, size=5, timestamp=2000)
        ws._fill_cache = [fd1, fd2]

        all_fills = ws.get_fills()
        assert len(all_fills) == 2

        recent_fills = ws.get_fills(since=1500)
        assert len(recent_fills) == 1
        assert recent_fills[0].fill_id == "2"


class TestKalshiWebSocketSingleton:
    """Test singleton getter functions."""

    def test_get_kalshi_ws_creates_singleton(self, monkeypatch):
        """Test get_kalshi_ws creates and returns singleton."""
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)

        with patch("kalshi_ws.Path.exists", return_value=False):
            # Reset singleton
            import kalshi_ws
            kalshi_ws._kalshi_ws_instance = None

            ws1 = kalshi_ws.get_kalshi_ws()
            ws2 = kalshi_ws.get_kalshi_ws()

            assert ws1 is ws2

    def test_close_kalshi_ws_resets_singleton(self, monkeypatch):
        """Test close_kalshi_ws stops and resets singleton."""
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)

        with patch("kalshi_ws.Path.exists", return_value=False):
            import kalshi_ws
            kalshi_ws._kalshi_ws_instance = None

            ws = kalshi_ws.get_kalshi_ws()
            ws._thread = MagicMock()
            ws._thread.is_alive.return_value = False

            kalshi_ws.close_kalshi_ws()

            assert kalshi_ws._kalshi_ws_instance is None


class TestDataClasses:
    """Test dataclass creation and defaults."""

    def test_market_data_defaults(self):
        """Test MarketData defaults."""
        md = MarketData(ticker="TEST")
        assert md.ticker == "TEST"
        assert md.yes_bid is None
        assert md.timestamp > 0

    def test_fill_data_defaults(self):
        """Test FillData defaults."""
        fd = FillData(fill_id="1", order_id="o1", market_ticker="TEST", side="yes", price=0.5, size=10)
        assert fd.fill_id == "1"
        assert fd.timestamp > 0

    def test_order_update_defaults(self):
        """Test OrderUpdate defaults."""
        ou = OrderUpdate(order_id="1", market_ticker="TEST", side="yes", price=0.5, size=10, status="open")
        assert ou.order_id == "1"
        assert ou.filled_size == 0.0
        assert ou.timestamp > 0

    def test_orderbook_snapshot_defaults(self):
        """Test OrderbookSnapshot defaults."""
        ob = OrderbookSnapshot(ticker="TEST", yes_levels=[], no_levels=[])
        assert ob.ticker == "TEST"
        assert ob.timestamp > 0

    def test_orderbook_delta_defaults(self):
        """Test OrderbookDelta defaults."""
        ob = OrderbookDelta(ticker="TEST", yes_changes=[], no_changes=[])
        assert ob.ticker == "TEST"
        assert ob.timestamp > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])