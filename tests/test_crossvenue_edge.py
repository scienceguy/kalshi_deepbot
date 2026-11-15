"""
Unit tests for crossvenue_edge.py.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import crossvenue_edge


def test_extract_asset_and_strike():
    market = {
        "slug": "cpc-btc-150k-12-31-2026",
        "assetPriceTerms": {
            "asset": {"symbol": "btc"},
            "strike": {"value": "150000.00"},
            "windowEnd": "2026-12-31T23:59:59Z",
        },
    }
    symbol, strike, end_dt = crossvenue_edge.extract_asset_and_strike(market)
    assert symbol == "BTC"
    assert strike == 150000.0
    assert end_dt is not None


def test_evaluate_crossvenue_edge_positive():
    end_time = datetime.now(timezone.utc) + timedelta(days=30)
    market = {
        "slug": "cpc-btc-test-high-strike",
        "assetPriceTerms": {
            "asset": {"symbol": "btc"},
            "strike": {"value": "200000.00"},
            "windowEnd": end_time.isoformat(),
        },
        "outcomePrices": ["0.02", "0.99"],
    }
    # BTC at $67,000, strike at $200,000 with 30 days left -> Short probability is ~99.9%
    with patch("crossvenue_edge.get_cached_vol", return_value=0.60):
        res = crossvenue_edge.evaluate_crossvenue_edge(market, spot_override=67000.0)
    assert res is not None
    assert res["symbol"] == "BTC"
    assert res["best_side"] == "no"
    assert res["best_price"] == 0.99
    assert res["best_edge_cents"] > 0.0


def test_evaluate_crossvenue_edge_missing_terms():
    market = {
        "slug": "tec-mls-general-future",
        "question": "Who will win?",
    }
    res = crossvenue_edge.evaluate_crossvenue_edge(market)
    assert res is None


def test_kalshi_implied_prob_at_strike_interpolates():
    cdf = [(90.0, 0.80), (100.0, 0.50), (110.0, 0.20)]
    assert crossvenue_edge.kalshi_implied_prob_at_strike(cdf, 100.0) == 0.50
    assert crossvenue_edge.kalshi_implied_prob_at_strike(cdf, 95.0) == 0.65
    assert crossvenue_edge.kalshi_implied_prob_at_strike(cdf, 105.0) == 0.35
    assert crossvenue_edge.kalshi_implied_prob_at_strike(cdf, 80.0) is None  # below range
    assert crossvenue_edge.kalshi_implied_prob_at_strike(cdf, 120.0) is None  # above range
    assert crossvenue_edge.kalshi_implied_prob_at_strike([], 100.0) is None


def test_evaluate_crossvenue_edge_cdf_poly_underprices_favorite():
    end_time = datetime.now(timezone.utc) + timedelta(minutes=10)
    market = {
        "slug": "cpc-btc-updown-15m-favorite",
        "assetPriceTerms": {
            "asset": {"symbol": "btc"},
            "strike": {"value": "100000.00"},
            "windowEnd": end_time.isoformat(),
        },
        "outcomePrices": ["0.90", "0.10"],
    }
    # Kalshi's CDF says P(BTC > 100k) = 0.97; Polymarket prices YES at 0.90.
    # edge = (0.97 - 0.90) * 100 = +7c -> Polymarket underprices the favorite.
    kalshi_cdf = [(99000.0, 0.98), (100000.0, 0.97), (101000.0, 0.95)]
    res = crossvenue_edge.evaluate_crossvenue_edge_cdf(market, kalshi_cdf,
                                                       spot_override=101000.0)
    assert res is not None
    assert res["edge_source"] == "kalshi_cdf"
    assert res["ref_prob"] == 0.97
    assert res["best_side"] == "yes"
    assert res["best_edge_cents"] == 7.0


def test_evaluate_crossvenue_edge_cdf_outside_cdf_range():
    end_time = datetime.now(timezone.utc) + timedelta(minutes=10)
    market = {
        "slug": "cpc-btc-updown-15m-wing",
        "assetPriceTerms": {
            "asset": {"symbol": "btc"},
            "strike": {"value": "200000.00"},
            "windowEnd": end_time.isoformat(),
        },
        "outcomePrices": ["0.01", "0.99"],
    }
    kalshi_cdf = [(99000.0, 0.98), (100000.0, 0.97), (101000.0, 0.95)]
    # Strike 200k is outside the Kalshi CDF range -> refuse to extrapolate.
    assert crossvenue_edge.evaluate_crossvenue_edge_cdf(market, kalshi_cdf,
                                                        spot_override=101000.0) is None


# ── A7: cache freshness and one sigma per decision ──────────────────────────

def test_stale_spot_cache_beyond_max_age_returns_none(monkeypatch):
    """A spot cached longer than the max age is not reused after a failed refresh."""
    monkeypatch.setattr(crossvenue_edge, "_spot_cache", {})
    monkeypatch.setattr(crossvenue_edge.time, "time", lambda: 10_000.0)
    crossvenue_edge._spot_cache["BTC"] = (10_000.0 - 31.0, 67000.0)
    with patch("crossvenue_edge.binance_feed.spot_price", side_effect=RuntimeError("down")):
        assert crossvenue_edge.get_cached_spot("BTC") is None


def test_fresh_spot_cache_survives_a_failed_refresh(monkeypatch):
    monkeypatch.setattr(crossvenue_edge, "_spot_cache", {})
    monkeypatch.setattr(crossvenue_edge.time, "time", lambda: 10_000.0)
    crossvenue_edge._spot_cache["BTC"] = (10_000.0 - 10.0, 67000.0)
    with patch("crossvenue_edge.binance_feed.spot_price", side_effect=RuntimeError("down")):
        assert crossvenue_edge.get_cached_spot("BTC") == 67000.0


def test_stale_vol_cache_beyond_max_age_returns_none(monkeypatch):
    """Vol cached longer than the max age is dropped, not reused indefinitely."""
    monkeypatch.setattr(crossvenue_edge, "_vol_cache", {})
    monkeypatch.setattr(crossvenue_edge.time, "time", lambda: 10_000.0)
    crossvenue_edge._vol_cache["BTC"] = (10_000.0 - 601.0, 0.9)
    with patch("crossvenue_edge.binance_feed.composite_sigma", side_effect=RuntimeError("down")):
        assert crossvenue_edge.get_cached_vol("BTC") is None


def _btc_market(minutes_ahead=30, strike="200000.00"):
    end_time = datetime.now(timezone.utc) + timedelta(minutes=minutes_ahead)
    return {
        "slug": "cpc-btc-a7",
        "assetPriceTerms": {
            "asset": {"symbol": "btc"},
            "strike": {"value": strike},
            "windowEnd": end_time.isoformat(),
        },
        "outcomePrices": ["0.02", "0.99"],
    }


def test_missing_vol_produces_no_opportunity(monkeypatch):
    """No usable vol means no edge: the evaluation refuses to trade blind."""
    with patch("crossvenue_edge.get_cached_vol", return_value=None):
        assert crossvenue_edge.evaluate_crossvenue_edge(
            _btc_market(), spot_override=67000.0) is None


def test_sigma_floor_unifies_edge_and_cushion():
    """The ref_prob and the cushion z in one result use the same sigma."""
    import math
    import pytest
    with patch("crossvenue_edge.get_cached_vol", return_value=0.40):
        res = crossvenue_edge.evaluate_crossvenue_edge(
            _btc_market(), spot_override=67000.0, sigma_floor=0.80)
    assert res is not None
    assert res["vol"] == 0.80
    expected_ref = crossvenue_edge.gbm_probability_yes_wins(
        spot_price=67000.0, strike=200000.0,
        minutes_left=res["minutes_left"], volatility_annualized=0.80)
    assert res["ref_prob"] == pytest.approx(expected_ref, abs=1e-3)
    sigma_per_sqrt_min = 0.80 / math.sqrt(525600.0)
    expected_z = abs(math.log(67000.0 / 200000.0)) / (
        sigma_per_sqrt_min * math.sqrt(res["minutes_left"]))
    assert res["cushion_z"] == pytest.approx(expected_z, abs=1e-2)
