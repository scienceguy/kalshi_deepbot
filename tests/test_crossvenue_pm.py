"""
Tests for crossvenue.py Polymarket US API integration and fallback.

These tests verify the US mapper function and the fallback logic without
making real network calls.
"""
from __future__ import annotations

import sys
from datetime import datetime


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

_real_us_client_module = None

def _inject_mock_us_client(mock_market: dict):
    """Add a mock polymarket_us_client to sys.modules for testing."""
    global _real_us_client_module
    if _real_us_client_module is None:
        _real_us_client_module = sys.modules.get("polymarket_us_client")
        if _real_us_client_module is None:
            import polymarket_us_client as _real
            _real_us_client_module = _real
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
    """Restore the real polymarket_us_client module after an injection.

    Restores the saved real module object rather than popping the name from
    sys.modules: popping makes the next `import polymarket_us_client` (e.g. via
    unittest.mock.patch) create a brand-new module object, while modules that
    already bound a reference to the old one (e.g. poly_us_trader) keep calling
    the stale real functions — leaking real network calls into later tests.
    """
    global _real_us_client_module
    if _real_us_client_module is not None:
        sys.modules["polymarket_us_client"] = _real_us_client_module
        _real_us_client_module = None


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
    # Temporarily pop the real module so the lazy import fails; restore it
    # afterwards so later tests that bind `import polymarket_us_client` keep
    # working.
    saved = sys.modules.pop("polymarket_us_client", None)
    try:
        ladder = fetch_pm_ladder_us("bitcoin-above-on-november-5-2026")
        # Without the client module, the lazy import fails and returns None
        assert ladder is None, "US fetch should return None when polymarket_us_client is absent"
    finally:
        if saved is not None:
            sys.modules["polymarket_us_client"] = saved


def test__fetch_pm_ladder_with_fallback_us_first():
    """Test fallback tries US first, then Gamma when US returns None."""
    # When US client is available and returns a ladder, fallback uses it
    market = _make_market_dict(yes_price="0.78", bid=0.80, ask=0.76)
    _inject_mock_us_client(market)
    try:
        result = _fetch_pm_ladder_with_fallback("bitcoin-above-on-november-5-2026")
        assert result is not None
        assert result.venue == "polymarket"
    finally:
        _remove_mock_us_client()

    # When US client is absent, fallback should try Gamma (which returns None for bad slug)
    saved = sys.modules.pop("polymarket_us_client", None)
    try:
        result = _fetch_pm_ladder_with_fallback("nonexistent-slug-12345")
        assert result is None
    finally:
        if saved is not None:
            sys.modules["polymarket_us_client"] = saved


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


def test_compute_kl_divergence_basic():
    """Test compute_kl_divergence with synthetic ladders.
    
    Based on PredictionMarketsPicks worked example:
    Kalshi P(above 96k) = 0.85, Polymarket P(above 96k) = 0.60
    Symmetric KL ~ 0.166 (above ARB threshold of 0.15)
    """
    from crossvenue import Ladder, Rung, compute_kl_divergence
    from datetime import datetime, timezone, timedelta
    
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    
    # Kalshi ladder: at strike 96000, P(above) = 0.85
    # Need 3 rungs for to_cdf to work
    kalshi = Ladder("kalshi", "BTC", expiry, label="KXBTC15M-TEST")
    kalshi.rungs = [
        Rung(strike=94000.0, mid=0.95, bid=0.94, ask=0.96),
        Rung(strike=96000.0, mid=0.85, bid=0.84, ask=0.86),
        Rung(strike=98000.0, mid=0.50, bid=0.49, ask=0.51),
    ]
    
    # Polymarket ladder: same strike range, at 96000 P(above) = 0.60
    poly = Ladder("polymarket", "BTC", expiry, label="btc-above-on-nov-5-2026")
    poly.rungs = [
        Rung(strike=94000.0, mid=0.80, bid=0.79, ask=0.81),
        Rung(strike=96000.0, mid=0.60, bid=0.59, ask=0.61),
        Rung(strike=98000.0, mid=0.40, bid=0.39, ask=0.41),
    ]
    
    results = compute_kl_divergence(kalshi, poly)
    assert len(results) == 3
    
    # Check the 96000 strike where Kalshi=0.85, Poly=0.60
    # Symmetric KL should be ~0.166 (above ARB threshold of 0.15)
    r96 = [r for r in results if r["strike"] == 96000.0][0]
    assert abs(r96["kalshi_mid"] - 0.85) < 1e-6
    assert abs(r96["poly_mid"] - 0.60) < 1e-6
    assert r96["symmetric_kl"] > 0.15  # Above ARB threshold
    assert r96["kl_arb_flag"] is True
    assert r96["kl_watch_flag"] is True


def test_compute_kl_divergence_low_kl():
    """Test compute_kl_divergence when KL is below WATCH threshold."""
    from crossvenue import Ladder, Rung, compute_kl_divergence
    from datetime import datetime, timezone, timedelta
    
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    
    # Need at least 3 rungs for to_cdf
    kalshi = Ladder("kalshi", "BTC", expiry)
    kalshi.rungs = [
        Rung(strike=94000.0, mid=0.55, bid=0.54, ask=0.56),
        Rung(strike=95000.0, mid=0.52, bid=0.51, ask=0.53),
        Rung(strike=96000.0, mid=0.48, bid=0.47, ask=0.49),
    ]
    
    poly = Ladder("polymarket", "BTC", expiry)
    poly.rungs = [
        Rung(strike=94000.0, mid=0.54, bid=0.53, ask=0.55),
        Rung(strike=95000.0, mid=0.51, bid=0.50, ask=0.52),
        Rung(strike=96000.0, mid=0.49, bid=0.48, ask=0.50),
    ]
    
    results = compute_kl_divergence(kalshi, poly)
    assert len(results) == 3
    
    # Near 50/50, small gap -> low KL
    for r in results:
        assert r["symmetric_kl"] < 0.05  # Below WATCH threshold
        assert r["kl_arb_flag"] is False
        assert r["kl_watch_flag"] is False


