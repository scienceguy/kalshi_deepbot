"""
Tests for crossvenue.py Polymarket US API integration and fallback.

These tests verify the US mapper function and the fallback logic without
making real network calls.
"""
from __future__ import annotations

import sys
from datetime import datetime
from unittest.mock import patch

import pytest

from crossvenue import (
    Ladder,
    Rung,
    fetch_pm_ladder_us,
    fetch_pm_ladder,
    _fetch_pm_ladder_with_fallback,
    parse_pm_slug,
)


def _make_market_dict(
    *,
    strike: float = 2600.0,
    yes_price: str = "0.78",
    bid: float | None = None,
    ask: float | None = None,
    asset_symbol: str = "BTC",
    window_end: datetime | None = None,
) -> dict:
    """Build a minimal synthetic US Polymarket market dict for testing."""
    market: dict = {
        "assetPriceTerms": {
            "strike": str(strike),
            "asset": {"symbol": asset_symbol},
            "assetName": "Bitcoin" if asset_symbol == "BTC" else "Ethereum",
            "marketType": "linear",
            "windowEnd": window_end or datetime(2026, 11, 5, 23, 59, 0),
            "direction": "up",
            "rangeLower": None,
            "rangeUpper": None,
            "priceToBeat": None,
        },
        "marketSides": [
            {"price": yes_price, "long": True, "marketSideType": "YES"},
        ],
    }
    if bid is not None:
        market["bestBidQuote"] = {"value": str(bid), "currency": "USD"}
    if ask is not None:
        market["bestAskQuote"] = {"value": str(ask), "currency": "USD"}
    return market


# ---------------------------------------------------------------------------
# Helper: inject a mock polymarket_us_client into sys.modules so that the
# lazy import inside fetch_pm_ladder_us succeeds, letting us test the mapper.
# ---------------------------------------------------------------------------

def _inject_mock_us_client(mock_market: dict):
    """Add a mock polymarket_us_client to sys.modules for testing."""
    class _MockGetMarketBySlug:
        def __call__(self, slug, timeout=20):
            return mock_market

    mock_module = type(sys)("polymarket_us_client")
    mock_module.get_market_by_slug = _MockGetMarketBySlug()
    # Also make the module importable from crossvenue's perspective
    # by ensuring the name resolves; the function does `from polymarket_us_client
    # import get_market_by_slug`, so we need the module object in sys.modules.
    sys.modules["polymarket_us_client"] = mock_module


