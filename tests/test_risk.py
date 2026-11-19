"""Tests for risk.py — Risk management."""
import pytest
from risk import RiskManager
import config
import fees


def test_risk_initialize():
    r = RiskManager()
    r.initialize(100.0)
    assert r.state.start_balance == 100.0
    assert r.state.session_high == 100.0
    assert not r.state.halted


def test_max_trade_dollars_halted():
    r = RiskManager()
    r.state.halted = True
    assert r.max_trade_dollars(100.0, 0.0) == 0.0


def test_max_trade_dollars_consecutive_losses():
    r = RiskManager()
    r.state.consecutive_losses = 3
    assert r.max_trade_dollars(100.0, 0.0) == 0.0


def test_max_trade_dollars_exposure_ceiling():
    config.EXPOSURE_CEILING_PCT = 0.30
    config.CASH_FLOOR_PCT = 0.70
    config.MIN_CASH_ABSOLUTE = 1.00
    config.PER_TRADE_PCT = 0.08
    config.MAX_TRADE_DOLLARS = 3.00
    r = RiskManager()
    r.state.start_balance = 100.0
    r.state.session_high = 100.0
    # portfolio = 100, exposure_room = 30 - 0 = 30, cash_room = 100 - 0 - 70 = 30
    # per_trade = 100 * 0.08 = 8, max = 3
    result = r.max_trade_dollars(100.0, 0.0)
    assert result == 3.00


def test_max_trade_dollars_skips_ceiling_when_mtm_none():
    """When mark_to_market_exposure is None, the exposure-ceiling cap is
    skipped (not silently using cost-basis); cash_room / per_trade / MAX still
    bind, so a missing mtm never becomes an unbounded trade."""
    config.EXPOSURE_CEILING_PCT = 0.30
    config.CASH_FLOOR_PCT = 0.0
    config.MIN_CASH_ABSOLUTE = 0.0
    config.PER_TRADE_PCT = 0.5
    config.MAX_TRADE_DOLLARS = 50.0
    config.MAX_POSITION_TOTAL_PCT = 1.0  # disable total cap for this test
    config.MAX_POSITION_PER_TICKER_PCT = 1.0  # disable ticker cap for this test
    r = RiskManager()
    r.state.start_balance = 100.0
    r.state.session_high = 100.0
    # portfolio=195, cost-basis exposure=95. If the ceiling were applied with
    # cost-basis it would cap at max(0, 58.5-95)=0. With mtm=None the ceiling is
    # skipped, so the result is min(cash_room=100, per_trade=50, MAX=50)=50.
    result = r.max_trade_dollars(100.0, 95.0)
    assert result == 50.0


def test_max_trade_dollars_uses_mtm_for_ceiling_when_given():
    """When mtm IS provided, the ceiling uses it (not cost-basis)."""
    config.EXPOSURE_CEILING_PCT = 0.30
    config.CASH_FLOOR_PCT = 0.0
    config.MIN_CASH_ABSOLUTE = 0.0
    config.PER_TRADE_PCT = 0.5
    config.MAX_TRADE_DOLLARS = 50.0
    config.MAX_POSITION_TOTAL_PCT = 1.0  # disable total cap for this test
    config.MAX_POSITION_PER_TICKER_PCT = 1.0  # disable ticker cap for this test
    r = RiskManager()
    r.state.start_balance = 100.0
    r.state.session_high = 100.0
    # portfolio=195, mtm=20 -> exposure_room = max(0, 58.5-20)=38.5, which binds
    # below per_trade (50): the ceiling is what caps the trade at 38.5.
    result = r.max_trade_dollars(100.0, 95.0, mark_to_market_exposure=20.0)
    assert result == 38.5


def test_risk_check_blocks():
    r = RiskManager()
    r.state.start_balance = 100.0
    r.state.session_high = 100.0
    config.EXPOSURE_CEILING_PCT = 0.30
    config.CASH_FLOOR_PCT = 0.70
    config.MIN_CASH_ABSOLUTE = 1.00
    config.PER_TRADE_PCT = 0.08
    config.MAX_TRADE_DOLLARS = 3.00
    allowed, reason = r.check(100.0, 0.0, 50.0, "TEST", side="yes")
    assert not allowed
    assert "risk cap" in reason


def test_reset_daily_keeps_the_peak_and_the_halt():
    """A day roll must not un-halt a bot that is still deep in drawdown."""
    r = RiskManager()
    r.state.start_balance = 100.0
    r.state.session_high = 150.0
    r.state.halted = True
    r.state.consecutive_losses = 3
    r.reset_daily()
    assert r.state.session_high == 150.0  # peak carried over
    assert r.state.halted is True         # halt lasts until the drawdown recovers


