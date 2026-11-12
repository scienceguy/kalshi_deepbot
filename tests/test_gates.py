"""Integration tests for gates.py pre-entry evaluation."""

import pytest
import math
import time
from contextlib import contextmanager
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
                            with patch('gates.spot_for', return_value=96000.0):  # Spot well above strike -> high prob
                                market = MockMarket(strike=95000.0, minutes_left=5.0)
                                # Required move = 0 (spot already past strike for YES), Expected = 223, Ratio = 0 < 5.0 → allow
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
        """High gamma zone should allow when GATES_DENY_HIGH_GAMMA=false."""
        with patch.object(config, 'GATES_ENABLED', True):
            with patch.object(config, 'GATES_TIME_DECAY_ENABLED', True):
                with patch.object(config, 'GATES_HIGH_GAMMA_MINUTES', 2.0):
                    with patch.object(config, 'GATES_DENY_HIGH_GAMMA', False):
                        with patch('gates.atr_1m', return_value=100.0):
                            with patch('gates.spot_for', return_value=96000.0):  # Spot above strike -> high prob
                                market = MockMarket(strike=95000.0, minutes_left=1.5)
                                result = gates.evaluate_entry(market, side="yes", entry_price_cents=77)
                                assert result.allowed is True
                                assert result.metrics.get("high_gamma_zone") is True
                                # Not blocked by high gamma because DENY_HIGH_GAMMA=False
                                assert "gate3" not in str(result.reasons)

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


class TestGatesStaleOk:
    """A feed outage must reuse the last good spot/ATR (stale-ok) rather than
    knock the bot out of the market; only an over-stale value fails closed."""

    @contextmanager
    def _clock(self, ttl, stale_ok):
        """Patch gates.time.time with a controllable clock; yield a mutable
        {"now": float} dict so the test can advance time."""
        import gates as _g
        holder = {"now": 0.0}

        class _Clock:
            @staticmethod
            def time():
                return holder["now"]

        patcher = patch.object(_g.time, "time", _Clock.time)
        patcher.start()
        try:
            with patch.object(config, 'GATES_STALE_OK_SECONDS', stale_ok):
                with patch.object(config, 'GATES_ATR_CACHE_SECONDS', ttl):
                    with patch.object(config, 'GATES_SPOT_CACHE_SECONDS', ttl):
                        yield holder
        finally:
            patcher.stop()

    def test_feed_failure_returns_last_good_spot(self):
        """After a good fetch, a TTL-expired refetch that fails returns the
        last good value while it is still within the stale-ok window."""
        gates._spot_cache.clear()
        with self._clock(ttl=30, stale_ok=300) as holder:
            with patch('binance_feed.composite_price', return_value=94000.0):
                assert gates.spot_for("BTC") == 94000.0
            # Advance past the spot TTL (30s) so the next call refetches.
            holder["now"] += 31.0
            with patch('binance_feed.composite_price',
                       side_effect=RuntimeError("binance down")):
                # 31s of age is < stale_ok (300) -> the gate reuses the good value.
                assert gates.spot_for("BTC") == 94000.0

    def test_over_stale_feed_fails_closed(self):
        """Once the last good value is older than GATES_STALE_OK_SECONDS,
        a failing feed returns None (fail closed)."""
        gates._spot_cache.clear()
        with self._clock(ttl=30, stale_ok=300) as holder:
            with patch('binance_feed.composite_price', return_value=94000.0):
                assert gates.spot_for("BTC") == 94000.0
            # Advance past the stale-ok window (300s) and past the TTL.
            holder["now"] += 301.0
            with patch('binance_feed.composite_price',
                       side_effect=RuntimeError("binance down")):
                # 301s of age is >= stale_ok (300) -> fail closed with None.
                assert gates.spot_for("BTC") is None


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

    def test_cushion_z_scales_with_sqrt_minutes(self):
        """Quadrupling the time left halves z: denom is sigma*sqrt(t)."""
        z1 = gates.cushion_z(96000, 95000, 100, 1, "yes")
        z4 = gates.cushion_z(96000, 95000, 100, 4, "yes")
        assert abs(z4 - z1 / 2.0) < 1e-9

    def test_cushion_z_undefined_inputs(self):
        """Fail closed on zero/negative inputs instead of dividing by zero."""
        assert gates.cushion_z(0, 95000, 100, 4, "yes") is None
        assert gates.cushion_z(96000, 0, 100, 4, "yes") is None
        assert gates.cushion_z(96000, 95000, 0, 4, "yes") is None
        assert gates.cushion_z(96000, 95000, -1, 4, "yes") is None
        assert gates.cushion_z(96000, 95000, 100, 0, "yes") is None

    def test_atr_to_sigma_conversion(self):
        """atr_1m is E|X|, not sigma: sigma = atr * sqrt(pi/2) = 1.2533*atr.

        A 1-dollar average true range is ~40% wider in sigma terms, since
        sqrt(pi/2) = 1.2533 and (1.2533)^2 ~= 1.57.
        """
        atr = 100.0
        sigma = atr * math.sqrt(math.pi / 2)
        assert abs(sigma - 125.33) < 0.01

    def test_normal_cdf(self):
        """normal_cdf should match standard normal CDF."""
        # CDF(0) = 0.5
        assert abs(gates.normal_cdf(0) - 0.5) < 0.001
        # CDF(1.96) ≈ 0.975
        assert abs(gates.normal_cdf(1.96) - 0.975) < 0.01


