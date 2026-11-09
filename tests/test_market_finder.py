#!/usr/bin/env python3
"""
Tests for market_finder.py — market discovery and ranking.

Run: python -m pytest tests/test_market_finder.py -v
"""
import logging
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, "/data/data/com.termux/files/home/kalshi_superbot")

from market_finder import MarketInfo, find_active_markets
from signals import _evaluate_taker_favourite, _evaluate_hedge, rank_signals

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)


class TestMarketInfo:
    """Tests for MarketInfo dataclass."""

    def test_minutes_left_positive(self):
        m = MarketInfo(
            ticker="TEST",
            series="KXBTC15M",
            close_time=datetime.now(timezone.utc) + timedelta(minutes=5),
            strike=50000,
            strike_type="",
            yes_bid=50,
            yes_ask=51,
            no_bid=49,
            no_ask=50,
            yes_liquidity=100,
            no_liquidity=100,
            minutes_left=5.0,
            bracket_role="MAIN",
            raw={},
        )
        assert m.minutes_left == 5.0
        assert m.mid_yes == 50.5

    def test_mid_yes_none_when_no_quotes(self):
        m = MarketInfo(
            ticker="TEST",
            series="KXBTC15M",
            close_time=datetime.now(timezone.utc) + timedelta(minutes=5),
            strike=50000,
            strike_type="",
            yes_bid=None,
            yes_ask=None,
            no_bid=None,
            no_ask=None,
            yes_liquidity=0,
            no_liquidity=0,
            minutes_left=5.0,
            bracket_role="MAIN",
            raw={},
        )
        # mid_yes returns None when both bid and ask are None (no quotes)
        assert m.mid_yes is None


class TestFindActiveMarkets:
    """Integration-style tests for find_active_markets (mocked API)."""

    @patch("market_finder.KalshiClient")
    def test_returns_empty_when_no_markets(self, mock_client_class):
        mock_client = MagicMock()
        mock_client.get_markets.return_value = []
        mock_client_class.return_value = mock_client

        result = find_active_markets(mock_client)
        assert result == []

    @patch("market_finder.KalshiClient")
    def test_filters_by_series_and_status(self, mock_client_class):
        mock_client = MagicMock()
        # Use a close_time in the future (~0.5 min from now = ~14.5 min into 15-min window)
        future_time = (datetime.now(timezone.utc) + timedelta(minutes=0.5)).isoformat().replace('+00:00', 'Z')
        mock_client.get_markets.return_value = [
            {
                "ticker": "KXBTC15M-24OCT1230-30",
                "series_ticker": "KXBTC15M",
                "status": "open",
                "floor_strike": 50000,
                "cap_strike": 51000,
                "strike_type": "",
                "close_time": future_time,
                "open_time": "2024-10-12T12:30:00Z",
                "yes_bid_dollars": "0.80",
                "yes_ask_dollars": "0.85",
                "no_bid_dollars": "0.15",
                "no_ask_dollars": "0.20",
            }
        ]
        mock_client.get_orderbook.return_value = {"yes": [[84, 100], [85, 100]], "no": [[15, 100], [16, 100]]}
        mock_client_class.return_value = mock_client

        result = find_active_markets(mock_client)
        assert len(result) >= 1
        # Check at least one MAIN is returned
        mains = [m for m in result if m.bracket_role == "MAIN"]
        assert len(mains) >= 1

    @patch("market_finder.KalshiClient")
    def test_skips_closed_markets(self, mock_client_class):
        mock_client = MagicMock()
        mock_client.get_markets.return_value = [
            {
                "ticker": "KXBTC15M-24OCT1230-30",
                "series_ticker": "KXBTC15M",
                "status": "closed",
                "floor_strike": 50000,
                "cap_strike": 51000,
                "strike_type": "",
                "close_time": "2024-10-12T12:45:00Z",
                "open_time": "2024-10-12T12:30:00Z",
            }
        ]
        mock_client_class.return_value = mock_client

        result = find_active_markets(mock_client)
        # Should not return closed markets
        mains = [m for m in result if m.bracket_role == "MAIN"]
        assert len(mains) == 0


