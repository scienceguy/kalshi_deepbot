"""Tests for backtest_engine.py — core backtest logic."""
import pytest
from datetime import datetime, timedelta, timezone
from backtest_engine import (
    StratParams,
    simulate_trade,
    run_backtest,
    parameter_grid,
    _default_settlement_yes_won,
)


def _make_market(ticker="KXBTC15M-TEST", close_minutes=10, yes_won=True,
                 mid_cents=50, last_price=None):
    """Build a minimal market dict for testing.

    Uses timedelta (not minute arithmetic) so the close time is always in the
    future and never wraps past the hour.
    """
    close = datetime.now(timezone.utc) + timedelta(minutes=close_minutes)
    open_ = close - timedelta(minutes=15)  # full 15-min window, minutes_in == 15
    exp_val = 1.0 if yes_won else 0.0
    market = {
        "ticker": ticker,
        "close_time": close.isoformat(),
        "open_time": open_.isoformat(),
        "expiration_value": exp_val,
        "floor_strike": 1.0,
    }
    # Always set bid/ask — needed for mid_proxy calculation in simulate_trade
    market["yes_bid_dollars"] = str(mid_cents / 100.0)
    market["yes_ask_dollars"] = str((mid_cents + 2) / 100.0)
    market["no_bid_dollars"] = str((100 - mid_cents - 2) / 100.0)
    market["no_ask_dollars"] = str((100 - mid_cents) / 100.0)
    if last_price is not None:
        market["last_price_dollars"] = last_price / 100.0
    return market


class TestSimulateTrade:
    """Tests for simulate_trade — the core per-market simulation."""

    def test_yes_win_in_band(self):
        """YES in favorite band, YES wins → profitable trade."""
        params = StratParams(ENTRY_BAND_MIN=82, ENTRY_BAND_MAX=93)
        market = _make_market(mid_cents=85, yes_won=True, last_price=85)
        result = simulate_trade(market, params, balance=10.0)
        assert result is not None
        assert result["won"] is True
        assert result["pnl"] > 0
        assert result["direction"] == "YES"

    def test_yes_loss_in_band(self):
        """YES in favorite band, YES loses → losing trade."""
        params = StratParams(ENTRY_BAND_MIN=82, ENTRY_BAND_MAX=93)
        market = _make_market(mid_cents=85, yes_won=False, last_price=85)
        result = simulate_trade(market, params, balance=10.0)
        assert result is not None
        assert result["won"] is False
        assert result["pnl"] < 0

    def test_outside_band_no_signal(self):
        """Price outside entry band → no signal."""
        params = StratParams(ENTRY_BAND_MIN=82, ENTRY_BAND_MAX=93)
        market = _make_market(mid_cents=50, yes_won=True, last_price=50)
        result = simulate_trade(market, params, balance=10.0)
        assert result is None

    def test_insufficient_balance_no_signal(self):
        """Balance too low for even 1 contract → no signal."""
        params = StratParams(ENTRY_BAND_MIN=82, ENTRY_BAND_MAX=93,
                             MAX_TRADE_DOLLARS=100.0, PER_TRADE_PCT=0.01)
        market = _make_market(mid_cents=85, yes_won=True, last_price=85)
        result = simulate_trade(market, params, balance=0.01)
        assert result is None

    def test_insufficient_valid_times_no_signal(self):
        """Missing close/open times → no signal."""
        params = StratParams()
        market = {"ticker": "TEST", "close_time": "", "open_time": ""}
        result = simulate_trade(market, params, balance=10.0)
        assert result is None


class TestRunBacktest:
    """Tests for run_backtest — full sweep for one parameter set."""

    def test_chronological_order(self):
        """Markets are processed in chronological order (sorted by close_time)."""
        params = StratParams(ENTRY_BAND_MIN=82, ENTRY_BAND_MAX=93,
                             PER_TRADE_PCT=0.20, MAX_TRADE_DOLLARS=10.0)
        markets = []
        for i in range(5):
            open_t = f"2026-01-01T10:{i:02d}:00Z"
            close_t = f"2026-01-01T10:{i+1:02d}:00Z"
            market = {
                "ticker": f"TEST{i}",
                "close_time": close_t,
                "open_time": open_t,
                "expiration_value": 1.0,
                "floor_strike": 1.0,
                "yes_bid_dollars": "0.83",
                "yes_ask_dollars": "0.87",
                "no_bid_dollars": "0.13",
                "no_ask_dollars": "0.17",
                "last_price_dollars": "0.85",
            }
            markets.append(market)
        # Shuffle order — run_backtest should sort by close_time
        markets = list(reversed(markets))
        result = run_backtest(params, markets, starting_balance=100.0)
        assert result["n_trades"] == 5
        assert result["total_pnl"] > 0

    def test_no_trades_returns_zero(self):
        """No matching markets → zero result."""
        params = StratParams(ENTRY_BAND_MIN=82, ENTRY_BAND_MAX=93)
        market = _make_market(mid_cents=50, yes_won=True, last_price=50)
        result = run_backtest(params, [market], starting_balance=100.0)
        assert result["n_trades"] == 0
        assert result["total_pnl"] == 0.0
        assert result["ev"] == 0.0


class TestDefaultSettlement:
    """Tests for _default_settlement_yes_won."""

    def test_yes_wins_when_exp_ge_strike(self):
        assert _default_settlement_yes_won({"expiration_value": 1.0, "floor_strike": 1.0}) is True

    def test_yes_loses_when_exp_below_strike(self):
        assert _default_settlement_yes_won({"expiration_value": 0.0, "floor_strike": 1.0}) is False

    def test_returns_none_on_missing_values(self):
        assert _default_settlement_yes_won({"expiration_value": None, "floor_strike": 1.0}) is None
        assert _default_settlement_yes_won({"expiration_value": 1.0, "floor_strike": None}) is None


