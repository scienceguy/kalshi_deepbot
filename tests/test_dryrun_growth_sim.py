"""Tests for scripts/dryrun_growth_sim.py — the live-orderbook dry-run replay.

Covers the three things that make the replay honest: the cents conversion of
the historical quote book, the resting-maker fill rule (no fabricated fills),
and the settlement P&L booked against the recorded trades.
"""
import importlib.util
import json
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


def _q(bid, ask, vol=2000, spot=0.0, sigma=0.0):
    """One book minute as build_tickers emits it: cents + per-side depth.

    spot/sigma default to 0.0 ("not recorded") exactly like a book built from
    a source that carries no feed values, so the maker tests exercise the
    unchanged path.
    """
    return {"bid": bid, "ask": ask, "vol": vol,
            "yes_vol": vol, "no_vol": vol,
            "spot": spot, "sigma_per_min": sigma}


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

def _taker_collected(tmp_path, with_spot=True):
    """A --collected JSONL whose only ticker has a recorded settlement.

    with_spot=False omits the contemporaneous feed values, standing in for a
    file collected before the 2026-11-21 schema change.
    """
    rows = []
    for minute in range(8, 15):
        rows.append({"series": "KXBTC15M", "ticker": "KXBTC15M-26NOV210230-30",
                     "minute": minute, "open_ts": 1_700_000_000,
                     "close_ts": 1_700_000_900, "floor_strike": 100.0,
                     "yes_bid": 0.97, "yes_ask": 0.98, "yes_vol": 50,
                     "no_bid": 0.02, "no_ask": 0.03, "no_vol": 50,
                     **({"spot": 100.5, "sigma_per_min": 0.003}
                        if with_spot else {})})
    rows.append({"series": "KXBTC15M", "ticker": "KXBTC15M-26NOV210230-30",
                 "minute": 15, "open_ts": 1_700_000_000, "close_ts": 1_700_000_900,
                 "floor_strike": 100.0, "yes_bid": 0, "yes_ask": 0, "yes_vol": 0,
                 "no_bid": 0, "no_ask": 0, "no_vol": 0,
                 "result": "yes", "expiration_value": 101.0})
    p = tmp_path / "books.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in (rows if with_spot else
                  [dict(r, spot=0.0, sigma_per_min=0.0) for r in rows[:-1]]
                  + [rows[-1]])) + "\n")
    return str(p)


def test_taker_mode_refuses_when_no_spot_was_recorded(tmp_path, capsys):
    # A historical strike priced against today's feed is a phantom edge; the
    # run must refuse instead of reporting a number nobody can trust.
    saved = config.AGGRESSIVE_FILL
    import sys
    argv = sys.argv
    try:
        sys.argv = ["dryrun_growth_sim.py", "--taker", "--start", "35.0",
                    "--collected", _taker_collected(tmp_path, with_spot=False)]
        rc = MOD.main()
    finally:
        sys.argv = argv
        config.AGGRESSIVE_FILL = saved
    assert rc == 2
    assert "--taker needs spot recorded per minute" in capsys.readouterr().out


def test_taker_mode_refuses_on_hf_history(tmp_path, capsys):
    # The HF parquet carries no underlying spot, so --taker --days must also
    # refuse (build_tickers has nothing to pin the gate to).
    saved_aggr = config.AGGRESSIVE_FILL
    import sys
    argv = sys.argv
    try:
        sys.argv = ["dryrun_growth_sim.py", "--taker", "--days", "2026-07-23"]
        rc = MOD.main()
    finally:
        sys.argv = argv
        config.AGGRESSIVE_FILL = saved_aggr
    assert rc == 2
    out = capsys.readouterr().out
    assert "--taker needs --collected" in out


def test_recorded_feed_pins_gates_and_restores_them():
    # signals.evaluate calls gates.spot_for / gates.sigma_per_min itself; the
    # replay substitutes the contemporaneous recording and must put the real
    # functions back — including when the wrapped call raises.
    import gates
    saved = (gates.spot_for, gates.sigma_per_min)
    try:
        with MOD._recorded_feed(100.5, 0.003):
            assert gates.spot_for("BTC") == 100.5
            assert gates.sigma_per_min("BTC") == 0.003
        try:
            with MOD._recorded_feed(100.5, 0.003):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        # the real feed functions are back, not the pinned lambdas
        assert gates.spot_for is saved[0]
        assert gates.sigma_per_min is saved[1]
    finally:
        gates.spot_for, gates.sigma_per_min = saved
