"""Tests for signals.py — Signal engine."""
import pytest
from unittest.mock import MagicMock
from signals import max_contracts_for_cash, _size, _maker_price
import config


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