class TestMeasuredFloorSide:
    """GATES_MEASURED_Q is a YES-side win rate; it must never be read as NO's.

    A NO contract priced at 97c is the complement of a YES contract at 3c,
    and settles NO only a few percent of the time. Flooring its model
    probability at P(YES wins)=0.9928 would tell the bot a near-certain
    loser is a near-certain winner, so the floor is keyed on the
    YES-equivalent price rather than the raw side price.
    """

    def _run(self, side, cents, q_model):
        market = MockMarket()
        book = Mock()
        with patch.object(gates, "normal_cdf", return_value=q_model), \
             patch.object(gates, "spot_for", return_value=market.strike + 50.0), \
             patch.object(gates, "atr_1m", return_value=10.0), \
             patch.object(config, "GATES_ATR_ENABLED", False), \
             patch.object(config, "GATES_EV_ENABLED", False):
            return gates.evaluate_entry(
                market, spot_state={}, orderbook=book,
                side=side, entry_price_cents=cents)

    def test_yes_in_band_not_floored_by_default(self):
        """97c in the band must NOT be raised to 0.9928 unless opted in.

        Raising q to the measured win rate is what made gate 2 pass every
        in-band candidate regardless of the model, so the default must be
        the model's own value.
        """
        config.GATES_MEASURED_Q = 0.9928
        res = self._run("yes", 97, 0.50)
        assert res.metrics.get("yes_equiv_price_cents") == 97
        assert not res.metrics.get("q_measured_floor")
        assert res.metrics.get("q") < config.GATES_MEASURED_Q

    def test_yes_in_band_floor_is_opt_in(self):
        """GATES_MEASURED_FLOOR_ENABLED=True restores the old floor behaviour."""
        config.GATES_MEASURED_Q = 0.9928
        market = MockMarket()
        book = Mock()
        with patch.object(gates, "normal_cdf", return_value=0.50), \
             patch.object(gates, "spot_for", return_value=market.strike + 50.0), \
             patch.object(gates, "atr_1m", return_value=10.0), \
             patch.object(config, "GATES_ATR_ENABLED", False), \
             patch.object(config, "GATES_EV_ENABLED", False), \
             patch.object(config, "GATES_MEASURED_FLOOR_ENABLED", True):
            res = gates.evaluate_entry(
                market, spot_state={}, orderbook=book,
                side="yes", entry_price_cents=97)
        assert res.metrics.get("q_measured_floor") is True
        assert res.metrics.get("q") >= 0.9928

    def test_no_at_97c_is_not_floored(self):
        """NO @ 97c == YES @ 3c: outside the band, must not get 99.28%."""
        res = self._run("no", 97, 0.50)
        assert res.metrics.get("yes_equiv_price_cents") == 3

    def test_no_at_3c_maps_to_yes_97(self):
        """The YES-equivalent of a NO@3c fill is 97c: inside the band."""
        res = self._run("no", 3, 0.50)
        assert res.metrics.get("yes_equiv_price_cents") == 97


if __name__ == "__main__":
    pytest.main([__file__, "-v"])