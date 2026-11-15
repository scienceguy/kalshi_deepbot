"""Shared pytest fixtures.

B16: trader.state is created lazily by get_state() instead of at import time,
so this autouse fixture creates the singleton before each test. That keeps
monkeypatch.setattr(trader.state, ...) and wholesale
monkeypatch.setattr(trader, "state", ...) working as before.
"""
import sys

import pytest

sys.argv = ["trader.py"]

import trader


@pytest.fixture(autouse=True)
def _trader_state_singleton():
    """Create the bot state singleton before every test."""
    trader.get_state()
    yield
