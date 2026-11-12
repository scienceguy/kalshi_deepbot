"""Unit tests for scripts/train_ml_hf.py entry simulation (no network, no HF).

The simulate() loop mirrors signals._evaluate_taker_favourite: one entry per
market, at the first in-window minute the gate lets through. A mistake here
would misreport the EV that decides whether the ML model is enabled, so it gets
a synthetic check.
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import train_ml_hf as T  # noqa: E402


def test_favourite_picks_in_band_side():
    assert T._favourite(97, 99, 95, 99) == ("YES", 99)      # YES ask in band
    assert T._favourite(1, 2, 95, 99) == ("NO", 99)         # NO = 100 - 1 in band
    assert T._favourite(50, 51, 95, 99) is None             # nothing in band


def test_favourite_tiebreak_prefers_more_extreme():
    # Both in band: YES ask 96, NO ask 100-4=96 -> equal distance, YES wins first.
    assert T._favourite(4, 96, 95, 99) == ("YES", 96)


def _rows():
    # minutes_remaining 8..4 == minutes_in 7..11, all inside the default window.
    return pd.DataFrame([
        # early minutes: favourite out of band (mid), later: YES ask 97
        {"ticker": "A", "minutes_remaining": 8.0, "kalshi_yes_bid": 0.40,
         "kalshi_yes_ask": 0.45, "yes_win": 1.0},
        {"ticker": "A", "minutes_remaining": 6.0, "kalshi_yes_bid": 0.95,
         "kalshi_yes_ask": 0.97, "yes_win": 1.0},
        {"ticker": "A", "minutes_remaining": 4.0, "kalshi_yes_bid": 0.95,
         "kalshi_yes_ask": 0.97, "yes_win": 1.0},
    ])


def test_simulate_no_gate_enters_first_in_band_minute():
    n, ev, wr = T.simulate(_rows(), None, 0.0, 95, 99, 6.0, 11.0, gate=False)
    assert n == 1               # one entry per market
    assert wr == 100.0          # it won


def test_simulate_gate_blocks_when_edge_negative():
    rows = _rows()
    n, ev, wr = T.simulate(rows, lambda r: 0.5, 0.5, 95, 99, 6.0, 11.0, gate=True)
    assert n == 0               # P(YES)=0.5 cannot clear a 97c ask + fee


def test_simulate_gate_enters_when_edge_positive():
    rows = _rows()
    n, ev, wr = T.simulate(rows, lambda r: 0.995, 0.5, 95, 99, 6.0, 11.0, gate=True)
    assert n == 1 and wr == 100.0
