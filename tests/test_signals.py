"""Tests for signals.py — Signal engine."""
import pytest
from unittest.mock import MagicMock
from dataclasses import dataclass
from signals import (max_contracts_for_cash, _size, _maker_price, evaluate,
                     rank_signals, TradeSignal, _model_volatility)
import config
import gates
from market_finder import MarketInfo


def test_max_contracts_for_cash():
    """Survival-first sizing: contracts per $10 of cash, capped at MAX_CONTRACTS_CAP."""
    config.CONTRACTS_PER_BALANCE_UNIT = 10.0
    config.MAX_CONTRACTS_CAP = 3
    assert max_contracts_for_cash(5.0) == 1   # 5 // 10 = 0 → min 1
    assert max_contracts_for_cash(10.0) == 1   # 10 // 10 = 1
    assert max_contracts_for_cash(50.0) == 3   # 50 // 10 = 5 → capped at 3


def test_size_zero_contracts():
    assert _size(50, 0.0) == 0.0
    assert _size(0, 10.0) == 0.0
    assert _size(100, 10.0) == 0.0


def test_size_basic():
    """$3.00 budget at 50c entry: 3.00 / 0.50 = 6 contracts, capped at MAX_TRADE_DOLLARS=3.00."""
    config.MAX_TRADE_DOLLARS = 3.00
    result = _size(50, 10.0)  # min(3.00, 10.0) / 0.50 = 6.0, but max 3.00/0.50 = 6.0
    assert result == 6.0


def test_maker_price_clamp():
    """Prices must be 1-99."""
    market = MagicMock()
    market.yes_bid = 0
    market.yes_ask = 0
    market.no_bid = 0
    market.no_ask = 0
    config.AGGRESSIVE_FILL = False
    # No quotes → result = yes_ask or yes_bid or 1 = 1
    assert _maker_price(market, True) == 1


def test_maker_price_aggressive():
    """Aggressive mode: yes_ask + buffer."""
    market = MagicMock()
    market.yes_bid = 50
    market.yes_ask = 55
    market.no_bid = 45
    market.no_ask = 50
    config.AGGRESSIVE_FILL = True
    config.AGGRESSIVE_ENTRY_BUFFER_CENTS = 2
    result = _maker_price(market, True)
    assert result == 57  # yes_ask(55) + buffer(2)


def make_mock_signal(ticker, side="yes", entry_price=50, strength=50.0, confidence=1.0, bracket_role="MAIN"):
    """Helper to create a mock TradeSignal."""
    market = MagicMock(spec=MarketInfo)
    market.ticker = ticker
    market.bracket_role = bracket_role
    direction = "YES" if side == "yes" else ("NO" if side == "no" else "PASS")
    return TradeSignal(
        direction=direction,
        side=side,
        entry_price=entry_price,
        contracts=1.0,
        dollar_risk=0.5,
        reason="test",
        market=market,
        strength=strength,
        confidence=confidence,
    )


