"""
Tests for crossvenue.py Polymarket US API integration and fallback.

These tests verify the US mapper function and the fallback logic without
making real network calls.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone


from crossvenue import (
    Ladder,
    Rung,
    fetch_pm_ladder_us,
    fetch_pm_ladder,
    fetch_kalshi_ladder,
    candidate_pm_slugs,
    three_way,
    _fetch_pm_ladder_with_fallback,
    _utc_to_naive_et,
    parse_pm_slug,
    to_utc,
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

# ---------------------------------------------------------------------------
# 15M up/down windows (live-verified 2026-11-21): one market per side. The
# event slug (btc-updown-15m-<date>-<hhmm>z, window START in UTC) 500s on
# /v1/market/slug; the cpc- market slug resolves there, so the US mapper
# carries the pair. The strike is the opening 60s BRTI average and both
# venues settle on BRTI, so a same-strike pair is the same contract.
# ---------------------------------------------------------------------------

def _make_updown_market(price_to_beat="60236.51", bid="0.58", ask="0.59"):
    """A US 15M up/down market dict in the shape the live API returns."""
    return {
        "assetPriceTerms": {
            "strike": None,
            "asset": {"symbol": "btc"},
            "assetName": "Bitcoin",
            "marketType": "",
            "windowStart": "2026-11-21T20:00:00Z",
            "windowEnd": "2026-11-21T20:15:00Z",
            "horizon": "15m",
            "indexSymbol": "BRTI",
            # live-verified shape: a {value, currency} quote object
            "priceToBeat": {"value": price_to_beat, "currency": "USD"},
            "direction": "up",
            "rangeLower": None,
            "rangeUpper": None,
        },
        "bestBidQuote": {"value": bid, "currency": "USD"},
        "bestAskQuote": {"value": ask, "currency": "USD"},
        "marketSides": [{"price": bid, "long": True,
                         "marketSideType": "MARKET_SIDE_TYPE_INSTRUMENT"}],
    }


def test_parse_pm_slug_15m_updown_gives_window_end_in_et():
    """The slug carries the window START in UTC; the expiry is windowEnd."""
    for slug, asset in (("btc-updown-15m-2026-11-21-1930z", "BTC"),
                        ("cpc-eth-updown-15m-2026-11-21-1930z", "ETH")):
        parsed = parse_pm_slug(slug)
        assert parsed is not None, slug
        assert parsed[0] == asset
        # 19:30Z + 15m = 19:45Z = 14:45 ET in November (EST)
        assert parsed[1] == datetime(2026, 11, 21, 14, 45)


def test_candidate_pm_slugs_leads_with_the_15m_market_slug():
    """A 15M close time produces the cpc- updown slug first, ladder after."""
    expiry_et = datetime(2026, 11, 21, 15, 15)  # KXBTC15M-26NOV211515 = 20:15Z
    slugs = candidate_pm_slugs("BTC", expiry_et)
    assert slugs[0] == "cpc-btc-updown-15m-2026-11-21-2000z"
    assert slugs[-1] == "bitcoin-above-on-november-21-2026-3pm-et"


def test_fetch_pm_ladder_us_15m_updown_single_rung():
    """The US mapper reads priceToBeat and the quotes into one rung."""
    _inject_mock_us_client(_make_updown_market())
    try:
        ladder = fetch_pm_ladder_us("cpc-btc-updown-15m-2026-11-21-2000z")
    finally:
        _remove_mock_us_client()
    assert ladder is not None
    assert len(ladder.rungs) == 1
    rung = ladder.rungs[0]
    assert rung.strike == 60236.51
    assert rung.mid == 0.585
    # expiry is the windowEnd, in ET like every other ladder
    # (20:00Z + 15m = 20:15Z = 15:15 ET in November)
    assert ladder.expiry_et == datetime(2026, 11, 21, 15, 15)


def test_fetch_kalshi_ladder_accepts_greater_or_equal(monkeypatch):
    """15M up/down markets quote greater_or_equal, not greater."""
    class _Resp:
        @staticmethod
        def raise_for_status():
            pass

        @staticmethod
        def json():
            return {"markets": [{
                "ticker": "KXBTC15M-26NOV211515-45",
                "strike_type": "greater_or_equal",
                "floor_strike": 60236.51,
                "yes_bid_dollars": "0.79",
                "yes_ask_dollars": "0.80",
                "last_price_dollars": "0.79",
            }]}

    class _FakeRequests:
        get = staticmethod(lambda *a, **k: _Resp())

    import crossvenue
    monkeypatch.setattr(crossvenue, "requests", _FakeRequests)
    ladder = fetch_kalshi_ladder("KXBTC15M-26NOV211515")
    assert ladder is not None
    assert len(ladder.rungs) == 1
    assert ladder.rungs[0].strike == 60236.51
    assert ladder.rungs[0].mid == 0.795


def _one_rung_ladder(venue, strike, bid, ask, expiry_et):
    ladder = Ladder(venue, "BTC", expiry_et, label=venue)
    ladder.rungs.append(Rung(strike, (bid + ask) / 2.0, bid, ask, "", ""))
    return ladder


def test_three_way_single_market_pair_is_one_row(monkeypatch):
    """One market per side: a row per window, not per ladder rung."""
    import sys
    import types
    stub = types.ModuleType("binance_feed")
    stub.prob_above = lambda spot, strike, sigma, minutes: 0.62
    monkeypatch.setitem(sys.modules, "binance_feed", stub)

    expiry_et = datetime(2026, 11, 21, 14, 30)  # close 19:30Z
    now = datetime(2026, 11, 21, 19, 15, tzinfo=timezone.utc)
    rows = three_way(
        _one_rung_ladder("kalshi", 60236.51, 0.79, 0.80, expiry_et),
        _one_rung_ladder("polymarket", 60236.51, 0.58, 0.59, expiry_et),
        spot=60236.51, sigma_min=0.003, now=now)
    assert len(rows) == 1
    row = rows[0]
    assert row["strike"] == 60236.51
    assert row["kalshi"] == 0.795
    assert row["poly"] == 0.585
    assert row["kalshi_edge_c"] == (0.62 - 0.795) * 100
    assert row["minutes_left"] == 15.0


def test_three_way_single_market_strike_mismatch_is_no_pair():
    """Same windowEnd, different opening average = a different window."""
    expiry_et = datetime(2026, 11, 21, 14, 30)
    now = datetime(2026, 11, 21, 19, 15, tzinfo=timezone.utc)
    rows = three_way(
        _one_rung_ladder("kalshi", 60236.51, 0.79, 0.80, expiry_et),
        _one_rung_ladder("polymarket", 60312.44, 0.58, 0.59, expiry_et),
        spot=60236.51, sigma_min=0.003, now=now)
    assert rows == []


def test_utc_to_naive_et_round_trips_both_dst_halves():
    for naive_et in (datetime(2026, 7, 15, 12, 0), datetime(2026, 11, 21, 12, 0)):
        assert _utc_to_naive_et(to_utc(naive_et)) == naive_et