# ═══════════════════════════════════════════════════════════════════════
# T15-31: Cross-venue up/down matcher tests
# ═══════════════════════════════════════════════════════════════════════

def test_parse_kalshi_15m_event():
    """Test parsing Kalshi 15M event ticker."""
    from crossvenue import parse_kalshi_15m_event
    from datetime import datetime
    
    # KXBTC15M-26NOV201530 -> window 15:15-15:30 ET: the ticker's HHMM is the
    # ET close, verified 2026-11-21 (KXBTC15M-26NOV210845-45 closed 13:45Z).
    result = parse_kalshi_15m_event("KXBTC15M-26NOV201530")
    assert result is not None
    asset, start, end = result
    assert asset == "BTC"
    assert start == datetime(2026, 11, 20, 15, 15)
    assert end == datetime(2026, 11, 20, 15, 30)

    # KXETH15M-26DEC152345 -> ('ETH', 2026-12-15 23:30 ET, 2026-12-15 23:45 ET)
    result = parse_kalshi_15m_event("KXETH15M-26DEC152345")
    assert result is not None
    asset, start, end = result
    assert asset == "ETH"
    assert start == datetime(2026, 12, 15, 23, 30)
    assert end == datetime(2026, 12, 15, 23, 45)
    
    # Invalid ticker
    assert parse_kalshi_15m_event("INVALID") is None
    assert parse_kalshi_15m_event("") is None


def test_parse_poly_us_updown_15m_slug():
    """Test parsing Polymarket US up/down 15m slug."""
    from crossvenue import parse_poly_us_updown_15m_slug
    from datetime import datetime
    
    # cpc-btc-updown-15m-2026-11-20-1530z -> ('BTC', 2026-11-20 10:30 ET, 2026-11-20 10:45 ET)
    # The 'z' suffix is UTC: 15:30-15:45Z is 10:30-10:45 ET in November. Verified
    # against the live venue 2026-11-21: at 12:23Z the open window was
    # cpc-btc-updown-15m-2026-11-21-1215z with start 12:15Z / end 12:30Z.
    result = parse_poly_us_updown_15m_slug("cpc-btc-updown-15m-2026-11-20-1530z")
    assert result is not None
    asset, start, end = result
    assert asset == "BTC"
    assert start == datetime(2026, 11, 20, 10, 30)
    assert end == datetime(2026, 11, 20, 10, 45)

    # cpc-eth-updown-15m-2026-12-15-2345z (December: EST all month)
    result = parse_poly_us_updown_15m_slug("cpc-eth-updown-15m-2026-12-15-2345z")
    assert result is not None
    asset, start, end = result
    assert asset == "ETH"
    assert start == datetime(2026, 12, 15, 18, 45)

    # Invalid slugs
    assert parse_poly_us_updown_15m_slug("invalid") is None
    assert parse_poly_us_updown_15m_slug("") is None
    assert parse_poly_us_updown_15m_slug("cpc-btc-updown-1h-2026-11-20-1530z") is None


def test_poly_and_kalshi_15m_clocks_convert_to_the_same_window():
    from datetime import timezone
    """The two venues' tickers are on different clocks; matching must convert.

    KXBTC15M-26NOV210730-30 closes at 07:30 ET = 12:30Z (its 15-minute window
    is 12:15-12:30Z). The live venue's slug for that same window is
    cpc-btc-updown-15m-2026-11-21-1215z. Parsing both must land on the same
    interval — reading the slug as ET put every pair five hours apart, so the
    old matcher compared Kalshi's current window against a window that had
    already settled and never found a real pair.
    """
    from crossvenue import (to_utc, parse_kalshi_15m_event,
                            parse_poly_us_updown_15m_slug)
    asset, start_et, end_et = parse_kalshi_15m_event("KXBTC15M-26NOV210730")
    # the event closes 07:30 ET = 12:30Z, so its window is 12:15-12:30Z: the
    # same interval the live venue publishes as ...-1215z.
    assert to_utc(start_et) == datetime(2026, 11, 21, 12, 15, tzinfo=timezone.utc)
    assert to_utc(end_et) == datetime(2026, 11, 21, 12, 30, tzinfo=timezone.utc)
    p_asset, p_start, p_end = parse_poly_us_updown_15m_slug(
        "cpc-btc-updown-15m-2026-11-21-1215z")
    assert (p_asset, p_start, p_end) == (asset, start_et, end_et)


def test_updown_match_dataclass_fields():
    """Test UpDownMatch dataclass has all required fields."""
    from crossvenue import UpDownMatch
    from datetime import datetime
    
    match = UpDownMatch(
        kalshi_event="KXBTC15M-26NOV201530",
        kalshi_yes_mid=0.95,
        kalshi_no_mid=0.05,
        polymarket_slug="cpc-btc-updown-15m-2026-11-20-1530z",
        poly_up_mid=0.94,
        poly_down_mid=0.06,
        asset="BTC",
        window_start_et=datetime(2026, 11, 20, 15, 30),
        window_end_et=datetime(2026, 11, 20, 15, 45),
        minutes_left=8.5,
        deviation_cents=1.0,
        basis_risk_bps=5.0,
        tie_rule_risk=True
    )
    assert match.kalshi_event == "KXBTC15M-26NOV201530"
    assert match.kalshi_yes_mid == 0.95
    assert match.poly_up_mid == 0.94
    assert match.deviation_cents == 1.0
    assert match.tie_rule_risk is True