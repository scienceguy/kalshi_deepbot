"""Tests for scripts/dryrun_growth_sim.py — the live-orderbook dry-run replay.

Covers the three things that make the replay honest: the cents conversion of
the historical quote book, the resting-maker fill rule (no fabricated fills),
and the settlement P&L booked against the recorded trades.
"""
import importlib.util
from pathlib import Path

import config
import risk as risk_mod

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dryrun_growth_sim.py"


def _load_module():
    """Load the sim script in a fresh namespace and undo the module-level
    patches it applies at import (config.DRY_RUN, risk.time) so the rest of
    the suite keeps the real values."""
    saved_dry = config.DRY_RUN
    saved_risk_time = risk_mod.time
    spec = importlib.util.spec_from_file_location("dryrun_growth_sim_ut", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        config.DRY_RUN = saved_dry
        risk_mod.time = saved_risk_time
    return mod


MOD = _load_module()


def _meta(book_minutes, yes_won):
    return {
        "series": "KXBTC15M",
        "open_ts": 1_700_000_000,
        "close_ts": 1_700_000_900,
        "strike": 100.0,
        "expiration": 101.0,
        "yes_won": yes_won,
        "book": book_minutes,
    }


def test_market_at_takes_the_loaders_cent_quotes():
    # build_tickers stores cents (int); market_at passes them through as cents.
    meta = _meta({8: {"bid": 96, "ask": 96, "vol": 2000}}, True)
    m = MOD.market_at("KXBTC15M-x", meta, 8)
    assert m.yes_bid == 96 and m.yes_ask == 96
    assert m.no_bid == 4 and m.no_ask == 4      # complement of a quoted YES book
    assert m.minutes_left == 7                 # 15 - minute


def _fav_book():
    # Ask 99c (p=1.0 favourite): the Kelly cap inside risk.check does not bind
    # below the 15% paper per-trade budget, so the risk gate passes and the
    # test isolates the fill rule. The maker bid rests at 98c.
    book = {m: {"bid": 98, "ask": 99, "vol": 2000} for m in range(1, 15)}
    book[14] = {"bid": 97, "ask": 98, "vol": 2000}   # ask comes down to us
    return book


def test_simulate_books_win_and_pnl_when_ask_comes_to_our_price():
    # Maker bid rests at ask-1; it only fills once the ask actually reaches it.
    tickers = {"KXBTC15M-26SEP301415-15": _meta(_fav_book(), yes_won=True)}
    s = MOD.simulate(tickers, days_n=1, start_balance=35.0)
    assert s.attempts == 1
    assert s.fills == 1
    assert len(s.trades) == 1
    tr = s.trades[0]
    assert tr["side"] == "yes" and tr["won"] is True
    assert tr["pnl"] > 0                        # paid ~98c, settled $1.00
    assert abs(s.cash - (35.0 - tr["cost"] + tr["revenue"])) < 1e-9


def test_simulate_books_loss_when_settlement_goes_against():
    tickers = {"KXBTC15M-26SEP301415-15": _meta(_fav_book(), yes_won=False)}
    s = MOD.simulate(tickers, days_n=1, start_balance=35.0)
    assert len(s.trades) == 1
    tr = s.trades[0]
    assert tr["won"] is False
    assert tr["pnl"] == -tr["cost"]            # YES settles 0: lose the cost


def test_simulate_no_fill_when_ask_never_reaches_our_price():
    book = {m: {"bid": 98, "ask": 99, "vol": 2000} for m in range(1, 15)}
    tickers = {"KXBTC15M-26SEP301415-15": _meta(book, yes_won=True)}
    s = MOD.simulate(tickers, days_n=1, start_balance=35.0)
    assert s.attempts == 1 and s.fills == 0
    assert s.trades == [] and s.cash == 35.0   # no fabricated fill


def test_simulate_no_fill_when_volume_is_zero():
    book = {m: {"bid": 95, "ask": 95, "vol": 0} for m in range(1, 15)}
    tickers = {"KXBTC15M-26SEP301415-15": _meta(book, yes_won=True)}
    s = MOD.simulate(tickers, days_n=1, start_balance=35.0)
    assert s.fills == 0 and s.trades == []     # phantom depth must not fill