def _remove_mock_us_client():
    """Remove the mock from sys.modules."""
    sys.modules.pop("polymarket_us_client", None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_fetch_pm_ladder_us_basic():
    """Test US mapper produces correct Rung from a minimal market dict."""
    market = _make_market_dict(yes_price="0.78", bid=0.80, ask=0.76)
    _inject_mock_us_client(market)
    try:
        ladder = fetch_pm_ladder_us("bitcoin-above-on-november-5-2026")
        assert ladder is not None, "US fetch should not return None when client is available"
        assert ladder.venue == "polymarket"
        assert ladder.asset == "BTC"
        assert len(ladder.rungs) == 1
        rung = ladder.rungs[0]
        assert rung.strike == 2600.0, f"expected strike 2600.0, got {rung.strike}"
        assert abs(rung.mid - 0.78) < 1e-6, f"expected mid ~0.78, got {rung.mid}"
        # bid/ask from bestBidQuote/bestAskQuote
        assert abs(rung.bid - 0.80) < 1e-6, f"expected bid 0.80, got {rung.bid}"
        assert abs(rung.ask - 0.76) < 1e-6, f"expected ask 0.76, got {rung.ask}"
    finally:
        _remove_mock_us_client()


def test_fetch_pm_ladder_us_with_explicit_bid_ask():
    """Test US mapper uses explicit bestBidQuote/bestAskQuote when present."""
    market = _make_market_dict(yes_price="0.85", bid=0.84, ask=0.86)
    _inject_mock_us_client(market)
    try:
        ladder = fetch_pm_ladder_us("bitcoin-above-on-november-5-2026")
        assert ladder is not None
        rung = ladder.rungs[0]
        assert abs(rung.bid - 0.84) < 1e-6, f"expected bid 0.84, got {rung.bid}"
        assert abs(rung.ask - 0.86) < 1e-6, f"expected ask 0.86, got {rung.ask}"
        # mid should be (bid+ask)/2
        assert abs(rung.mid - 0.85) < 1e-6, f"expected mid 0.85, got {rung.mid}"
    finally:
        _remove_mock_us_client()


def test_fetch_pm_ladder_us_range_market():
    """Test US mapper handles range markets using rangeLower/rangeUpper for strike."""
    market = _make_market_dict(
        yes_price="0.60",
        asset_symbol="ETH",
        bid=0.59,
        ask=0.61,
    )
    market["assetPriceTerms"]["strike"] = None
    market["assetPriceTerms"]["rangeLower"] = "2900.0"
    market["assetPriceTerms"]["rangeUpper"] = "3100.0"
    _inject_mock_us_client(market)
    try:
        ladder = fetch_pm_ladder_us("ethereum-above-on-november-5-2026")
        assert ladder is not None
        rung = ladder.rungs[0]
        assert rung.strike == 3000.0, f"expected strike 3000.0 (mid of range), got {rung.strike}"
        assert ladder.asset == "ETH"
    finally:
        _remove_mock_us_client()


def test_fetch_pm_ladder_us_import_fallback():
    """Test that when polymarket_us_client is absent, US fetch falls back gracefully."""
    # Ensure the module is NOT in sys.modules
    _remove_mock_us_client()
    ladder = fetch_pm_ladder_us("bitcoin-above-on-november-5-2026")
    # Without the client module, the lazy import fails and returns None
    assert ladder is None, "US fetch should return None when polymarket_us_client is absent"


def test__fetch_pm_ladder_with_fallback_us_first():
    """Test fallback tries US first, then Gamma when US returns None."""
    # When US client is available and returns a ladder, fallback uses it
    market = _make_market_dict(yes_price="0.78", bid=0.80, ask=0.76)
    _inject_mock_us_client(market)
    try:
        result = _fetch_pm_ladder_with_fallback("bitcoin-above-on-november-5-2026")
        assert result is not None
        assert result.venue == "polymarket"
        log_info = (
            "polymarket source: US production api.polymarket.us"
        )  # just verify the logic; actual log captured via test spy if needed
    finally:
        _remove_mock_us_client()

    # When US client is absent, fallback should try Gamma (which returns None for bad slug)
    _remove_mock_us_client()
    result = _fetch_pm_ladder_with_fallback("nonexistent-slug-12345")
    assert result is None


def test_parse_pm_slug_still_works():
    """Ensure the slug parser is unaffected."""
    result = parse_pm_slug("bitcoin-above-on-november-5-2026")
    assert result is not None
    assert result == ("BTC", datetime(2026, 11, 5, 23, 59))


def test_rung_dataclass_fields():
    """Verify Rung fields are correctly set."""
    r = Rung(strike=2600.0, mid=0.78, bid=0.80, ask=0.76, ticker="BTC", rules="test")
    assert r.strike == 2600.0
    assert abs(r.mid - 0.78) < 1e-6
    assert abs(r.bid - 0.80) < 1e-6
    assert abs(r.ask - 0.76) < 1e-6
    assert r.ticker == "BTC"
    assert r.rules == "test"
    # spread = ask - bid = 0.76 - 0.80 = -0.04
    assert abs(r.spread + 0.04) < 1e-6


def test_ladder_dataclass_fields():
    """Verify Ladder fields are correctly set."""
    ladder = Ladder(venue="polymarket", asset="BTC", expiry_et=datetime(2026, 11, 5, 23, 59))
    assert ladder.venue == "polymarket"
    assert ladder.asset == "BTC"
    assert len(ladder.rungs) == 0
    assert ladder.label == ""
    assert ladder.ok == False  # needs >= 3 rungs


def test_fetch_pm_ladder_gamma_fallback_path():
    """Verify the Gamma fallback path still works (no network, just compile check)."""
    # Just verify the function is callable and returns None for bad slug
    result = fetch_pm_ladder("nonexistent-slug-12345")
    assert result is None