def test_consecutive_loss_cooldown_expires_after_configured_minutes(monkeypatch):
    """B3: a 3-loss cooldown clears after LOSS_COOLDOWN_MINUTES with no win."""
    import risk
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    r = RiskManager()
    r.initialize(100.0)
    for _ in range(3):
        r.record_trade_outcome(False, -1.0)
    assert r.state.cooldown_started_at == 1000.0
    allowed, reason = r.check(100.0, 0.0, 1.0, "TESTTICKER", side="yes")
    assert allowed is False and "cooldown" in reason
    clock["t"] += (config.LOSS_COOLDOWN_MINUTES + 1) * 60.0
    allowed, reason = r.check(100.0, 0.0, 1.0, "TESTTICKER", side="yes")
    assert allowed is True
    assert r.state.consecutive_losses == 0


def test_legacy_cooldown_without_timestamp_stays_blocked(monkeypatch):
    """B3: a pre-B3 state with no start time stays blocked until a win."""
    import risk
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    clock["t"] += 10 * 3600.0
    r = RiskManager()
    r.state.consecutive_losses = 3
    r.state.cooldown_started_at = 0.0
    assert r.max_trade_dollars(100.0, 0.0) == 0.0


def test_drawdown_halt_does_not_auto_resume_by_default(monkeypatch):
    """B3: a drawdown halt needs an operator resume unless HALT_AUTO_RESUME=true."""
    import risk
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "HALT_AUTO_RESUME", False)
    r = RiskManager()
    r.initialize(100.0)
    r.update_balance(cash=60.0, exposure=0.0)  # -40% drawdown trips the halt
    assert r.state.halted is True
    clock["t"] += (config.HALT_MIN_MINUTES + 1) * 60.0
    r.update_balance(cash=60.0, exposure=0.0)
    assert r.state.halted is True


def test_drawdown_halt_auto_resumes_with_flag_after_min_minutes(monkeypatch):
    """B3: HALT_AUTO_RESUME=true lifts the halt after HALT_MIN_MINUTES."""
    import risk
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "HALT_AUTO_RESUME", True)
    r = RiskManager()
    r.initialize(100.0)
    r.update_balance(cash=60.0, exposure=0.0)
    assert r.state.halted is True
    clock["t"] += (config.HALT_MIN_MINUTES + 1) * 60.0
    r.update_balance(cash=60.0, exposure=0.0)
    assert r.state.halted is False
    assert r.state.halted_at == 0.0


def test_risk_state_persists_across_restart(monkeypatch):
    """B3: halt/cooldown fields survive a save/load round trip."""
    import risk
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    r = RiskManager()
    r.initialize(100.0)
    for _ in range(3):
        r.record_trade_outcome(False, -1.0)
    r.update_balance(cash=60.0, exposure=0.0)
    saved = r.state_dict()
    r2 = RiskManager()
    r2.load_state_dict(saved)
    assert r2.state.consecutive_losses == 3
    assert r2.state.halted is True
    assert r2.state.cooldown_started_at == pytest.approx(1000.0)
    assert r2.state.halted_at == pytest.approx(1000.0)
    assert r.state.consecutive_losses == 3  # consecutive losses preserved across daily resets


# ══════════════════════════════════════════════════════════════════════
# PILLAR B - Financial Guardrails Tests (Red-first)
# ══════════════════════════════════════════════════════════════════════

def test_daily_loss_circuit_breaker_halts_trading(monkeypatch):
    """Daily loss circuit breaker: halt trading when daily P&L exceeds limit."""
    import risk
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.05)  # 5% daily loss limit
    
    r = RiskManager()
    r.initialize(100.0)  # $100 starting balance
    
    # First, add a small loss (within limit)
    r.update_daily_pnl(-3.0)  # $3 loss (3% of $100)
    assert r.state.daily_pnl == -3.0
    assert r.state.halted is False
    
    # Add another loss that breaches the 5% limit ($5)
    halted, reason = r.update_daily_pnl(-3.0)  # Total $6 loss (6% of $100)
    assert halted is True
    assert "daily loss limit" in reason
    assert r.state.halted is True
    assert r.state.halt_reason == reason
    
    # Subsequent checks should also block
    allowed, reason = r.check(100.0, 0.0, 1.0, "TEST", total_value=100.0, side="yes")
    assert allowed is False
    assert "daily loss circuit breaker" in reason


