"""Integration tests for gates.py pre-entry evaluation."""

import pytest
import time
from unittest.mock import Mock, patch, MagicMock
from dataclasses import dataclass
from datetime import datetime, timezone

import config
import gates
from market_finder import MarketInfo


@dataclass
class MockMarket:
    """Mock market for testing gates."""
    ticker: str = "KXBTC15M-26NOV090415-15"
    series: str = "KXBTC15M"
    close_time: datetime = None
    strike: float = 95000.0
    strike_type: str = "floor"
    yes_bid: int = 75
    yes_ask: int = 77
    no_bid: int = 23
    no_ask: int = 25
    yes_liquidity: int = 100
    no_liquidity: int = 100
    minutes_left: float = 8.0
    bracket_role: str = "MAIN"
    raw: dict = None

    def __post_init__(self):
        if self.close_time is None:
            self.close_time = datetime.now(timezone.utc)
        if self.raw is None:
            self.raw = {"ticker": self.ticker, "event_ticker": self.ticker.split("-")[0]}


class TestGatesEvaluateEntry:
    """Test gates.evaluate_entry with various market conditions."""

    def test_gates_disabled_allows_all(self):
        """When GATES_ENABLED=false, all entries should be allowed."""
        with patch.object(config, 'GATES_ENABLED', False):
            market = MockMarket()
            result = gates.evaluate_entry(market, side="yes", entry_price_cents=77)
            assert result.allowed is True
            assert result.reasons == ["gates disabled"]

    def test_gate1_atr_hurdle_blocks_when_ratio_exceeds(self):
        """ATR hurdle should block when required move exceeds hurdle * expected move."""
        with patch.object(config, 'GATES_ENABLED', True):
            with patch.object(config, 'GATES_ATR_ENABLED', True):
                with patch.object(config, 'GATES_ATR_SIDE_AWARE', True):
                    with patch.object(config, 'GATES_ATR_HURDLE_BTC', 1.0):
                        with patch.object(config, 'GATES_SHADOW_MODE', False):
                            with patch('gates.atr_1m', return_value=100.0):  # $100 ATR
                                with patch('gates.spot_for', return_value=94000.0):  # Spot below strike
                                    market = MockMarket(strike=95000.0, minutes_left=5.0)
                                    # Required move = 1000 (strike - spot), Expected = 100 * sqrt(5) ≈ 223
                                    # Ratio = 1000/223 ≈ 4.48 > 1.0 hurdle → should block
                                    result = gates.evaluate_entry(market, side="yes", entry_price_cents=77)
                                    assert result.allowed is False
                                    assert any("gate1" in r for r in result.reasons)

    def test_gate1_atr_hurdle_allows_when_ratio_within_hurdle(self):
        """ATR hurdle should allow when required move is within hurdle * expected move."""
        with patch.object(config, 'GATES_ENABLED', True):
            with patch.object(config, 'GATES_ATR_ENABLED', True):
                with patch.object(config, 'GATES_ATR_SIDE_AWARE', True):
                    with patch.object(config, 'GATES_ATR_HURDLE_BTC', 5.0):
                        with patch('gates.atr_1m', return_value=100.0):
                            with patch('gates.spot_for', return_value=94500.0):
                                market = MockMarket(strike=95000.0, minutes_left=5.0)
                                # Required move = 500, Expected = 223, Ratio = 2.24 < 5.0 → allow
                                result = gates.evaluate_entry(market, side="yes", entry_price_cents=77)
                                assert result.allowed is True

    def test_gate2_ev_blocks_when_net_ev_below_min(self):
        """Fee-adjusted EV gate should block when net EV < GATES_MIN_EDGE."""
        with patch.object(config, 'GATES_ENABLED', True):
            with patch.object(config, 'GATES_EV_ENABLED', True):
                with patch.object(config, 'GATES_MIN_EDGE', 0.005):  # $0.005 per contract
                    with patch.object(config, 'GATES_FEE_TAKER', True):
                        with patch.object(config, 'GATES_TIME_DECAY_ENABLED', True):
                            with patch.object(config, 'GATES_SHADOW_MODE', False):
                                with patch('gates.atr_1m', return_value=100.0):
                                    with patch('gates.spot_for', return_value=95000.0):
                                        market = MockMarket(strike=95000.0, minutes_left=5.0)
                                        # Entry at 77c, model q ≈ 0.5 (at-the-money), fee ≈ 0.013
                                        # net_ev = 0.5*(1-0.77) - 0.5*0.77 - 0.013 = 0.115 - 0.385 - 0.013 = -0.283 < 0.005
                                        result = gates.evaluate_entry(market, side="yes", entry_price_cents=77)
                                        assert result.allowed is False
                                        assert any("gate2" in r for r in result.reasons)

    def test_gate3_high_gamma_zone_blocks_when_enabled(self):
        """High gamma zone should block when GATES_DENY_HIGH_GAMMA=true."""
        with patch.object(config, 'GATES_ENABLED', True):
            with patch.object(config, 'GATES_TIME_DECAY_ENABLED', True):
                with patch.object(config, 'GATES_HIGH_GAMMA_MINUTES', 2.0):
                    with patch.object(config, 'GATES_DENY_HIGH_GAMMA', True):
                        with patch.object(config, 'GATES_SHADOW_MODE', False):
                            with patch('gates.atr_1m', return_value=100.0):
                                with patch('gates.spot_for', return_value=95000.0):
                                    market = MockMarket(strike=95000.0, minutes_left=1.5)  # < 2.0 min
                                    result = gates.evaluate_entry(market, side="yes", entry_price_cents=77)
                                    assert result.allowed is False
                                    assert any("gate3" in r and "high-gamma" in r for r in result.reasons)

    def test_gate3_high_gamma_allows_when_deny_false(self):
        """High gamma zone should allow when GATES_DENY_HIGH_GAMMA=false (shadow mode)."""
        with patch.object(config, 'GATES_ENABLED', True):
            with patch.object(config, 'GATES_TIME_DECAY_ENABLED', True):
                with patch.object(config, 'GATES_HIGH_GAMMA_MINUTES', 2.0):
                    with patch.object(config, 'GATES_DENY_HIGH_GAMMA', False):
                        with patch('gates.atr_1m', return_value=100.0):
                            with patch('gates.spot_for', return_value=95000.0):
                                market = MockMarket(strike=95000.0, minutes_left=1.5)
                                result = gates.evaluate_entry(market, side="yes", entry_price_cents=77)
                                assert result.allowed is True
                                assert result.metrics.get("high_gamma_zone") is True
                                assert result.metrics.get("shadow_mode") is True

    def test_unsupported_asset_blocks(self):
        """Unsupported underlying asset should fail closed."""
        with patch.object(config, 'GATES_ENABLED', True):
            with patch.object(config, 'GATES_SHADOW_MODE', False):
                market = MockMarket(series="KXUNKNOWN15M", ticker="KXUNKNOWN15M-26NOV090415-15")
                result = gates.evaluate_entry(market, side="yes", entry_price_cents=77)
                assert result.allowed is False
                assert any("gate0" in r for r in result.reasons)

    def test_shadow_mode_allows_but_logs_would_block(self):
        """In shadow mode, denies should return allowed=True with would_block metric."""
        with patch.object(config, 'GATES_ENABLED', True):
            with patch.object(config, 'GATES_SHADOW_MODE', True):
                with patch.object(config, 'GATES_ATR_ENABLED', True):
                    with patch.object(config, 'GATES_ATR_SIDE_AWARE', True):
                        with patch.object(config, 'GATES_ATR_HURDLE_BTC', 1.0):
                            with patch('gates.atr_1m', return_value=100.0):
                                with patch('gates.spot_for', return_value=94000.0):
                                    market = MockMarket(strike=95000.0, minutes_left=5.0)
                                    result = gates.evaluate_entry(market, side="yes", entry_price_cents=77)
                                    assert result.allowed is True  # Shadow mode allows
                                    assert result.metrics.get("shadow_mode") is True
                                    assert result.metrics.get("decision") == "would_block"

    def test_side_aware_atr_uses_distance_against_position(self):
        """SIDE_AWARE should measure distance against the position, not absolute."""
        with patch.object(config, 'GATES_ENABLED', True):
            with patch.object(config, 'GATES_ATR_ENABLED', True):
                with patch.object(config, 'GATES_ATR_SIDE_AWARE', True):
                    with patch.object(config, 'GATES_ATR_HURDLE_BTC', 1.0):
                        with patch('gates.atr_1m', return_value=100.0):
                            with patch('gates.spot_for', return_value=96000.0):  # Spot ABOVE strike
                                market = MockMarket(strike=95000.0, minutes_left=5.0)
                                # For YES side: required_move = max(0, strike - spot) = 0 (already past strike)
                                # Ratio = 0/223 = 0 < 1.0 → should allow
                                result = gates.evaluate_entry(market, side="yes", entry_price_cents=77)
                                assert result.allowed is True

    def test_no_side_uses_spot_above_strike(self):
        """NO side should measure distance when spot is above strike."""
        with patch.object(config, 'GATES_ENABLED', True):
            with patch.object(config, 'GATES_ATR_ENABLED', True):
                with patch.object(config, 'GATES_ATR_SIDE_AWARE', True):
                    with patch.object(config, 'GATES_ATR_HURDLE_BTC', 1.0):
                        with patch.object(config, 'GATES_SHADOW_MODE', False):
                            with patch('gates.atr_1m', return_value=100.0):
                                with patch('gates.spot_for', return_value=96000.0):  # Spot above strike
                                    market = MockMarket(strike=95000.0, minutes_left=5.0)
                                    # For NO side: required_move = max(0, spot - strike) = 1000
                                    # Ratio = 1000/223 ≈ 4.48 > 1.0 → should block
                                    result = gates.evaluate_entry(market, side="no", entry_price_cents=77)
                                    assert result.allowed is False
                                    assert any("gate1" in r for r in result.reasons)


