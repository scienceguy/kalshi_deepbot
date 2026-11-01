"""Tests for market_finder.py — Market discovery and parsing.

These tests mock the KalshiClient to avoid making real network calls.
"""
import pytest
from unittest.mock import MagicMock
from market_finder import (
    _dollars_str_to_cents,
    _parse_api_time,
    _parse_market_prices,
    _build,
    find_active_markets,
    MarketInfo,
)
import config


class TestDollarsStrToCents:
    """Tests for _dollars_str_to_cents helper."""

    def test_dollar_string(self):
        """Dollar strings like '0.3900' convert to cents (39)."""
        assert _dollars_str_to_cents("0.3900") == 39
        assert _dollars_str_to_cents("0.50") == 50
        assert _dollars_str_to_cents("0.01") == 1

    def test_integer_cents(self):
        """Integer values >= 1 are treated as cents directly."""
        assert _dollars_str_to_cents(50) == 50
        assert _dollars_str_to_cents(1) == 1
        assert _dollars_str_to_cents(99) == 99

    def test_none_and_empty(self):
        """None, empty string, and 0 all return 0."""
        assert _dollars_str_to_cents(None) == 0
        assert _dollars_str_to_cents("") == 0
        assert _dollars_str_to_cents(0) == 0

    def test_invalid_string(self):
        """Invalid strings return 0."""
        assert _dollars_str_to_cents("invalid") == 0


class TestParseApiTime:
    """Tests for _parse_api_time helper."""

    def test_valid_iso_string(self):
        """Valid ISO 8601 strings parse correctly."""
        result = _parse_api_time("2024-01-01T00:00:00Z")
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 1

    def test_invalid_string(self):
        """Invalid strings return None."""
        assert _parse_api_time(None) is None
        assert _parse_api_time("") is None
        assert _parse_api_time("not-a-date") is None


class TestParseMarketPrices:
    """Tests for _parse_market_prices helper."""

    def test_dollar_fields(self):
        """Uses *_dollars fields when available."""
        mkt = {
            "yes_bid_dollars": "0.50",
            "yes_ask_dollars": "0.52",
            "no_bid_dollars": "0.48",
            "no_ask_dollars": "0.50",
        }
        yb, ya, nb, na, yq, nq = _parse_market_prices(mkt)
        assert yb == 50
        assert ya == 52
        assert nb == 48
        assert na == 50

    def test_fallback_to_non_dollar_fields(self):
        """Falls back to non-dollar fields when *_dollars fields absent."""
        mkt = {
            "yes_bid": "50",
            "yes_ask": "52",
            "no_bid": "48",
            "no_ask": "50",
        }
        yb, ya, nb, na, yq, nq = _parse_market_prices(mkt)
        assert yb == 50
        assert ya == 52
        assert nb == 48
        assert na == 50

    def test_missing_fields_return_zero(self):
        """Missing fields return 0."""
        mkt = {}
        yb, ya, nb, na, yq, nq = _parse_market_prices(mkt)
        assert yb == 0
        assert ya == 0
        assert nb == 0
        assert na == 0
        assert yq == 0
        assert nq == 0


class TestMarketInfo:
    """Tests for MarketInfo dataclass."""

    def test_mid_yes_property(self):
        """mid_yes returns average of yes_bid and yes_ask."""
        m = MarketInfo(
            ticker="KXBTC15M", series="KXBTC", close_time=None,
            strike=0, strike_type="",
            yes_bid=50, yes_ask=52, no_bid=48, no_ask=50,
            yes_liquidity=100, no_liquidity=100,
        )
        assert m.mid_yes == 51.0

    def test_mid_yes_fallback(self):
        """mid_yes falls back to yes_ask or yes_bid when one is missing."""
        m = MarketInfo(
            ticker="KXBTC15M", series="KXBTC", close_time=None,
            strike=0, strike_type="",
            yes_bid=50, yes_ask=0, no_bid=0, no_ask=0,
            yes_liquidity=0, no_liquidity=0,
        )
        assert m.mid_yes == 50.0

    def test_skew_property(self):
        """skew returns yes_bid - no_bid."""
        m = MarketInfo(
            ticker="KXBTC15M", series="KXBTC", close_time=None,
            strike=0, strike_type="",
            yes_bid=50, yes_ask=52, no_bid=48, no_ask=50,
            yes_liquidity=100, no_liquidity=100,
        )
        assert m.skew == 2


class TestFindActiveMarkets:
    """Tests for find_active_markets function."""

    def test_returns_empty_when_no_markets(self, monkeypatch):
        """Returns empty list when no markets match criteria."""
        monkeypatch.setattr(config, "MIN_YES_PRICE_CENTS", 1)
        monkeypatch.setattr(config, "MAX_YES_PRICE_CENTS", 99)
        monkeypatch.setattr(config, "HEDGE_YES_ASK_MAX", 12)
        monkeypatch.setattr(config, "MAX_HEDGE_PER_SERIES", 0)
        monkeypatch.setattr(config, "TARGET_SERIES", ["KXBTC15M"])

        client = MagicMock()
        client.get_markets.return_value = []

        result = find_active_markets(client)
        assert result == []

    def test_returns_main_markets(self, monkeypatch):
        """Returns markets with mid price in range."""
        monkeypatch.setattr(config, "MIN_YES_PRICE_CENTS", 1)
        monkeypatch.setattr(config, "MAX_YES_PRICE_CENTS", 99)
        monkeypatch.setattr(config, "HEDGE_YES_ASK_MAX", 12)
        monkeypatch.setattr(config, "MAX_HEDGE_PER_SERIES", 0)
        monkeypatch.setattr(config, "TARGET_SERIES", ["KXBTC15M"])

        client = MagicMock()
        client.get_markets.return_value = [
            {
                "ticker": "KXBTC15M",
                "status": "active",
                "close_time": "2024-01-01T23:59:59Z",
                "yes_bid_dollars": "0.50",
                "yes_ask_dollars": "0.52",
                "no_bid_dollars": "0.48",
                "no_ask_dollars": "0.50",
                "yes_bid_size_fp": "100",
                "no_bid_size_fp": "100",
            }
        ]

        result = find_active_markets(client)
        assert len(result) == 1
        assert result[0].ticker == "KXBTC15M"

    def test_filters_out_markets_outside_window(self, monkeypatch):
        """Filters out markets with minutes_left outside range."""
        monkeypatch.setattr(config, "MIN_YES_PRICE_CENTS", 1)
        monkeypatch.setattr(config, "MAX_YES_PRICE_CENTS", 99)
        monkeypatch.setattr(config, "HEDGE_YES_ASK_MAX", 12)
        monkeypatch.setattr(config, "MAX_HEDGE_PER_SERIES", 0)
        monkeypatch.setattr(config, "TARGET_SERIES", ["KXBTC15M"])

        client = MagicMock()
        client.get_markets.return_value = [
            {
                "ticker": "KXBTC15M",
                "status": "active",
                "close_time": "2024-01-01T00:00:00Z",  # Already past
                "yes_bid_dollars": "0.50",
                "yes_ask_dollars": "0.52",
                "no_bid_dollars": "0.48",
                "no_ask_dollars": "0.50",
                "yes_bid_size_fp": "100",
                "no_bid_size_fp": "100",
            }
        ]

        result = find_active_markets(client)
        assert result == []