def test_daily_loss_circuit_breaker_exact_limit_halts(monkeypatch):
    """Daily loss exactly AT the limit must halt (guard fires on equality)."""
    import risk
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.05)

    r = RiskManager()
    r.initialize(100.0)
    halted, reason = r.update_daily_pnl(-5.0)  # exactly the daily limit
    assert halted is True
    assert r.state.halted is True


def test_daily_loss_breaker_skips_uninitialized_zero_base(monkeypatch):
    """A zero-base (never initialized) RiskManager must not halt on a probe.

    risk.check() probes with update_daily_pnl(0.0); with start_balance 0.0
    the daily limit is 0.0 and 0.0 <= -0.0 is IEEE-true, which used to halt
    a fresh manager before any balance was ever known. # reviewed: zero-base
    guard in update_daily_pnl (daily_limit > 0.0 condition).
    """
    import risk
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.05)

    r = RiskManager()
    halted, reason = r.update_daily_pnl(0.0)
    assert halted is False
    assert reason == "ok"
    assert r.state.halted is False


def test_daily_loss_circuit_breaker_resets_at_midnight(monkeypatch):
    """Daily P&L resets at midnight UTC."""
    import risk
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.05)
    
    r = RiskManager()
    r.initialize(100.0)
    
    # Add a loss that would trigger halt
    r.update_daily_pnl(-6.0)
    assert r.state.halted is True
    
    # Fast-forward past midnight (24 hours)
    clock["t"] += 86400 + 1
    r.update_daily_pnl(0.0)  # Just check
    
    assert r.state.daily_pnl == 0.0
    assert r.state.halted is False  # Halt should be lifted after reset
    assert r.state.daily_pnl_reset_ts == clock["t"]


def test_kelly_sizing_with_min_ev_edge(monkeypatch):
    """Kelly sizing respects MIN_EV_EDGE_PCT - rejects trades with insufficient edge."""
    import risk
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "KELLY_FRACTION", 0.25)
    monkeypatch.setattr(config, "MIN_EV_EDGE_PCT", 0.05)  # 5% min EV edge
    monkeypatch.setattr(fees, "FEE_RATE", 0.07)
    
    r = RiskManager()
    r.initialize(100.0)
    
    # Test case: entry at 75c, win_prob = 0.80
    # Profit = 25c, Loss = 75c, b = 25/75 = 1/3
    # Kelly = (0.80 * 1/3 - 0.20) / (1/3) = 0.0667
    # Fractional Kelly (0.25) = 0.0167 * $100 = $1.67
    # EV = 0.80 * 0.25 - 0.20 * 0.75 - fee = 0.20 - 0.15 - 0.013 = 0.037 < 0.05
    kelly = r.kelly_size(0.80, 75, 100.0)
    assert kelly == 0.0  # Below min EV edge
    
    # Test case: entry at 50c, win_prob = 0.60 (sufficient edge)
    # Profit = 50c, Loss = 50c, b = 1.0
    # EV = 0.60 * 0.50 - 0.40 * 0.50 - fee = 0.10 - 0.0175 = 0.0825 > 0.05
    kelly = r.kelly_size(0.60, 50, 100.0)
    assert kelly > 0.0  # Should be positive


def test_liquidity_check_rejects_wide_spread(monkeypatch):
    """Liquidity check rejects markets with spread > MAX_SPREAD_CENTS."""
    import risk
    from market_finder import MarketInfo
    
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "MAX_SPREAD_CENTS", 10)
    monkeypatch.setattr(config, "MIN_DEPTH_CONTRACTS", 10.0)
    
    r = RiskManager()
    r.initialize(100.0)
    
    # Market with wide spread (15c > 10c max)
    market = MarketInfo(
        ticker="KXBTC15M-26NOV15-15",
        series="KXBTC15M",
        close_time=1000000.0,
        strike_type="CE",
        strike=50000.0,
        yes_bid=70, yes_ask=85,  # 15c spread
        no_bid=15, no_ask=30,
        yes_liquidity=100, no_liquidity=100,
        minutes_left=10.0,
    )
    
    ok, reason = r.check_liquidity(market, "yes")
    assert ok is False
    assert "spread 15c > max 10c" in reason
    
    # Market with tight spread (5c)
    market.yes_bid = 72
    market.yes_ask = 77
    ok, reason = r.check_liquidity(market, "yes")
    assert ok is True


