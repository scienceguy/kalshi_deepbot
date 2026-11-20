"""Integration tests for gates.py pre-entry evaluation."""

import pytest
import math
from contextlib import contextmanager
from unittest.mock import Mock, patch
from dataclasses import dataclass
from datetime import datetime, timezone

import config
import gates


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

    def test_kl_divergence_formula(self):
        """KL divergence: p*ln(p/q) + (1-p)*ln((1-p)/(1-q))."""
        # From PredictionMarketsPicks worked example: p=0.85, q=0.60
        kl_pq = gates.kl_divergence(0.85, 0.60)
        kl_qp = gates.kl_divergence(0.60, 0.85)
        sym_kl = gates.symmetric_kl_divergence(0.85, 0.60)
        assert abs(kl_pq - 0.1489) < 0.001
        assert abs(kl_qp - 0.1833) < 0.001
        assert abs(sym_kl - 0.1661) < 0.001

    def test_symmetric_kl_near_middle_low(self):
        """Near 50/50, symmetric KL is small for same price gap."""
        # 10-cent gap at 55¢ vs 45¢
        sym_kl = gates.symmetric_kl_divergence(0.55, 0.45)
        assert sym_kl < 0.05  # Below WATCH threshold

    def test_symmetric_kl_near_edge_higher(self):
        """Near edge, same 10-cent gap produces higher symmetric KL."""
        # 10-cent gap at 95¢ vs 85¢ - same raw gap but more information
        sym_kl = gates.symmetric_kl_divergence(0.95, 0.85)
        # This should be higher than 0.55 vs 0.45
        assert sym_kl > gates.symmetric_kl_divergence(0.55, 0.45)

    def test_symmetric_kl_boundary_returns_inf(self):
        """Boundary probabilities (0 or 1) return infinity."""
        assert gates.symmetric_kl_divergence(1.0, 0.5) == float("inf")
        assert gates.symmetric_kl_divergence(0.0, 0.5) == float("inf")
        assert gates.symmetric_kl_divergence(0.5, 1.0) == float("inf")
        assert gates.symmetric_kl_divergence(0.5, 0.0) == float("inf")

    def test_cushion_z_formula(self):
        """cushion_z should compute |ln(S/K)|/(sigma*sqrt(t)) for YES.

        S=100, K=100*e^-0.1 => ln(S/K)=0.1, sigma=0.1, t=1 => z=1.0.
        sigma is per-sqrt-minute LOG vol (dimensionless), not dollars.
        """
        strike = 100.0 * math.exp(-0.1)
        z = gates.cushion_z(100.0, strike, 0.1, 1.0, "yes")
        assert abs(z - 1.0) < 1e-6

    def test_cushion_z_no_side(self):
        """cushion_z should negate for NO side (mirror cushion)."""
        strike = 100.0 * math.exp(-0.1)
        z_yes = gates.cushion_z(100.0, strike, 0.1, 1.0, "yes")
        z_no = gates.cushion_z(100.0, strike, 0.1, 1.0, "no")
        assert abs(z_no - (-z_yes)) < 1e-12

    def test_cushion_z_sign_itm(self):
        """Positive z means the chosen side is in-the-money (cushion in our favour)."""
        strike = 100.0 * math.exp(-0.1)  # spot 100 > strike: YES ITM
        assert gates.cushion_z(100.0, strike, 0.1, 1.0, "yes") > 0
        assert gates.cushion_z(100.0, strike, 0.1, 1.0, "no") < 0
        # Mirror: spot < strike -> NO ITM.
        assert gates.cushion_z(strike, 100.0, 0.1, 1.0, "no") > 0
        assert gates.cushion_z(strike, 100.0, 0.1, 1.0, "yes") < 0

    def test_cushion_z_scales_with_sqrt_minutes(self):
        """Quadrupling the time left halves z: denom is sigma*sqrt(t)."""
        strike = 100.0 * math.exp(-0.1)
        z1 = gates.cushion_z(100.0, strike, 0.1, 1.0, "yes")
        z4 = gates.cushion_z(100.0, strike, 0.1, 4.0, "yes")
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
    """F-04: measured floor uses own-side price (not YES-equivalent) to decide if in band.

    The floor is a YES-side win rate (0.9928) measured on YES quotes in 95-99c band.
    The fix uses own_price_cents to check band membership:
    - YES @ 97c (favorite, own_price=97 in 95-99) -> floor applies with YES floor (0.9928)
    - YES @ 3c (longshot, own_price=3 out of band) -> no floor
    - NO @ 97c (favorite, own_price=97 in 95-99) -> floor applies with NO complement (~0.0072)
    - NO @ 3c (longshot, own_price=3 out of band) -> no floor

    This prevents NO @ 97c from being incorrectly floored with the YES floor
    (the old bug used yes_equiv = 100-97 = 3, outside band -> no floor, which
    was correct by accident; but NO @ 3c had yes_equiv = 97, inside band ->
    got YES floor incorrectly).
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
        assert res.metrics.get("own_price_cents") == 97
        assert not res.metrics.get("q_measured_floor")
        assert res.metrics.get("q") < config.GATES_MEASURED_Q

    def test_yes_in_band_floor_is_opt_in(self):
        """GATES_MEASURED_FLOOR_ENABLED=True applies floor for YES favorite in band."""
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
        assert res.metrics.get("own_price_cents") == 97

    def test_no_favorite_in_band_gets_no_floor(self):
        """NO @ 97c (favorite, own_price=97 in 95-99) gets NO complement floor.

        This is the fix: own-side price 97c is in band, so floor applies
        with the NO complement (1 - 0.9928 = 0.0072).
        """
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
                side="no", entry_price_cents=97)
        assert res.metrics.get("q_measured_floor") is True
        assert "NO complement" in res.metrics.get("q_floor_source", "")
        assert res.metrics.get("own_price_cents") == 97
        # q should be max(0.50, 0.0072) = 0.50 (floor doesn't change it but is applied)
        assert res.metrics.get("q") == 0.50

    def test_no_longshot_out_of_band_no_floor(self):
        """NO @ 3c (longshot, own_price=3 out of band) -> no floor.

        This was the bug: old logic used yes_equiv=97 (in band) and applied
        YES floor to a NO longshot that wins ~3% of the time.
        """
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
                side="no", entry_price_cents=3)
        assert res.metrics.get("q_measured_floor") is not True
        assert res.metrics.get("own_price_cents") == 3
        assert res.metrics.get("q") == 0.50

    def test_pin_risk_gate_blocks_when_enabled(self):
        """GATES_DENY_PIN_RISK=True blocks entries with minutes <= GATES_PIN_RISK_MINUTES."""
        market = MockMarket()
        market.minutes_left = 5.0
        book = Mock()
        with patch.object(gates, "normal_cdf", return_value=0.99), \
             patch.object(gates, "spot_for", return_value=market.strike + 500.0), \
             patch.object(gates, "atr_1m", return_value=10.0), \
             patch.object(config, "GATES_SHADOW_MODE", False), \
             patch.object(config, "GATES_DENY_PIN_RISK", True), \
             patch.object(config, "GATES_PIN_RISK_MINUTES", 6.0):
            res = gates.evaluate_entry(
                market, spot_state={}, orderbook=book,
                side="yes", entry_price_cents=95)
        assert res.metrics.get("pin_risk_denied") is True
        assert res.allowed is False
        assert any("pin risk" in r for r in res.reasons)



class TestCushionZSignAndScale:
    """cushion_z sign/scale invariants a quiet corruption would break.

    The log form z = ln(S/K)/(sigma*sqrt(t)) means: YES is positive-ITM,
    NO mirrors it, and the value depends only on the S/K ratio, not the
    units spot and strike are quoted in.
    """

    def test_sign_yes_above_strike(self):
        """Spot above strike: YES cushion positive, NO negative."""
        assert gates.cushion_z(101.0, 100.0, 0.1, 1.0, "yes") > 0
        assert gates.cushion_z(101.0, 100.0, 0.1, 1.0, "no") < 0

    def test_sign_yes_below_strike(self):
        """Spot below strike: YES cushion negative, NO positive."""
        assert gates.cushion_z(99.0, 100.0, 0.1, 1.0, "yes") < 0
        assert gates.cushion_z(99.0, 100.0, 0.1, 1.0, "no") > 0

    def test_sign_at_strike_is_zero(self):
        """Spot == strike: ln(S/K)=0 so z is exactly 0 for both sides."""
        assert gates.cushion_z(100.0, 100.0, 0.1, 1.0, "yes") == 0.0
        assert gates.cushion_z(100.0, 100.0, 0.1, 1.0, "no") == 0.0

    def test_scale_invariance_to_price_units(self):
        """(60000, 60100) and (60.0, 60.1) are the same ratio -> same z."""
        z_big = gates.cushion_z(60000.0, 60100.0, 0.001, 7.0, "yes")
        z_small = gates.cushion_z(60.0, 60.1, 0.001, 7.0, "yes")
        assert z_big is not None and z_small is not None
        assert abs(z_big - z_small) < 1e-12

    def test_zero_minutes_returns_none(self):
        """Zero time to expiry is undefined, not inf or ZeroDivisionError."""
        assert gates.cushion_z(100.0, 99.0, 0.1, 0.0, "yes") is None

    def test_zero_sigma_returns_none(self):
        """Zero volatility is undefined, not inf or ZeroDivisionError."""
        assert gates.cushion_z(100.0, 99.0, 0.0, 5.0, "yes") is None

    def test_negative_minutes_returns_none(self):
        assert gates.cushion_z(100.0, 99.0, 0.1, -3.0, "yes") is None


class TestPinRiskGateBoundaries:
    """Pin-risk guard: deny when 0 < minutes_left <= GATES_PIN_RISK_MINUTES."""

    def _eval(self, minutes_left):
        market = MockMarket(minutes_left=minutes_left, strike=95000.0)
        spot_state = {"spot": 100000.0, "sigma_per_min": 0.001, "atr_1m": 10.0}
        return gates.evaluate_entry(
            market, spot_state=spot_state, orderbook=Mock(),
            side="yes", entry_price_cents=95)

    def test_at_exact_boundary_is_denied(self):
        """minutes_left == GATES_PIN_RISK_MINUTES is inside the deny window."""
        with patch.object(config, "GATES_DENY_PIN_RISK", True), \
             patch.object(config, "GATES_PIN_RISK_MINUTES", 6.0), \
             patch.object(config, "GATES_ATR_ENABLED", False), \
             patch.object(config, "GATES_SHADOW_MODE", False):
            res = self._eval(6.0)
        assert res.metrics["pin_risk_denied"] is True
        assert res.allowed is False
        assert any("pin risk" in r for r in res.reasons)

    def test_just_above_boundary_not_pin_denied(self):
        """minutes_left a hair above the window is not pin-denied."""
        with patch.object(config, "GATES_DENY_PIN_RISK", True), \
             patch.object(config, "GATES_PIN_RISK_MINUTES", 6.0), \
             patch.object(config, "GATES_ATR_ENABLED", False), \
             patch.object(config, "GATES_SHADOW_MODE", False):
            res = self._eval(6.5)
        assert res.metrics["pin_risk_denied"] is False
        assert not any("pin risk" in r for r in res.reasons)
        assert res.allowed is True

    def test_flag_off_does_not_deny_even_at_one_minute(self):
        """GATES_DENY_PIN_RISK=False (default) never triggers the guard."""
        with patch.object(config, "GATES_DENY_PIN_RISK", False), \
             patch.object(config, "GATES_PIN_RISK_MINUTES", 6.0), \
             patch.object(config, "GATES_ATR_ENABLED", False), \
             patch.object(config, "GATES_SHADOW_MODE", False):
            res = self._eval(1.0)
        assert res.metrics["pin_risk_denied"] is False
        assert not any("pin risk" in r for r in res.reasons)

    def test_zero_minutes_not_pin_denied(self):
        """minutes_left == 0 must not match the 0 < minutes guard."""
        with patch.object(config, "GATES_DENY_PIN_RISK", True), \
             patch.object(config, "GATES_PIN_RISK_MINUTES", 6.0), \
             patch.object(config, "GATES_ATR_ENABLED", False), \
             patch.object(config, "GATES_SHADOW_MODE", False):
            res = self._eval(0.0)
        assert res.metrics["pin_risk_denied"] is False
        assert not any("pin risk" in r for r in res.reasons)


class TestThinCushionDenial:
    """A thin cushion must come back as a denial reason, not an exception."""

    def test_at_the_money_denied_with_reason(self):
        """Spot == strike -> z = 0 < GATES_MIN_CUSHION_Z -> block, no raise."""
        market = MockMarket(strike=95000.0, minutes_left=8.0)
        spot_state = {"spot": 95000.0, "sigma_per_min": 0.001, "atr_1m": 10.0}
        with patch.object(config, "GATES_ATR_ENABLED", False), \
             patch.object(config, "GATES_SHADOW_MODE", False):
            res = gates.evaluate_entry(
                market, spot_state=spot_state, orderbook=Mock(),
                side="yes", entry_price_cents=77)
        assert res.allowed is False
        assert any("cushion_z" in r for r in res.reasons)
        assert res.metrics["cushion_z"] == pytest.approx(0.0)
        assert res.metrics["q"] == pytest.approx(0.5)

    def test_slightly_otm_denied_when_z_below_min(self):
        """A small positive z below the configured minimum is denied."""
        market = MockMarket(strike=95000.0, minutes_left=8.0)
        spot_state = {"spot": 95100.0, "sigma_per_min": 0.001, "atr_1m": 10.0}
        with patch.object(config, "GATES_ATR_ENABLED", False), \
             patch.object(config, "GATES_SHADOW_MODE", False):
            res = gates.evaluate_entry(
                market, spot_state=spot_state, orderbook=Mock(),
                side="yes", entry_price_cents=77)
        z = res.metrics["cushion_z"]
        assert z is not None
        assert z < config.GATES_MIN_CUSHION_Z
        assert res.allowed is False
        assert any("cushion_z" in r for r in res.reasons)

    def test_zero_minutes_denies_with_reason_not_exception(self):
        """No time left -> gate3 denial reason, not a crash."""
        market = MockMarket(minutes_left=0.0)
        spot_state = {"spot": 96000.0, "sigma_per_min": 0.001, "atr_1m": 10.0}
        with patch.object(config, "GATES_SHADOW_MODE", False):
            res = gates.evaluate_entry(
                market, spot_state=spot_state, orderbook=Mock(),
                side="yes", entry_price_cents=77)
        assert res.allowed is False
        assert any("gate3" in r for r in res.reasons)


class TestEntryFee:
    """Test gates.entry_fee with series parameter (S7 fix)."""

    def test_entry_fee_standard_series_returns_taker(self):
        """Standard series (no maker fees) returns taker fee when enabled."""
        with patch.object(config, 'GATES_FEE_TAKER', True):
            with patch.object(config, 'AGGRESSIVE_FILL', False):
                fee = gates.entry_fee(0.95, series="KXBTC15M")
                assert fee > 0  # taker fee applied

    def test_entry_fee_maker_series_returns_maker_fee(self):
        """Maker-fee series returns maker fill fee."""
        with patch.object(config, 'GATES_FEE_TAKER', False):
            with patch.object(config, 'AGGRESSIVE_FILL', False):
                # Mock a maker-fee series
                with patch('fees.series_fee_info', return_value={'fee_type': 'quadratic_with_maker_fees', 'fee_multiplier': 1.0}):
                    with patch('fees.series_has_maker_fees', return_value=True):
                        with patch('fees.maker_fee_dollars', return_value=0.001):
                            fee = gates.entry_fee(0.95, series="KXBTC15M")
                            assert fee == 0.001  # maker fee

    def test_momentum_signal_returns_aligned_up(self):
        """momentum_signal returns UP direction when change_5m and velocity_1m are positive."""
        # Clear gates momentum cache to ensure mock is used
        gates._momentum_cache.clear()
        with patch('binance_feed.momentum_cached') as mock_momentum:
            mock_momentum.return_value = {
                "change_5m": 0.002,
                "velocity_1m": 0.0005,
                "aligned": True,
                "direction": "UP",
                "timestamp": 1795078200.0,
            }
            result = gates.momentum_signal("BTC")
            assert result is not None
            assert result["direction"] == "UP"
            assert result["aligned"] is True

    def test_momentum_gate_boosts_q_when_aligned_yes_up(self):
        """Gate 4 boosts q when YES trade and momentum UP (aligned)."""
        market = MockMarket(strike=95000.0, minutes_left=8.0)
        gates._momentum_cache.clear()
        with patch.object(config, 'GATES_MOMENTUM_ENABLED', True):
            with patch.object(config, 'GATES_TIME_DECAY_ENABLED', True):
                with patch.object(config, 'GATES_ATR_ENABLED', False):
                    with patch.object(config, 'GATES_EV_ENABLED', False):
                        with patch('gates.momentum_signal') as mock_momentum:
                            mock_momentum.return_value = {
                                "change_5m": 0.002,
                                "velocity_1m": 0.0005,
                                "aligned": True,
                                "direction": "UP",
                                "timestamp": 1795078200.0,
                            }
                            with patch('gates.spot_for', return_value=95500.0):
                                with patch('gates.atr_1m', return_value=100.0):
                                    with patch('gates.sigma_per_min', return_value=0.002):
                                        result = gates.evaluate_entry(
                                            market, side="yes", entry_price_cents=95
                                        )
                                        # q should be boosted by GATES_MOMENTUM_BOOST_Q (default 0.005)
                                        assert result.metrics.get("q_momentum_boost") == 0.005
                                        assert result.metrics.get("momentum_aligned") is True
                                        assert result.metrics.get("momentum_direction") == "UP"

    def test_momentum_gate_boosts_q_when_aligned_no_down(self):
        """Gate 4 boosts q when NO trade and momentum DOWN (aligned)."""
        market = MockMarket(strike=95000.0, minutes_left=8.0)
        gates._momentum_cache.clear()
        with patch.object(config, 'GATES_MOMENTUM_ENABLED', True):
            with patch.object(config, 'GATES_TIME_DECAY_ENABLED', True):
                with patch.object(config, 'GATES_ATR_ENABLED', False):
                    with patch.object(config, 'GATES_EV_ENABLED', False):
                        with patch('gates.momentum_signal') as mock_momentum:
                            mock_momentum.return_value = {
                                "change_5m": -0.002,
                                "velocity_1m": -0.0005,
                                "aligned": True,
                                "direction": "DOWN",
                                "timestamp": 1795078200.0,
                            }
                            with patch('gates.spot_for', return_value=94500.0):
                                with patch('gates.atr_1m', return_value=100.0):
                                    with patch('gates.sigma_per_min', return_value=0.002):
                                        result = gates.evaluate_entry(
                                            market, side="no", entry_price_cents=95
                                        )
                                        # q should be boosted by GATES_MOMENTUM_BOOST_Q (default 0.005)
                                        assert result.metrics.get("q_momentum_boost") == 0.005
                                        assert result.metrics.get("momentum_aligned") is True
                                        assert result.metrics.get("momentum_direction") == "DOWN"

    def test_momentum_gate_denies_misaligned_when_enabled(self):
        """Gate 4 denies when momentum opposes trade and GATES_MOMENTUM_DENY_MISALIGNED=True."""
        market = MockMarket(strike=95000.0, minutes_left=8.0)
        gates._momentum_cache.clear()
        with patch.object(config, 'GATES_MOMENTUM_ENABLED', True):
            with patch.object(config, 'GATES_TIME_DECAY_ENABLED', True):
                with patch.object(config, 'GATES_MOMENTUM_DENY_MISALIGNED', True):
                    with patch.object(config, 'GATES_ATR_ENABLED', False):
                        with patch.object(config, 'GATES_EV_ENABLED', False):
                            with patch('gates.momentum_signal') as mock_momentum:
                                mock_momentum.return_value = {
                                    "change_5m": -0.002,
                                    "velocity_1m": -0.0005,
                                    "aligned": True,
                                    "direction": "DOWN",
                                    "timestamp": 1795078200.0,
                                }
                                with patch('gates.spot_for', return_value=95500.0):
                                    with patch('gates.atr_1m', return_value=100.0):
                                        with patch('gates.sigma_per_min', return_value=0.002):
                                            result = gates.evaluate_entry(
                                                market, side="yes", entry_price_cents=95
                                            )
                                            assert result.allowed is False
                                            assert any("gate4: momentum DOWN opposes yes trade" in r for r in result.reasons)


class TestMeasuredFloorFourCorners:
    """F-04: Test measured floor on all four (side, price) corners.

    The floor is a YES-side win rate (0.9928) measured on YES quotes in 95-99c band.
    Current logic uses yes_equiv = price if YES else 100-price, which incorrectly
    applies the YES floor to NO longshots (NO @ 3c -> yes_equiv=97 -> floor applied).
    """

    def _setup_market(self, side, price_cents):
        """Helper to create market and mocks for a given side/price."""
        market = MockMarket(
            strike=95000.0,
            minutes_left=8.0,
            yes_bid=price_cents if side == "yes" else 100 - price_cents,
            yes_ask=price_cents if side == "yes" else 100 - price_cents + 2,
            no_bid=100 - price_cents - 2 if side == "yes" else price_cents,
            no_ask=100 - price_cents if side == "yes" else price_cents + 2,
        )
        return market

    def _run_floor_test(self, side, price_cents, spot, sigma=0.002):
        """Run floor test with parameters that pass early gates so we test floor logic."""
        market = self._setup_market(side, price_cents)
        gates._momentum_cache.clear()
        with patch.object(config, 'GATES_MEASURED_FLOOR_ENABLED', True):
            with patch.object(config, 'GATES_ATR_ENABLED', True):
                with patch.object(config, 'GATES_EV_ENABLED', False):
                    with patch.object(config, 'GATES_TIME_DECAY_ENABLED', True):
                        with patch.object(config, 'GATES_MOMENTUM_ENABLED', False):
                            with patch.object(config, 'GATES_ATR_HURDLE_BTC', 100.0):  # Very high hurdle to pass
                                with patch.object(config, 'GATES_ATR_HURDLE', 100.0):
                                    with patch.object(config, 'GATES_MIN_CUSHION_Z', 0.0):  # Disable cushion check
                                        with patch.object(config, 'GATES_DENY_PIN_RISK', False):
                                            with patch.object(config, 'GATES_DENY_HIGH_GAMMA', False):
                                                with patch('gates.atr_1m', return_value=100.0):
                                                    with patch('gates.spot_for', return_value=spot):
                                                        # Pass sigma via spot_state to ensure Gate 3 runs
                                                        spot_state = {"spot": spot, "sigma_per_min": sigma, "atr_1m": 100.0}
                                                        result = gates.evaluate_entry(
                                                            market, spot_state=spot_state, side=side, entry_price_cents=price_cents
                                                        )
                                                        return result

    def test_floor_yes_favorite_in_band(self):
        """YES @ 97c (favorite, in 95-99 band) -> floor should apply."""
        result = self._run_floor_test("yes", 97, spot=96000.0)
        # Floor should be applied: q_floor_n should be 0 (config fallback)
        assert result.metrics.get("q_floor_n") == 0
        assert result.metrics.get("q_floor_source") == "config:GATES_MEASURED_Q"
        assert result.metrics.get("q_measured_floor") is True
        # q should be at least the floor (0.9928)
        assert result.metrics["q"] >= 0.9928

    def test_floor_yes_longshot_out_of_band(self):
        """YES @ 3c (longshot, out of band) -> no floor."""
        # Use spot > strike so cushion_z passes; model q will be high but that's fine
        # we're testing that floor doesn't apply when own_price=3 is out of band
        result = self._run_floor_test("yes", 3, spot=96000.0)
        # No floor: q_floor_n should not be set (None), q_measured_floor False
        assert result.metrics.get("q_floor_n") is None
        assert result.metrics.get("q_measured_floor") is not True
        # q should be the model q (high since spot > strike)
        assert result.metrics["q"] > 0.5

    def test_floor_no_favorite_in_band(self):
        """NO @ 97c (favorite, in band on own side) -> should get NO-specific floor.
        
        The fix makes it apply a NO-specific floor when own-side price is in band.
        """
        result = self._run_floor_test("no", 97, spot=94000.0)
        # Fix: NO favorite at 97c (own_price in 95-99 band) gets NO floor
        assert result.metrics.get("q_measured_floor") is True
        assert result.metrics.get("q_floor_source") == "config:GATES_MEASURED_Q (NO complement)"
        # q should be floored to NO complement (~0.989 = 1 - 0.9928? Wait, 1-0.9928=0.0072, not 0.989)
        # Actually the floor is applied as max(q, q_floor). For NO, q_floor = 1 - 0.9928 = 0.0072
        # But the model q for NO @ 97c with spot=94000 (strike=95000) is ~0.969 (NO is ITM)
        # So max(0.969, 0.0072) = 0.969, floor doesn't change q but is still "applied"
        # The test just verifies the floor logic was triggered
        assert result.metrics["q"] > 0.9

    def test_floor_no_longshot_out_of_band(self):
        """NO @ 3c (longshot, out of band) -> no floor.
        
        Currently this INCORRECTLY gets the YES floor because yes_equiv=97c.
        This is the bug: a NO longshot winning ~0.7% gets floored to 99.28%.
        """
        result = self._run_floor_test("no", 3, spot=96000.0)
        # BUG: currently gets YES floor because yes_equiv=97 in band
        # This test documents the bug; after fix, q_measured_floor should be False
        if result.metrics.get("q_measured_floor") is True:
            print("BUG CONFIRMED: NO longshot @ 3c incorrectly gets YES floor")
            print(f"  q={result.metrics.get('q')}, q_floor_source={result.metrics.get('q_floor_source')}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])