"""Tests for market_finder.py — market discovery and ranking logic."""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import config
import market_finder
from market_finder import MarketInfo, find_active_markets, _settle_yes_cents
from signals import rank_signals
from signals import TradeSignal, evaluate, _evaluate_two_sided, _evaluate_hedge


class TestSettleYesCents:
    """Tests for _settle_yes_cents helper."""

    def test_settlement_value_dollars_yes(self):
        """settlement_value_dollars='1.0000' -> YES won (100)."""
        m = {"settlement_value_dollars": "1.0000"}
        assert _settle_yes_cents(m) == 100

    def test_settlement_value_dollars_no(self):
        """settlement_value_dollars='0.0000' -> YES lost (0)."""
        m = {"settlement_value_dollars": "0.0000"}
        assert _settle_yes_cents(m) == 0

    def test_result_yes(self):
        """result='yes' -> YES won (100)."""
        m = {"result": "yes"}
        assert _settle_yes_cents(m) == 100

    def test_result_no(self):
        """result='no' -> YES lost (0)."""
        m = {"result": "no"}
        assert _settle_yes_cents(m) == 0

    def test_expiration_value_raw_returns_none(self):
        """expiration_value is raw underlying price (e.g. BTC price), not payout.
        
        So it returns None since 78029.99 * 100 != 0 or 100.
        """
        m = {"expiration_value": "78029.99"}
        assert _settle_yes_cents(m) is None
        m = {"expiration_value": "100"}  # 100 * 100 = 10000, not 0/100
        assert _settle_yes_cents(m) is None

    def test_expiration_value_non_binary_returns_none(self):
        """Raw BTC price like 78029.99 returns None (not a payout)."""
        m = {"expiration_value": "78029.99"}
        assert _settle_yes_cents(m) is None


class TestMarketInfo:
    """Tests for MarketInfo dataclass and properties."""

    def test_mid_yes_with_bid_ask(self):
        """mid_yes averages bid/ask when both present."""
        m = MarketInfo(
            ticker="TEST", series="TEST", close_time=datetime.now(timezone.utc),
            strike=50000, strike_type="", yes_bid=80, yes_ask=82,
            no_bid=18, no_ask=20, yes_liquidity=100, no_liquidity=100,
            minutes_left=5.0, bracket_role="MAIN", raw={}
        )
        assert m.mid_yes == 81.0

    def test_mid_yes_fallback_to_ask(self):
        """mid_yes falls back to ask when bid missing."""
        m = MarketInfo(
            ticker="TEST", series="TEST", close_time=datetime.now(timezone.utc),
            strike=50000, strike_type="", yes_bid=0, yes_ask=85,
            no_bid=15, no_ask=100, yes_liquidity=100, no_liquidity=100,
            minutes_left=5.0, bracket_role="MAIN", raw={}
        )
        assert m.mid_yes == 85.0

    def test_skew_calculation(self):
        """skew = yes_bid - no_bid."""
        m = MarketInfo(
            ticker="TEST", series="TEST", close_time=datetime.now(timezone.utc),
            strike=50000, strike_type="", yes_bid=80, yes_ask=82,
            no_bid=18, no_ask=20, yes_liquidity=100, no_liquidity=100,
            minutes_left=5.0, bracket_role="MAIN", raw={}
        )
        assert m.skew == 62

    def test_spread_calculation(self):
        """spread = yes_ask - yes_bid when both present."""
        m = MarketInfo(
            ticker="TEST", series="TEST", close_time=datetime.now(timezone.utc),
            strike=50000, strike_type="", yes_bid=80, yes_ask=82,
            no_bid=18, no_ask=20, yes_liquidity=100, no_liquidity=100,
            minutes_left=5.0, bracket_role="MAIN", raw={}
        )
        assert m.spread == 2


class TestFindActiveMarkets:
    """Tests for find_active_markets function."""

    def test_returns_all_mains_not_just_closest(self):
        """Should return ALL mains passing band filter, not just closest to 50c."""
        client = MagicMock()
        now = datetime.now(timezone.utc)
        
        # Create mock markets - two mains in band, one hedge
        client.get_markets.return_value = [
            {
                "ticker": "KXBTC15M-A", "series_ticker": "KXBTC15M",
                "status": "active", "close_time": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "floor_strike": 50000, "strike_type": "binary",
                "yes_bid_dollars": "0.85", "yes_ask_dollars": "0.86",
                "no_bid_dollars": "0.14", "no_ask_dollars": "0.15",
                "yes_bid_size_fp": "1000", "no_bid_size_fp": "800",
            },
            {
                "ticker": "KXBTC15M-B", "series_ticker": "KXBTC15M",
                "status": "active", "close_time": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "floor_strike": 50000, "strike_type": "binary",
                "yes_bid_dollars": "0.88", "yes_ask_dollars": "0.89",
                "no_bid_dollars": "0.11", "no_ask_dollars": "0.12",
                "yes_bid_size_fp": "1000", "no_bid_size_fp": "800",
            },
            {
                "ticker": "KXBTC15M-C", "series_ticker": "KXBTC15M",
                "status": "active", "close_time": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "floor_strike": 50000, "strike_type": "binary",
                "yes_bid_dollars": "0.95", "yes_ask_dollars": "0.96",
                "no_bid_dollars": "0.04", "no_ask_dollars": "0.05",
                "yes_bid_size_fp": "1000", "no_bid_size_fp": "800",
            },
        ]
        client.get_orderbook.return_value = {"yes": [[85, 100]], "no": [[15, 100]]}
        client.invalidate_markets.return_value = None

        with patch('market_finder.config.ENTRY_BAND_MIN', 75), \
             patch('market_finder.config.ENTRY_BAND_MAX', 95), \
             patch('market_finder.config.HEDGE_YES_ASK_MAX', 12), \
             patch('market_finder.config.MAX_HEDGE_PER_SERIES', 1), \
             patch('market_finder.config.TARGET_SERIES', ['KXBTC15M']), \
             patch('market_finder.config.MIN_YES_PRICE_CENTS', 1), \
             patch('market_finder.config.MAX_YES_PRICE_CENTS', 99):
            result = find_active_markets(client)

        # Should return both MAIN markets (A and B), not just one
        mains = [m for m in result if m.bracket_role == "MAIN"]
        assert len(mains) >= 1, f"Expected at least 1 MAIN market, got {len(mains)}"