class TestRankSignals:
    """Tests for rank_signals ranking function."""

    def test_rank_signals_empty(self):
        """Empty list returns empty list."""
        assert rank_signals([]) == []

    def test_rank_signals_single(self):
        """Single signal returned as-is."""
        sig = make_mock_signal("TEST1", strength=50.0, confidence=1.0)
        result = rank_signals([sig])
        assert len(result) == 1
        assert result[0] is sig

    def test_rank_signals_by_strength(self):
        """Signals ranked by strength * confidence * boost (higher first)."""
        sig1 = make_mock_signal("TEST1", strength=50.0, confidence=1.0)  # score = 50
        sig2 = make_mock_signal("TEST2", strength=30.0, confidence=1.0)  # score = 30
        sig3 = make_mock_signal("TEST3", strength=80.0, confidence=1.0)  # score = 80
        result = rank_signals([sig1, sig2, sig3])
        assert [s.market.ticker for s in result] == ["TEST3", "TEST1", "TEST2"]

    def test_rank_signals_confidence_weighted(self):
        """Confidence affects ranking: lower confidence = lower rank."""
        sig1 = make_mock_signal("TEST1", strength=100.0, confidence=0.5)  # score = 50
        sig2 = make_mock_signal("TEST2", strength=50.0, confidence=1.0)   # score = 50
        # Same score, tie broken by ticker name (descending alphabetically since reverse=True)
        result = rank_signals([sig1, sig2])
        assert result[0].market.ticker == "TEST2"  # TEST2 > TEST1 alphabetically

    def test_rank_signals_hedge_boost(self):
        """HEDGE_NO side gets 1.2x boost."""
        sig_yes = make_mock_signal("TEST1", strength=50.0, confidence=1.0, bracket_role="MAIN")  # score = 50
        sig_no = make_mock_signal("TEST2", strength=50.0, confidence=1.0, bracket_role="HEDGE_NO")  # score = 60
        result = rank_signals([sig_yes, sig_no])
        assert result[0].market.ticker == "TEST2"
        assert result[1].market.ticker == "TEST1"

    def test_rank_signals_empty(self):
        """Empty list returns empty list."""
        assert rank_signals([]) == []

    def test_rank_signals_non_trade_filtered(self):
        """PASS signals are filtered out of ranking."""
        sig = make_mock_signal("TEST1", side="", strength=50.0, confidence=1.0)
        result = rank_signals([sig])
        assert result == []

    def test_rank_signals_tie_breaker(self):
        """Ties broken by ticker name (reverse alphabetical due to reverse=True sort)."""
        sig1 = make_mock_signal("BBB", strength=50.0, confidence=1.0)  # score = 50
        sig2 = make_mock_signal("AAA", strength=50.0, confidence=1.0)   # score = 50
        result = rank_signals([sig1, sig2])
        assert result[0].market.ticker == "BBB"
        assert result[1].market.ticker == "AAA"


# ── _model_volatility: dynamic (realized) vs fixed fallback ────────────────

def test_model_volatility_dynamic_annualizes_per_minute(monkeypatch):
    """Realized per-minute vol is annualized by sqrt(minutes/year)."""
    monkeypatch.setattr(config, "MODEL_VOL_DYNAMIC", True)
    monkeypatch.setattr(gates, "sigma_per_min", lambda asset: 0.001)
    # 0.001 * sqrt(365*24*60) ~= 0.725 — inside the clamp band.
    assert _model_volatility("BTC") == pytest.approx(0.72498, abs=1e-4)


def test_model_volatility_falls_back_when_feed_empty(monkeypatch):
    """A missing feed value must use the configured fixed vol, not zero."""
    monkeypatch.setattr(config, "MODEL_VOL_DYNAMIC", True)
    monkeypatch.setattr(config, "MODEL_VOLATILITY", 0.80)
    monkeypatch.setattr(gates, "sigma_per_min", lambda asset: None)
    assert _model_volatility("BTC") == 0.80


def test_model_volatility_disabled_uses_fixed(monkeypatch):
    """MODEL_VOL_DYNAMIC off ignores the feed entirely."""
    monkeypatch.setattr(config, "MODEL_VOL_DYNAMIC", False)
    monkeypatch.setattr(config, "MODEL_VOLATILITY", 0.55)
    monkeypatch.setattr(gates, "sigma_per_min", lambda asset: 0.01)
    assert _model_volatility("BTC") == 0.55


def test_model_volatility_clamps_extremes(monkeypatch):
    """One bad tick cannot push the model vol outside [MIN, MAX]."""
    monkeypatch.setattr(config, "MODEL_VOL_DYNAMIC", True)
    monkeypatch.setattr(config, "MODEL_VOL_MIN", 0.20)
    monkeypatch.setattr(config, "MODEL_VOL_MAX", 5.00)
    monkeypatch.setattr(gates, "sigma_per_min", lambda asset: 10.0)
    assert _model_volatility("BTC") == 5.00
    monkeypatch.setattr(gates, "sigma_per_min", lambda asset: 1e-9)
    assert _model_volatility("BTC") == 0.20