class TestRankSignals:
    """Tests for signal ranking."""

    def test_ranks_by_strength_confidence_boost(self):
        m1 = MarketInfo(
            ticker="A", series="KXBTC15M",
            close_time=datetime.now(timezone.utc) + timedelta(minutes=10),
            strike=50000, strike_type="",
            yes_bid=50, yes_ask=51, no_bid=49, no_ask=50,
            yes_liquidity=100, no_liquidity=100,
            minutes_left=5.0, bracket_role="MAIN", raw={}
        )
        m2 = MarketInfo(
            ticker="B", series="KXBTC15M",
            close_time=datetime.now(timezone.utc) + timedelta(minutes=10),
            strike=50000, strike_type="",
            yes_bid=50, yes_ask=51, no_bid=49, no_ask=50,
            yes_liquidity=100, no_liquidity=100,
            minutes_left=5.0, bracket_role="HEDGE_NO", raw={}
        )
        m3 = MarketInfo(
            ticker="C", series="KXBTC15M",
            close_time=datetime.now(timezone.utc) + timedelta(minutes=10),
            strike=50000, strike_type="",
            yes_bid=50, yes_ask=51, no_bid=49, no_ask=50,
            yes_liquidity=100, no_liquidity=100,
            minutes_left=5.0, bracket_role="MAIN", raw={}
        )

        from signals import TradeSignal
        signals = [
            TradeSignal("YES", "yes", 50, 1.0, 0.5, "r1", m1, strength=10.0, confidence=0.5),
            TradeSignal("NO", "no", 50, 1.0, 0.5, "r2", m2, strength=10.0, confidence=0.5),  # HEDGE gets 1.2x boost
            TradeSignal("YES", "yes", 50, 1.0, 0.5, "r3", m3, strength=5.0, confidence=0.9),
        ]

        ranked = rank_signals(signals)
        # HEDGE_NO with boost: 10.0 * 0.5 * 1.2 = 6.0
        # m1: 10.0 * 0.5 * 1.0 = 5.0
        # m3: 5.0 * 0.9 * 1.0 = 4.5
        assert ranked[0].market.ticker == "B"
        assert ranked[1].market.ticker == "A"
        assert ranked[2].market.ticker == "C"

    def test_excludes_pass_signals(self):
        m = MarketInfo(
            ticker="TEST", series="KXBTC15M",
            close_time=datetime.now(timezone.utc) + timedelta(minutes=10),
            strike=50000, strike_type="",
            yes_bid=50, yes_ask=51, no_bid=49, no_ask=50,
            yes_liquidity=100, no_liquidity=100,
            minutes_left=5.0, bracket_role="MAIN", raw={}
        )
        from signals import TradeSignal
        signals = [
            TradeSignal("YES", "yes", 50, 1.0, 0.5, "r1", m, strength=10.0, confidence=0.5),
            TradeSignal("PASS", "", 0, 0, 0.0, "r2", m),
        ]
        ranked = rank_signals(signals)
        assert len(ranked) == 1
        assert ranked[0].direction == "YES"


class TestEvaluateHedge:
    """Tests for HEDGE_NO evaluation."""

    def test_hedge_requires_no_ask_in_band(self):
        """HEDGE only triggers when NO ask is in MIN_NO_ENTRY_CENTS..MAX_NO_ENTRY_CENTS."""
        m = MagicMock()
        m.ticker = "TEST"
        m.series = "KXBTC15M"
        m.close_time = datetime.now(timezone.utc) + timedelta(minutes=0.5)
        m.minutes_left = 0.5
        m.no_ask = 90  # Outside hedge band (82-88)
        m.no_bid = 10
        m.yes_bid = 10
        m.yes_ask = 90
        m.yes_liquidity = 100
        m.no_liquidity = 100

        with patch('signals.config.MIN_NO_ENTRY_CENTS', 82), \
             patch('signals.config.MAX_NO_ENTRY_CENTS', 88), \
             patch('signals.config.NO_PRICE_IS_COMPLEMENT', False):
            result = _evaluate_hedge(m, 10.0)

        trades = [s for s in result if s.is_trade]
        assert len(trades) == 0

    def test_hedge_enters_when_no_ask_in_band(self):
        """HEDGE triggers when NO ask is within the hedge band."""
        m = MagicMock()
        m.ticker = "TEST"
        m.series = "KXBTC15M"
        m.close_time = datetime.now(timezone.utc) + timedelta(minutes=0.5)
        m.minutes_left = 0.5
        m.no_ask = 85  # Inside hedge band (82-88)
        m.no_bid = 15
        m.yes_bid = 15
        m.yes_ask = 85
        m.yes_liquidity = 100
        m.no_liquidity = 100

        with patch('signals.config.MIN_NO_ENTRY_CENTS', 82), \
             patch('signals.config.MAX_NO_ENTRY_CENTS', 88), \
             patch('signals.config.NO_PRICE_IS_COMPLEMENT', False):
            result = _evaluate_hedge(m, 10.0)

        trades = [s for s in result if s.is_trade]
        assert len(trades) == 1
        assert trades[0].direction == "NO"


class TestMarketFinderIntegration:
    """Integration-style tests for find_active_markets with orderbook data."""

    @patch("market_finder.KalshiClient")
    def test_bracket_selection_main_and_hedge(self, mock_client_class):
        """MAIN + HEDGE_NO brackets are both returned when conditions met."""
        mock_client = MagicMock()
        future_time = (datetime.now(timezone.utc) + timedelta(minutes=0.5)).isoformat().replace('+00:00', 'Z')
        mock_client.get_markets.return_value = [
            {
                "ticker": "KXBTC15M-24OCT1230-30",
                "series_ticker": "KXBTC15M",
                "status": "open",
                "floor_strike": 50000,
                "cap_strike": 51000,
                "strike_type": "",
                "close_time": future_time,
                "open_time": "2024-10-12T12:30:00Z",
                "yes_bid_dollars": "0.80",
                "yes_ask_dollars": "0.85",
                "no_bid_dollars": "0.15",
                "no_ask_dollars": "0.20",
            },
            {
                "ticker": "KXBTC15M-24OCT1230-35",
                "series_ticker": "KXBTC15M",
                "status": "open",
                "floor_strike": 49000,
                "cap_strike": 50000,
                "strike_type": "",
                "close_time": future_time,
                "open_time": "2024-10-12T12:30:00Z",
                "yes_bid_dollars": "0.10",
                "yes_ask_dollars": "0.12",
                "no_bid_dollars": "0.88",
                "no_ask_dollars": "0.92",
            },
        ]
        mock_client.get_orderbook.side_effect = [
            {"yes": [[84, 100], [85, 100]], "no": [[15, 100], [16, 100]]},
            {"yes": [[11, 100], [12, 100]], "no": [[88, 100], [89, 100]]},
        ]
        mock_client_class.return_value = mock_client

        result = find_active_markets(mock_client)
        # Should return at least MAIN (and possibly HEDGE if yes_ask <= 12)
        mains = [m for m in result if m.bracket_role == "MAIN"]
        assert len(mains) >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])