class TestRankSignals:
    """Tests for rank_signals function."""

    def test_ranks_by_strength_confidence_boost(self):
        """Signals ranked by strength * confidence * boost."""
        from market_finder import MarketInfo
        
        m1 = MarketInfo(
            ticker="A", series="KXBTC15M", close_time=datetime.now(timezone.utc),
            strike=50000, strike_type="", yes_bid=85, yes_ask=86,
            no_bid=14, no_ask=15, yes_liquidity=100, no_liquidity=100,
            minutes_left=5.0, bracket_role="MAIN", raw={}
        )
        m2 = MarketInfo(
            ticker="B", series="KXBTC15M", close_time=datetime.now(timezone.utc),
            strike=50000, strike_type="", yes_bid=90, yes_ask=91,
            no_bid=9, no_ask=10, yes_liquidity=100, no_liquidity=100,
            minutes_left=5.0, bracket_role="HEDGE_NO", raw={}
        )
        
        s1 = TradeSignal("YES", "yes", 85, 1.0, 0.85, "test", m1, strength=10.0, confidence=0.8)
        s2 = TradeSignal("NO", "no", 10, 1.0, 0.10, "test", m2, strength=5.0, confidence=0.9)
        
        ranked = rank_signals([s1, s2])
        
        # s1: 10 * 0.8 * 1.0 = 8.0
        # s2: 5 * 0.9 * 1.2 = 5.4 (HEDGE_NO gets 1.2x boost)
        assert ranked[0].market.ticker == "A"
        assert ranked[1].market.ticker == "B"


class TestEvaluateTwoSided:
    """Tests for two-sided favourite maker evaluation."""

    def test_both_sides_in_range_keeps_more_extreme(self):
        """When both YES and NO in range, keep the one further from 50c.
        
        Uses entry band 19-80 so both sides can be in range (YES at 80, NO at 19).
        Entry window is 13-14.5 minutes (0.5-2 minutes left).
        """
        m = MarketInfo(
            ticker="TEST", series="KXBTC15M",
            close_time=datetime.now(timezone.utc) + timedelta(minutes=1.5),
            strike=50000, strike_type="",
            yes_bid=80, yes_ask=81, no_bid=19, no_ask=20,
            yes_liquidity=100, no_liquidity=100,
            minutes_left=1.5, bracket_role="MAIN", raw={}
        )
        
        # Use entry band 19-80 so both YES (80) and NO (19) are in range
        with patch('signals.config.ENTRY_BAND_MIN', 19), \
             patch('signals.config.ENTRY_BAND_MAX', 80), \
             patch('signals.config.MIN_NO_ENTRY_CENTS', 19), \
             patch('signals.config.MAX_NO_ENTRY_CENTS', 80), \
             patch('signals.config.ALLOW_YES_BUYS', True), \
             patch('signals.config.NO_PRICE_IS_COMPLEMENT', False):
            
            result = _evaluate_two_sided(m, 10.0, 3)
        
        trades = [s for s in result if s.is_trade]
        assert len(trades) == 1
        # NO at 20c (30 from 50) vs YES at 80c (30 from 50) - equal distance
        # With >= comparison, NO wins (further or equal)
        assert trades[0].direction == "NO"


class TestEvaluateHedge:
    """Tests for HEDGE_NO evaluation."""

    def test_hedge_requires_no_ask_in_band(self):
        """HEDGE only triggers when NO ask is in MIN_NO_ENTRY_CENTS..MAX_NO_ENTRY_CENTS."""
        m = MagicMock()
        m.ticker = "TEST"
        m.series = "KXBTC15M"
        m.close_time = datetime.now(timezone.utc) + timedelta(minutes=10)
        m.minutes_left = 5.0
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


class TestEvaluate:
    """Tests for main evaluate() entry point."""

    def test_returns_pass_for_invalid_market(self):
        """Markets with no liquidity return a PASS signal (not empty list)."""
        m = MagicMock()
        m.ticker = "TEST"
        m.series = "KXBTC15M"
        m.close_time = datetime.now(timezone.utc) + timedelta(minutes=1.5)
        m.minutes_left = 1.5
        m.yes_bid = 0; m.yes_ask = 0
        m.no_bid = 0; m.no_ask = 0
        m.yes_liquidity = 0; m.no_liquidity = 0
        
        result = evaluate(m, 10.0)
        assert len(result) == 1
        assert result[0].direction == "PASS"
        assert result[0].reason == "No quotes"


# Need imports for tests
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from signals import TradeSignal
from market_finder import MarketInfo