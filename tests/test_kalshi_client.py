"""Tests for kalshi_client.py — Kalshi REST client authentication and request handling.

These tests mock the Kalshi API responses to avoid making real network calls.
"""
import pytest
from unittest.mock import MagicMock, patch
from kalshi_client import KalshiClient, KalshiAPIError
import config


def _make_mock_private_key():
    """Create a mock private key whose .sign() returns bytes."""
    key = MagicMock()
    key.sign.return_value = b"fake_signature_bytes"
    return key


class TestKalshiClientInit:
    """Tests for KalshiClient initialization."""

    def test_init_sets_attributes(self, monkeypatch):
        """Client stores key_id, base_url, and creates session."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "KALSHI_BASE_URL", "https://external-api.kalshi.com/trade-api/v2")
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            client = KalshiClient()
            assert client.key_id == "test_key_id"
            assert client.base_url == "https://external-api.kalshi.com/trade-api/v2"
            assert client._verified is False  # Not verified until first request


class TestAuthHeader:
    """Tests for auth header generation."""

    def test_auth_header_has_required_fields(self, monkeypatch):
        """Auth header contains key_id, timestamp, and signature."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_sig"
            client = KalshiClient()

        headers = client._auth_header("GET", "/markets")
        assert "KALSHI-ACCESS-KEY" in headers
        assert "KALSHI-ACCESS-TIMESTAMP" in headers
        assert "KALSHI-ACCESS-SIGNATURE" in headers
        assert headers["KALSHI-ACCESS-KEY"] == "test_key_id"
        assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()


class TestGetMarkets:
    """Tests for get_markets method."""

    def test_get_markets_returns_list(self, monkeypatch):
        """get_markets returns a list of market dicts."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_sig"
            client = KalshiClient()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"markets": [{"ticker": "KXBTC15M"}]}
        mock_resp.status_code = 200
        mock_resp.get.return_value = [{"ticker": "KXBTC15M"}]

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client.get_markets()
            assert isinstance(result, list)
            assert len(result) == 1
            assert result[0]["ticker"] == "KXBTC15M"

    def test_get_markets_caches_results(self, monkeypatch):
        """get_markets caches results per (series_ticker, status, limit)."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_sig"
            client = KalshiClient()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"markets": [{"ticker": "KXBTC15M"}]}
        mock_resp.status_code = 200
        mock_resp.get.return_value = [{"ticker": "KXBTC15M"}]

        with patch.object(client.session, "request", return_value=mock_resp) as mock_request:
            result1 = client.get_markets(series_ticker="KXBTC15M", status="open", limit=50)
            result2 = client.get_markets(series_ticker="KXBTC15M", status="open", limit=50)
            assert result1 is result2  # Same cached object
            # First call makes the HTTP request, second call uses cache
            assert mock_request.call_count >= 1  # At least one HTTP call


class TestHistoricalMarkets:
    """Tests for get_historical_markets method."""

    def test_get_historical_markets_returns_list(self, monkeypatch):
        """get_historical_markets returns a list of historical market dicts."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_sig"
            client = KalshiClient()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "markets": [
                {"ticker": "KXBTC15M-26NOV011145-45", "status": "finalized",
                 "expiration_value": "100", "close_time": "2026-11-01T11:45:00Z"}
            ],
            "cursor": "next_cursor_value"
        }
        mock_resp.status_code = 200

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client.get_historical_markets(series_ticker="KXBTC15M", limit=100)
            assert isinstance(result, dict)
            assert "markets" in result
            assert len(result["markets"]) == 1
            assert result["markets"][0]["ticker"] == "KXBTC15M-26NOV011145-45"
            assert result["cursor"] == "next_cursor_value"

    def test_get_market_candlesticks_returns_data(self, monkeypatch):
        """get_market_candlesticks returns candlestick data."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_sig"
            client = KalshiClient()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ticker": "KXBTC15M-26NOV011145-45",
            "candlesticks": [
                {"t": 1780380000, "o": 0.50, "h": 0.55, "l": 0.48, "c": 0.52, "v": 1000}
            ]
        }
        mock_resp.status_code = 200

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client.get_market_candlesticks(
                ticker="KXBTC15M-26NOV011145-45",
                start_ts=1780376400,
                end_ts=1780380000,
                period_interval=1,
                limit=100
            )
            assert isinstance(result, dict)
            assert "candlesticks" in result
            assert result["ticker"] == "KXBTC15M-26NOV011145-45"
            assert len(result["candlesticks"]) == 1
            assert result["candlesticks"][0]["o"] == 0.50

class TestOrderPayload:
    """Every order is quoted on the YES leg, so NO prices must be complemented."""

    def _client(self):
        from kalshi_client import KalshiClient
        c = KalshiClient.__new__(KalshiClient)
        sent = {}
        c._request = lambda method, path, json=None, **kw: (sent.update(json) or {"order_id": "x"})
        return c, sent

    def test_buy_no_is_ask_at_complement(self, monkeypatch):
        import kalshi_client
        monkeypatch.setattr(kalshi_client.config, "DRY_RUN", False)
        c, sent = self._client()
        c.place_order("T", "no", 5, 85, "cid-1")
        assert sent["side"] == "ask"
        assert sent["price"] == "0.1500"  # buy NO at 85c == sell YES at 15c
        assert sent["count"] == "5.00"

    def test_buy_yes_is_bid_at_price(self, monkeypatch):
        import kalshi_client
        monkeypatch.setattr(kalshi_client.config, "DRY_RUN", False)
        c, sent = self._client()
        c.place_order("T", "yes", 2, 61, "cid-2")
        assert sent["side"] == "bid"
        assert sent["price"] == "0.6100"


