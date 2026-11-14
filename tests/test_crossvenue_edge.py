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