def test_liquidity_check_rejects_thin_depth(monkeypatch):
    """Liquidity check rejects markets with depth < MIN_DEPTH_CONTRACTS."""
    import risk
    from market_finder import MarketInfo
    
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "MAX_SPREAD_CENTS", 10)
    monkeypatch.setattr(config, "MIN_DEPTH_CONTRACTS", 10.0)
    
    r = RiskManager()
    r.initialize(100.0)
    
    # Market with thin depth (5 < 10 min)
    market = MarketInfo(
        ticker="KXBTC15M-26NOV15-15",
        series="KXBTC15M",
        close_time=1000000.0,
        strike_type="CE",
        strike=50000.0,
        yes_bid=72, yes_ask=77,
        no_bid=23, no_ask=28,
        yes_liquidity=5, no_liquidity=100,  # Thin YES depth
        minutes_left=10.0,
    )
    
    ok, reason = r.check_liquidity(market, "yes")
    assert ok is False
    assert "depth 5 < min 10.0" in reason
    
    # Market with sufficient depth
    market.yes_liquidity = 15
    ok, reason = r.check_liquidity(market, "yes")
    assert ok is True


def test_liquidity_check_rejects_missing_book(monkeypatch):
    """Liquidity check rejects markets with missing bid/ask."""
    import risk
    from market_finder import MarketInfo
    
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "MAX_SPREAD_CENTS", 10)
    monkeypatch.setattr(config, "MIN_DEPTH_CONTRACTS", 10.0)
    
    r = RiskManager()
    r.initialize(100.0)
    
    # Market with missing ask
    market = MarketInfo(
        ticker="KXBTC15M-26NOV15-15",
        series="KXBTC15M",
        close_time=1000000.0,
        strike_type="CE",
        strike=50000.0,
        yes_bid=72, yes_ask=None,
        no_bid=23, no_ask=28,
        yes_liquidity=100, no_liquidity=100,
        minutes_left=10.0,
    )
    
    ok, reason = r.check_liquidity(market, "yes")
    assert ok is False
    assert "no yes book" in reason


def test_is_halted_rolls_daily_window(monkeypatch):
    """K6: check_halt_and_reset() rolls the daily window without adding P&L."""
    import risk
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.05)

    r = RiskManager()
    r.initialize(100.0)

    # Simulate a daily loss that would halt if the window hasn't rolled
    r.state.daily_pnl = -10.0  # exceeds 5% limit
    r.state.halted = True
    r.state.halt_reason = "daily loss limit: $-10.00 <= -$5.00"

    # Before midnight: check_halt_and_reset returns True
    assert r.check_halt_and_reset() is True

    # After midnight roll: window rolls, halt clears
    clock["t"] = 90000.0  # > 86400 seconds later
    assert r.check_halt_and_reset() is False
    assert r.state.halted is False
    assert r.state.daily_pnl == 0.0


def test_is_halted_pure_read(monkeypatch):
    """is_halted property is a pure read - no side effects."""
    import risk
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.05)

    r = RiskManager()
    r.initialize(100.0)

    # Simulate a daily loss that would halt if the window hasn't rolled
    r.state.daily_pnl = -10.0  # exceeds 5% limit
    r.state.halted = True
    r.state.halt_reason = "daily loss limit: $-10.00 <= -$5.00"

    # is_halted property should NOT roll the window
    assert r.is_halted is True
    assert r.state.daily_pnl == -10.0  # unchanged
    assert r.state.halted is True  # unchanged

    # After midnight, is_halted still returns True (no roll)
    clock["t"] = 90000.0
    assert r.is_halted is True
    assert r.state.daily_pnl == -10.0  # still unchanged


def test_check_calls_check_halt_and_reset(monkeypatch):
    """K6/S4: check() uses is_halted (pure read); check_halt_and_reset() rolls the daily window at cycle boundary."""
    import risk
    clock = {"t": 1000.0}
    monkeypatch.setattr(risk.time, "time", lambda: clock["t"])
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.05)
    monkeypatch.setattr(config, "PAPER_GUARDS_ENABLED", True)

    r = RiskManager()
    r.initialize(100.0)

    # Set up a daily loss that would halt
    r.state.daily_pnl = -10.0
    r.state.halted = True
    r.state.halt_reason = "daily loss limit: $-10.00 <= -$5.00"

    # Before midnight: check() uses is_halted (pure read) - still halted
    ok, reason = r.check(100.0, 0.0, 10.0, "KXBTC15M-1", total_value=100.0, paper=True, side="yes")
    assert ok is False
    assert "daily loss circuit breaker" in reason

    # Simulate cycle boundary: call check_halt_and_reset() which rolls the daily window
    # After midnight: window rolls, daily loss cleared, halt removed
    clock["t"] = 90000.0
    r.check_halt_and_reset()  # This rolls the daily window at midnight
    ok, reason = r.check(100.0, 0.0, 10.0, "KXBTC15M-1", total_value=100.0, paper=True, side="yes")
    assert ok is True
    assert reason == "ok"