class TestGetBalance:
    """Tests for get_balance() sanity guard."""

    def test_get_balance_normal_cents(self, monkeypatch):
        """Normal balance in cents divides by 100 correctly."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_signature_bytes"
            client = KalshiClient()

            mock_resp = MagicMock()
            mock_resp.json.return_value = {"balance": 12250}  # 12250 cents = $122.50
            mock_resp.status_code = 200

            with patch.object(client.session, "request", return_value=mock_resp):
                assert client.get_balance() == 122.50

    def test_get_balance_raw_cents_over_10b_warns(self, monkeypatch, caplog):
        """Raw >$10B as cents warns (unit mismatch) but still divides."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_signature_bytes"
            client = KalshiClient()

            mock_resp = MagicMock()
            mock_resp.json.return_value = {"balance": 15_000_000_000}  # $150B as cents — implausible
            mock_resp.status_code = 200

            with patch.object(client.session, "request", return_value=mock_resp), \
                 caplog.at_level("WARNING", logger="kalshi_client"):
                result = client.get_balance()
                assert result == 150_000_000.0  # still divides
                assert "seems mislabeled" in caplog.text

    def test_get_balance_zero(self, monkeypatch):
        """Zero balance returns 0.0 without warning."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_signature_bytes"
            client = KalshiClient()

            mock_resp = MagicMock()
            mock_resp.json.return_value = {"balance": 0}  # 0 cents -> $0.00, no warning needed
            mock_resp.status_code = 200

            with patch.object(client.session, "request", return_value=mock_resp):
                assert client.get_balance() == 0.0

    def test_get_balance_sub_cent(self, monkeypatch, caplog):
        """Sub-cent balances (1-99 cents) convert correctly without false warning."""
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)

        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_signature_bytes"
            client = KalshiClient()

            # 1 cent = $0.01 — smallest non-zero balance, should NOT warn
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"balance": 1}
            mock_resp.status_code = 200

            with patch.object(client.session, "request", return_value=mock_resp), \
                 caplog.at_level("WARNING", logger="kalshi_client"):
                assert client.get_balance() == 0.01
                assert "seems mislabeled" not in caplog.text

            # 5 cents = $0.05
            mock_resp.json.return_value = {"balance": 5}
            with patch.object(client.session, "request", return_value=mock_resp), \
                 caplog.at_level("WARNING", logger="kalshi_client"):
                assert client.get_balance() == 0.05
                assert "seems mislabeled" not in caplog.text

            # 99 cents = $0.99 — sub-dollar, still no warning
            mock_resp.json.return_value = {"balance": 99}
            with patch.object(client.session, "request", return_value=mock_resp), \
                 caplog.at_level("WARNING", logger="kalshi_client"):
                assert client.get_balance() == 0.99
                assert "seems mislabeled" not in caplog.text



class TestGetShardBalance:
    """get_shard_balance() must return DOLLARS for the routed shard only.

    Regression: the first version divided nothing, so a shard holding 98
    cents returned 98.0 -- a live order would have been sized 10,000x too
    large on a sub-dollar account.
    """

    def _client(self, monkeypatch, balance_resp):
        monkeypatch.setattr(config, "KALSHI_KEY_ID", "test_key_id")
        monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
        monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)
        with patch.object(KalshiClient, "_load_private_key") as mock_load, \
             patch.object(KalshiClient, "_verify_key"):
            mock_load.return_value = MagicMock()
            mock_load.return_value.sign.return_value = b"fake_signature_bytes"
            c = KalshiClient()
        mock_resp = MagicMock()
        mock_resp.json.return_value = balance_resp
        mock_resp.status_code = 200
        c.session.request = MagicMock(return_value=mock_resp)
        return c

    def test_returns_dollars_for_routed_shard_not_total(self, monkeypatch):
        """Total is 122.49c, shard 2 holds 0.98c -> shard cash is $0.0098."""
        # Shape captured from a live GET /portfolio/balance: breakdown values
        # are dollar STRINGS, the legacy total is integer CENTS.
        c = self._client(monkeypatch, {
            "balance": 122,
            "balance_dollars": "1.2249",
            "balance_breakdown": [
                {"exchange_index": 0, "balance": "1.2151"},
                {"exchange_index": 1, "balance": "0.0000"},
                {"exchange_index": 2, "balance": "0.0098"},
                {"exchange_index": 3, "balance": "0.0000"},
            ],
        })
        assert c.get_shard_balance(0) == pytest.approx(1.2151)
        assert c.get_shard_balance(2) == pytest.approx(0.0098)

    def test_empty_or_absent_shard_is_zero(self, monkeypatch):
        """A shard not in the breakdown holds nothing; sizing must see 0."""
        c = self._client(monkeypatch, {"balance": 12151,
                                       "balance_breakdown": [
                                           {"exchange_index": 0, "balance": "121.51"}]})
        assert c.get_shard_balance(2) == 0.0

    def test_falls_back_to_total_without_breakdown(self, monkeypatch):
        """Subaccount-restricted keys omit balance_breakdown: keep old behaviour."""
        c = self._client(monkeypatch, {"balance": 12249})
        assert c.get_shard_balance(2) == pytest.approx(122.49)
