"""Integration tests for market_finder.py market discovery."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone, timedelta

import config
import market_finder
from market_finder import MarketInfo
from kalshi_client import KalshiClient


class MockKalshiClient:
    """Mock Kalshi client for testing market_finder."""
    
    def __init__(self):
        self.markets_cache = {}
        self.orderbooks_cache = {}
    
    def get_markets(self, series_ticker=None, status="open", limit=50):
        """Return mock markets for a series."""
        now = datetime.now(timezone.utc)
        close_time = now + timedelta(minutes=10)
        
        markets = []
        for i, ticker in enumerate([f"{series_ticker}-26NOV090415-15", f"{series_ticker}-26NOV090430-30"]):
            markets.append({
                "ticker": ticker,
                "series_ticker": series_ticker,
                "status": "active",
                "close_time": close_time.isoformat(),
                "floor_strike": 95000.0 if "BTC" in series_ticker else 1.0,
                "yes_bid_dollars": "0.75",
                "yes_ask_dollars": "0.77",
                "no_bid_dollars": "0.23",
                "no_ask_dollars": "0.25",
                "yes_bid_size_fp": "100",
                "no_bid_size_fp": "100",
            })
        return markets
    
    def get_orderbook(self, ticker, depth=5):
        """Return mock orderbook for a ticker."""
        return {
            "yes": [[77, 50], [78, 30]],  # [price, size]
            "no": [[23, 50], [22, 30]],
        }
    
    def invalidate_markets(self, series_ticker):
        """Mock invalidate."""
        pass


class TestMarketFinder:
    """Test market_finder.find_active_markets."""

    def test_find_active_markets_returns_tradeable_brackets(self):
        """Should return markets with ask in entry band."""
        client = MockKalshiClient()
        
        with patch.object(config, 'TARGET_SERIES', ['KXBTC15M']):
            with patch.object(config, 'MIN_YES_PRICE_CENTS', 75):
                with patch.object(config, 'MAX_YES_PRICE_CENTS', 99):
                    with patch.object(config, 'HEDGE_YES_ASK_MAX', 12):
                        with patch.object(config, 'MAX_HEDGE_PER_SERIES', 0):
                            markets = market_finder.find_active_markets(client)
                            
                            assert markets.tradable_count > 0
                            assert len(markets) > 0
                            
                            for m in markets:
                                # Should have ask in band
                                assert m.yes_ask is not None
                                assert 75 <= m.yes_ask <= 99 or (m.no_ask and 75 <= m.no_ask <= 99)

    def test_find_active_markets_filters_by_minutes_left(self):
        """Should filter markets with 0 < minutes_left <= 15.5."""
        client = MockKalshiClient()
        
        # Override to return a market that's too old
        old_close = datetime.now(timezone.utc) - timedelta(minutes=5)
        client.markets_cache = {}
        def get_markets_old(*args, **kwargs):
            return [{
                "ticker": "KXBTC15M-26NOV090415-15",
                "series_ticker": "KXBTC15M",
                "status": "active",
                "close_time": old_close.isoformat(),
                "floor_strike": 95000.0,
                "yes_bid_dollars": "0.75",
                "yes_ask_dollars": "0.77",
                "no_bid_dollars": "0.23",
                "no_ask_dollars": "0.25",
            }]
        client.get_markets = get_markets_old
        
        with patch.object(config, 'TARGET_SERIES', ['KXBTC15M']):
            with patch.object(config, 'MIN_YES_PRICE_CENTS', 75):
                with patch.object(config, 'MAX_YES_PRICE_CENTS', 99):
                    markets = market_finder.find_active_markets(client)
                    # Old market should be filtered out
                    assert markets.tradable_count == 0

    def test_find_active_markets_parallel_fetch(self):
        """Should fetch all series concurrently."""
        client = MockKalshiClient()
        call_count = [0]
        
        def counting_get_markets(*args, **kwargs):
            call_count[0] += 1
            return client.get_markets(*args, **kwargs)
        
        client.get_markets = counting_get_markets
        
        with patch.object(config, 'TARGET_SERIES', ['KXBTC15M', 'KXETH15M', 'KXSOL15M']):
            with patch.object(config, 'MIN_YES_PRICE_CENTS', 75):
                with patch.object(config, 'MAX_YES_PRICE_CENTS', 99):
                    markets = market_finder.find_active_markets(client)
                    # Should have made 3 calls (one per series)
                    assert call_count[0] >= 3

    def test_find_active_markets_deduplicates_tickers(self):
        """Should not return duplicate tickers."""
        client = MockKalshiClient()
        
        with patch.object(config, 'TARGET_SERIES', ['KXBTC15M']):
            with patch.object(config, 'MIN_YES_PRICE_CENTS', 75):
                with patch.object(config, 'MAX_YES_PRICE_CENTS', 99):
                    markets = market_finder.find_active_markets(client)
                    
                    tickers = [m.ticker for m in markets]
                    assert len(tickers) == len(set(tickers)), "Duplicate tickers found"

    def test_find_active_markets_includes_bracket_role(self):
        """Each market should have bracket_role set."""
        client = MockKalshiClient()
        
        with patch.object(config, 'TARGET_SERIES', ['KXBTC15M']):
            with patch.object(config, 'MIN_YES_PRICE_CENTS', 75):
                with patch.object(config, 'MAX_YES_PRICE_CENTS', 99):
                    markets = market_finder.find_active_markets(client)
                    
                    for m in markets:
                        assert hasattr(m, 'bracket_role')
                        assert m.bracket_role in ('MAIN', 'HEDGE_NO')


class TestMarketInfo:
    """Test MarketInfo dataclass."""

    def test_mid_yes_calculation(self):
        """mid_yes should calculate (bid + ask) / 2."""
        m = MarketInfo(
            ticker="TEST", series="TEST", close_time=datetime.now(timezone.utc),
            strike=100.0, strike_type="floor",
            yes_bid=75, yes_ask=77, no_bid=23, no_ask=25,
            yes_liquidity=100, no_liquidity=100,
            minutes_left=10.0
        )
        assert m.mid_yes == 76.0

    def test_mid_yes_with_missing_bid(self):
        """mid_yes should use ask when bid is missing."""
        m = MarketInfo(
            ticker="TEST", series="TEST", close_time=datetime.now(timezone.utc),
            strike=100.0, strike_type="floor",
            yes_bid=0, yes_ask=77, no_bid=23, no_ask=25,
            yes_liquidity=100, no_liquidity=100,
            minutes_left=10.0
        )
        assert m.mid_yes == 77.0

    def test_mid_yes_with_missing_ask(self):
        """mid_yes should use bid when ask is missing."""
        m = MarketInfo(
            ticker="TEST", series="TEST", close_time=datetime.now(timezone.utc),
            strike=100.0, strike_type="floor",
            yes_bid=75, yes_ask=0, no_bid=23, no_ask=25,
            yes_liquidity=100, no_liquidity=100,
            minutes_left=10.0
        )
        assert m.mid_yes == 75.0

    def test_mid_yes_with_no_quotes(self):
        """mid_yes should return None when no quotes."""
        m = MarketInfo(
            ticker="TEST", series="TEST", close_time=datetime.now(timezone.utc),
            strike=100.0, strike_type="floor",
            yes_bid=0, yes_ask=0, no_bid=0, no_ask=0,
            yes_liquidity=0, no_liquidity=0,
            minutes_left=10.0
        )
        assert m.mid_yes is None

    def test_skew_calculation(self):
        """skew should be yes_bid - no_bid."""
        m = MarketInfo(
            ticker="TEST", series="TEST", close_time=datetime.now(timezone.utc),
            strike=100.0, strike_type="floor",
            yes_bid=75, yes_ask=77, no_bid=23, no_ask=25,
            yes_liquidity=100, no_liquidity=100,
            minutes_left=10.0
        )
        assert m.skew == 52  # 75 - 23

    def test_spread_calculation(self):
        """spread should be ask - bid."""
        m = MarketInfo(
            ticker="TEST", series="TEST", close_time=datetime.now(timezone.utc),
            strike=100.0, strike_type="floor",
            yes_bid=75, yes_ask=77, no_bid=23, no_ask=25,
            yes_liquidity=100, no_liquidity=100,
            minutes_left=10.0
        )
        assert m.spread == 2


class TestMarketFinderConfig:
    """Test market_finder configuration."""

    def test_entry_band_filters_correctly(self):
        """Markets outside entry band should be filtered."""
        client = MockKalshiClient()
        
        # Override to return market with BOTH sides outside band
        def get_markets_outside_band(*args, **kwargs):
            return [{
                "ticker": "KXBTC15M-26NOV090415-15",
                "series_ticker": "KXBTC15M",
                "status": "active",
                "close_time": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "floor_strike": 95000.0,
                "yes_bid_dollars": "0.10",  # 10c - outside band
                "yes_ask_dollars": "0.12",
                "no_bid_dollars": "0.40",   # 40c - outside band (60c NO ask)
                "no_ask_dollars": "0.60",
                "yes_bid_size_fp": "100",
                "no_bid_size_fp": "100",
            }]
        client.get_markets = get_markets_outside_band
        
        with patch.object(config, 'TARGET_SERIES', ['KXBTC15M']):
            with patch.object(config, 'MIN_YES_PRICE_CENTS', 75):
                with patch.object(config, 'MAX_YES_PRICE_CENTS', 99):
                    markets = market_finder.find_active_markets(client)
                    assert markets.tradable_count == 0


class TestMarketFinderOrderbook:
    """Test orderbook integration."""

    def test_orderbook_overrides_market_bids(self):
        """Orderbook should override market bid prices."""
        client = MockKalshiClient()
        
        # Market has 75c bid, but orderbook has 76c bid (better)
        def get_orderbook_custom(ticker, depth=5):
            return {
                "yes": [[76, 100]],  # Better bid in orderbook
                "no": [[24, 100]],
            }
        client.get_orderbook = get_orderbook_custom
        
        with patch.object(config, 'TARGET_SERIES', ['KXBTC15M']):
            with patch.object(config, 'MIN_YES_PRICE_CENTS', 75):
                with patch.object(config, 'MAX_YES_PRICE_CENTS', 99):
                    markets = market_finder.find_active_markets(client)
                    
                    for m in markets:
                        # Orderbook bid should override market bid
                        assert m.yes_bid == 76


class TestMarketFinderSignalQuality:
    """Test signal quality improvements in market_finder."""
    
    def test_no_ask_false_positive_removed(self):
        """Markets where only NO ask is in band (not YES ask) are not tradeable.
        
        For a favourite-YES strategy, a NO ask of 85 (YES bid 15) should NOT
        flag the market as tradeable - this was a false positive.
        """
        client = MockKalshiClient()
        
        # Market with NO ask in band but YES ask OUTSIDE band
        # YES ask = 60c (outside [75,99]), NO ask = 85c (inside [75,99])
        def get_markets_no_ask_only(*args, **kwargs):
            return [{
                "ticker": "KXBTC15M-26NOV090415-15",
                "series_ticker": "KXBTC15M",
                "status": "active",
                "close_time": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "floor_strike": 95000.0,
                "yes_bid_dollars": "0.60",   # YES bid 60c
                "yes_ask_dollars": "0.62",   # YES ask 62c - OUTSIDE [75,99]
                "no_bid_dollars": "0.15",    # NO bid 15c
                "no_ask_dollars": "0.85",    # NO ask 85c - inside [75,99]
                "yes_bid_size_fp": "100",
                "no_bid_size_fp": "100",
            }]
        client.get_markets = get_markets_no_ask_only
        
        # Mock orderbook with low YES liquidity to trigger the liquidity guard
        # and ensure the market is filtered for the right reason
        client.get_orderbook = lambda ticker, depth=5: {
            "yes": [[77, 1]],  # 1 contract only - low liquidity
            "no": [[23, 100]],
        }
        
        with patch.object(config, 'TARGET_SERIES', ['KXBTC15M']):
            with patch.object(config, 'MIN_YES_PRICE_CENTS', 75):
                with patch.object(config, 'MAX_YES_PRICE_CENTS', 99):
                    with patch.object(config, 'HEDGE_YES_ASK_MAX', 12):
                        with patch.object(config, 'MAX_HEDGE_PER_SERIES', 0):
                            markets = market_finder.find_active_markets(client)
                            # Market should NOT be tradeable because only NO ask is in band,
                            # not YES ask. This was a false positive before the fix.
                            assert markets.tradable_count == 0, \
                                f"Expected 0 tradeable markets (NO-ask only false positive), got {markets.tradable_count}"
                            assert len(markets) == 0, \
                                f"Expected 0 markets, got {len(markets)}"
    
    def test_minimum_liquidity_guard(self):
        """Markets with YES liquidity < 5 are skipped by the size guard."""
        client = MockKalshiClient()
        
        # Market with YES liquidity = 2 (too low, should be skipped)
        # and NO liquidity = 100
        def get_markets_low_yes_liq(*args, **kwargs):
            return [{
                "ticker": "KXBTC15M-26NOV090415-15",
                "series_ticker": "KXBTC15M",
                "status": "active",
                "close_time": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "floor_strike": 95000.0,
                "yes_bid_dollars": "0.75",
                "yes_ask_dollars": "0.77",
                "no_bid_dollars": "0.23",
                "no_ask_dollars": "0.25",
                "yes_bid_size_fp": "2",   # YES liquidity = 2 contracts (too low!)
                "no_bid_size_fp": "100",
            }]
        client.get_markets = get_markets_low_yes_liq
        
        # Mock orderbook that preserves the low YES liquidity
        client.get_orderbook = lambda ticker, depth=5: {
            "yes": [[77, 2]],  # 2 contracts in orderbook
            "no": [[23, 100]],
        }
        
        with patch.object(config, 'TARGET_SERIES', ['KXBTC15M']):
            with patch.object(config, 'MIN_YES_PRICE_CENTS', 75):
                with patch.object(config, 'MAX_YES_PRICE_CENTS', 99):
                    with patch.object(config, 'HEDGE_YES_ASK_MAX', 12):
                        with patch.object(config, 'MAX_HEDGE_PER_SERIES', 0):
                            markets = market_finder.find_active_markets(client)
                            # Market should be skipped because yes_liquidity < 5
                            assert markets.tradable_count == 0, \
                                f"Expected 0 tradeable markets (low liquidity guard), got {markets.tradable_count}"
                            assert len(markets) == 0, \
                                f"Expected 0 markets, got {len(markets)}"
    
    def test_minimum_liquidity_passes_when_high(self):
        """Markets with YES liquidity >= 5 pass the size guard."""
        client = MockKalshiClient()
        
        # Market with YES liquidity = 10 (sufficient) and NO liquidity = 50
        def get_markets_high_yes_liq(*args, **kwargs):
            return [{
                "ticker": "KXBTC15M-26NOV090415-15",
                "series_ticker": "KXBTC15M",
                "status": "active",
                "close_time": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "floor_strike": 95000.0,
                "yes_bid_dollars": "0.75",
                "yes_ask_dollars": "0.77",
                "no_bid_dollars": "0.23",
                "no_ask_dollars": "0.25",
                "yes_bid_size_fp": "10",  # YES liquidity = 10 contracts (ok)
                "no_bid_size_fp": "50",
            }]
        client.get_markets = get_markets_high_yes_liq
        
        # Mock orderbook with sufficient YES liquidity
        client.get_orderbook = lambda ticker, depth=5: {
            "yes": [[77, 10]],  # 10 contracts in orderbook
            "no": [[23, 50]],
        }
        
        with patch.object(config, 'TARGET_SERIES', ['KXBTC15M']):
            with patch.object(config, 'MIN_YES_PRICE_CENTS', 75):
                with patch.object(config, 'MAX_YES_PRICE_CENTS', 99):
                    with patch.object(config, 'HEDGE_YES_ASK_MAX', 12):
                        with patch.object(config, 'MAX_HEDGE_PER_SERIES', 0):
                            markets = market_finder.find_active_markets(client)
                            # Market should be tradeable because YES ask is in band
                            # AND YES liquidity >= 5
                            assert markets.tradable_count > 0, \
                                f"Expected >0 tradeable markets (sufficient liquidity), got {markets.tradable_count}"
                            assert len(markets) > 0, \
                                f"Expected >0 markets, got {len(markets)}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])