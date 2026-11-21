"""Tests for scripts/dryrun_growth_sim.py — the live-orderbook dry-run replay.

Covers the three things that make the replay honest: the cents conversion of
the historical quote book, the resting-maker fill rule (no fabricated fills),
and the settlement P&L booked against the recorded trades.
"""
import importlib.util
from pathlib import Path

import pytest

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


@pytest.fixture(autouse=True)
def _production_no_buys():
    # Production .env has ALLOW_NO_BUYS=true, but conftest.py pins it false
    # suite-wide. The sim tests replay production config, so NO-side signals
    # must be reachable; restore the suite's value afterwards.
    saved = config.ALLOW_NO_BUYS
    config.ALLOW_NO_BUYS = True
    yield
    config.ALLOW_NO_BUYS = saved


_TICKERS = iter(range(1, 10_000))


def _meta(book_minutes, yes_won):
    # A unique ticker per call keeps the module-level bid history clean: the
    # stability gate compares a ticker against its own recent bids, so reusing
    # one ticker across tests would trip it on the second, different book.
    return {
        "series": "KXBTC15M",
        "ticker": "KXBTC15M-26SEP301430-%d" % next(_TICKERS),
        "open_ts": 1_700_000_000,
        "close_ts": 1_700_000_900,
        "strike": 100.0,
        "expiration": 101.0,
        "yes_won": yes_won,
        "book": book_minutes,
    }


def test_market_at_takes_the_loaders_cent_quotes():
    # build_tickers stores cents (int); market_at passes them through as cents.
    meta = _meta({8: _q(96, 96)}, True)
    m = MOD.market_at("KXBTC15M-x", meta, 8)
    assert m.yes_bid == 96 and m.yes_ask == 96
    assert m.no_bid == 4 and m.no_ask == 4      # complement of a quoted YES book
    assert m.minutes_left == 7                 # 15 - minute
    assert m.yes_liquidity == 2000 and m.no_liquidity == 2000  # per-side depth


def _q(bid, ask, vol=2000):
    """One book minute as build_tickers emits it: cents + per-side depth."""
    return {"bid": bid, "ask": ask, "vol": vol,
            "yes_vol": vol, "no_vol": vol}


def _fav_book():
    # Ask 99c (p=1.0 favourite): the Kelly cap inside risk.check does not bind
    # below the 15% paper per-trade budget, so the risk gate passes and the
    # test isolates the fill rule. The maker bid rests at 98c.
    book = {m: _q(98, 99) for m in range(1, 15)}
    book[14] = _q(97, 98)                        # ask comes down to us
    return book


def test_simulate_books_win_and_pnl_when_ask_comes_to_our_price():
    # Maker bid rests at ask-1; it only fills once the ask actually reaches it.
    meta = _meta(_fav_book(), yes_won=True)
    tickers = {meta["ticker"]: meta}
    s = MOD.simulate(tickers, days_n=1, start_balance=35.0)
    assert s.attempts == 1
    assert s.fills == 1
    assert len(s.trades) == 1
    tr = s.trades[0]
    assert tr["side"] == "yes" and tr["won"] is True
    assert tr["pnl"] > 0                        # paid ~98c, settled $1.00
    assert abs(s.cash - (35.0 - tr["cost"] + tr["revenue"])) < 1e-9


def test_simulate_books_loss_when_settlement_goes_against():
    meta = _meta(_fav_book(), yes_won=False)
    tickers = {meta["ticker"]: meta}
    s = MOD.simulate(tickers, days_n=1, start_balance=35.0)
    assert len(s.trades) == 1
    tr = s.trades[0]
    assert tr["won"] is False
    assert tr["pnl"] == -tr["cost"]            # YES settles 0: lose the cost


def test_simulate_no_fill_when_ask_never_reaches_our_price():
    book = {m: _q(98, 99) for m in range(1, 15)}
    meta = _meta(book, yes_won=True)
    tickers = {meta["ticker"]: meta}
    s = MOD.simulate(tickers, days_n=1, start_balance=35.0)
    assert s.attempts == 1 and s.fills == 0
    assert s.trades == [] and s.cash == 35.0   # no fabricated fill


def test_simulate_no_fill_when_volume_is_zero():
    book = {m: _q(95, 95, vol=0) for m in range(1, 15)}
    meta = _meta(book, yes_won=True)
    tickers = {meta["ticker"]: meta}
    s = MOD.simulate(tickers, days_n=1, start_balance=35.0)
    assert s.fills == 0 and s.trades == []     # phantom depth must not fill


def test_simulate_no_side_uses_the_no_ask_not_the_yes_ask():
    # Regression for the fill-rule bug: a NO bid rests against the NO ask
    # (complement of the YES bid), so it fills only when the YES bid RISES to
    # 100 - entry. Checking the YES ask instead filled NO orders on books
    # where nothing had crossed into them.
    book = {m: _q(1, 2) for m in range(1, 15)}   # NO favourite: NO ask 99c
    book[14] = _q(2, 2)                          # YES bid 1 -> 2: NO ask 99 -> 98
    meta = _meta(book, yes_won=False)
    tickers = {meta["ticker"]: meta}
    s = MOD.simulate(tickers, days_n=1, start_balance=35.0)
    assert s.attempts == 1 and s.fills == 1
    tr = s.trades[0]
    assert tr["side"] == "no" and tr["won"] is True   # NO settles $1.00
    assert tr["pnl"] > 0


def test_simulate_no_side_does_not_fill_on_a_low_yes_ask():
    # YES ask 2c is NOT the NO ask: without the YES bid rising, a NO bid at
    # 98c has no counterparty and must not fill (the old wrong check filled it).
    book = {m: _q(1, 2) for m in range(1, 15)}
    meta = _meta(book, yes_won=False)
    tickers = {meta["ticker"]: meta}
    s = MOD.simulate(tickers, days_n=1, start_balance=35.0)
    assert s.attempts == 1 and s.fills == 0
    assert s.trades == [] and s.cash == 35.0