class TestGatesPrefetch:
    """Test gates.prefetch warms caches correctly."""

    def test_prefetch_warms_spot_and_atr(self):
        """prefetch should call spot_for and atr_1m for each asset."""
        with patch('gates.spot_for') as mock_spot:
            with patch('gates.atr_1m') as mock_atr:
                gates.prefetch(["BTC", "ETH", "BTC"])  # Duplicate should be deduped
                assert mock_spot.call_count == 2
                assert mock_atr.call_count == 2
                mock_spot.assert_any_call("BTC")
                mock_spot.assert_any_call("ETH")
                mock_atr.assert_any_call("BTC")
                mock_atr.assert_any_call("ETH")


class TestGatesMath:
    """Test pure math functions in gates.py."""

    def test_net_ev_formula(self):
        """net_ev should compute q*(1-p) - (1-q)*p - fee."""
        # At p=0.77, q=0.80, fee=0.013
        # net_ev = 0.80*0.23 - 0.20*0.77 - 0.013 = 0.184 - 0.154 - 0.013 = 0.017
        ev = gates.net_ev(0.80, 0.77, 0.013)
        assert abs(ev - 0.017) < 0.001

    def test_breakeven_q_formula(self):
        """breakeven_q should return p + fee."""
        # At p=0.77, fee=0.013, breakeven = 0.783
        be = gates.breakeven_q(0.77, 0.013)
        assert abs(be - 0.783) < 0.001

    def test_cushion_z_formula(self):
        """cushion_z should compute (spot-strike)/(sigma*sqrt(t)) for YES."""
        # spot=96000, strike=95000, sigma=100, t=4 min
        # z = 1000/(100*2) = 5.0
        z = gates.cushion_z(96000, 95000, 100, 4, "yes")
        assert abs(z - 5.0) < 0.01

    def test_cushion_z_no_side(self):
        """cushion_z should negate for NO side."""
        z_yes = gates.cushion_z(96000, 95000, 100, 4, "yes")
        z_no = gates.cushion_z(96000, 95000, 100, 4, "no")
        assert z_no == -z_yes

    def test_normal_cdf(self):
        """normal_cdf should match standard normal CDF."""
        # CDF(0) = 0.5
        assert abs(gates.normal_cdf(0) - 0.5) < 0.001
        # CDF(1.96) ≈ 0.975
        assert abs(gates.normal_cdf(1.96) - 0.975) < 0.01


if __name__ == "__main__":
    pytest.main([__file__, "-v"])