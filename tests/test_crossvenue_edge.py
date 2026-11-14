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