class TestMinuteQuotes:
    """Real per-minute book: the entry must use the quote at the window start."""

    def _market(self, quotes, result="yes"):
        close = datetime.now(timezone.utc) + timedelta(minutes=15)
        return {
            "ticker": "KXBTC15M-MQ",
            "open_time": (close - timedelta(minutes=15)).isoformat(),
            "close_time": close.isoformat(),
            "minute_quotes": quotes,
            "result": result,
        }

    def test_enters_at_quote_for_window_start(self):
        # minute 6 is 30c, minute 12 is 90c — window start 6 must pick 30c
        m = self._market({1: (20, 21), 6: (30, 31), 12: (90, 91)})
        r = simulate_trade(
            m, StratParams(ENTRY_WINDOW_START_MIN=6.0, ENTRY_BAND_MIN=15, ENTRY_BAND_MAX=55),
            balance=100.0, settlement_yes_won=lambda mk: mk["result"] == "yes",
        )
        assert r is not None
        assert r["entry_price"] == 30

    def test_window_change_changes_entry_price(self):
        m = self._market({1: (20, 21), 6: (30, 31), 12: (50, 51)})
        settle = lambda mk: mk["result"] == "yes"
        early = simulate_trade(m, StratParams(ENTRY_WINDOW_START_MIN=6.0,
                                              ENTRY_BAND_MIN=15, ENTRY_BAND_MAX=55),
                               balance=100.0, settlement_yes_won=settle)
        late = simulate_trade(m, StratParams(ENTRY_WINDOW_START_MIN=12.0,
                                             ENTRY_BAND_MIN=15, ENTRY_BAND_MAX=55),
                              balance=100.0, settlement_yes_won=settle)
        assert early["entry_price"] == 30
        assert late["entry_price"] == 50

    def test_settles_on_result(self):
        m = self._market({6: (30, 31)}, result="no")
        r = simulate_trade(
            m, StratParams(ENTRY_WINDOW_START_MIN=6.0, ENTRY_BAND_MIN=15, ENTRY_BAND_MAX=55),
            balance=100.0, settlement_yes_won=lambda mk: mk["result"] == "yes",
        )
        assert r["won"] is False


class TestParameterGrid:
    """Tests for parameter_grid — combination generation."""

    def test_quick_grid_is_smaller(self):
        quick = list(parameter_grid(quick=True))
        full = list(parameter_grid(quick=False))
        assert len(quick) < len(full)
        assert len(quick) > 0

    def test_grid_contains_expected_keys(self):
        combo = next(iter(parameter_grid(quick=True)))
        # Only knobs simulate_trade reads. Sweeping unread knobs made every
        # value score identically and produced bogus "optimal parameters".
        expected_keys = {
            "ENTRY_WINDOW_START_MIN", "ENTRY_BAND_MIN", "ENTRY_BAND_MAX",
            "AGGRESSIVE_FILL", "EXIT_STOP_LOSS_CENTS", "PER_TRADE_PCT",
            "MAX_TRADE_DOLLARS", "BALANCE_PCT_PER_TRADE",
        }
        assert set(combo.keys()) == expected_keys


class TestStopLoss:
    """The early stop must cap the loss instead of letting it settle at zero."""

    def _market(self, quotes, result):
        close = datetime.now(timezone.utc) + timedelta(minutes=15)
        return {
            "ticker": "KXBTC15M-STOP",
            "open_time": (close - timedelta(minutes=15)).isoformat(),
            "close_time": close.isoformat(),
            "minute_quotes": quotes,
            "result": result,
        }

    def _settle(self, mk):
        return mk["result"] == "yes"

    def _params(self, stop):
        return StratParams(ENTRY_WINDOW_START_MIN=6.0, ENTRY_BAND_MIN=15,
                           ENTRY_BAND_MAX=55, EXIT_STOP_LOSS_CENTS=stop,
                           MAX_TRADE_DOLLARS=1.0, BALANCE_PCT_PER_TRADE=1.0,
                           PER_TRADE_PCT=1.0)

    def test_without_stop_a_loser_settles_at_zero(self):
        # never dips below 22c, then settles NO -> full loss
        m = self._market({6: (30, 31), 7: (28, 29), 9: (40, 41)}, "no")
        r = simulate_trade(m, self._params(0), balance=100.0, settlement_yes_won=self._settle)
        assert r["won"] is False
        assert r["pnl"] < -0.2  # ~full stake lost

    def test_stop_exits_at_the_bid_and_caps_the_loss(self):
        # dips to 21c at minute 9 -> stopped at 21c instead of settling at 0
        m = self._market({6: (30, 31), 7: (25, 26), 9: (21, 22)}, "no")
        r = simulate_trade(m, self._params(8), balance=100.0, settlement_yes_won=self._settle)
        assert r["stopped"] is True
        # 3 contracts @30c in, out at 21c less the taker fee: a small loss, not the stake
        assert -0.31 < r["pnl"] < 0

    def test_stop_uses_only_quotes_after_entry(self):
        # minute 5 is already below the stop but we are not in the trade yet
        m = self._market({5: (10, 11), 6: (30, 31), 8: (35, 36)}, "yes")
        r = simulate_trade(m, self._params(8), balance=100.0, settlement_yes_won=self._settle)
        assert r["stopped"] is False
        assert r["won"] is True
