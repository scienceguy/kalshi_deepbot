"""Tests for the nice-to-have docs/type-hint pass.

The money-path diff only added docstrings and return annotations; these
tests pin that the documented entry points stay documented. No behaviour
is exercised beyond the existing suite.
"""
import sys

sys.argv = ["trader.py"]

import inspect

import signals
import trader


def test_place_order_documented():
    doc = trader.BotState._place_order.__doc__
    assert doc and "client_order_id" in doc
    assert inspect.signature(trader.BotState._place_order).return_annotation is not inspect.Parameter.empty


def test_record_filled_order_documented():
    assert trader.BotState._record_filled_order.__doc__


def test_cycle_and_main_documented():
    # Changed entry points in the money path: `cycle`, `main`, `_run_loop`.
    assert trader.cycle.__doc__ and "trading cycle" in trader.cycle.__doc__
    assert trader.main.__doc__
    assert trader._run_loop.__doc__


def test_bid_history_documented():
    assert signals.BidHistory.track_bid.__doc__
    assert signals.BidHistory.compute_momentum.__doc__
    assert signals.BidHistory._load.__annotations__.get("return") is None
