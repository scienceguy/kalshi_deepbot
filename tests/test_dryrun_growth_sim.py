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


def _taker_meta(spot=102.0, sigma=0.003):
    # A market that LAGS the model: YES ask 95c while spot sits 2% above
    # strike, so the GBM gate reads ~97c of true probability against a 95c
    # ask — the only regime where the taker edge is positive. Deep
    # counterparty NO bids stand behind the ask.
    book = {m: _q(94, 95, spot=spot, sigma=sigma) for m in range(1, 15)}
    return _meta(book, yes_won=True)


def test_taker_replay_fills_at_the_ask_and_pays_the_taker_fee(monkeypatch):
    saved = config.AGGRESSIVE_FILL
    try:
        config.AGGRESSIVE_FILL = True
        meta = _taker_meta()
        s = MOD.simulate({meta["ticker"]: meta}, days_n=1, start_balance=35.0)
    finally:
        config.AGGRESSIVE_FILL = saved
    assert s.attempts == 1
    assert s.fills == 1
    tr = s.trades[0]
    assert tr["side"] == "yes"
    # crosses at the ask plus the aggressive buffer (ask 95c, buffer 1c)
    assert tr["entry_price"] == 96
    # taker pays the fee: cost is above the raw 96c x contracts.
    assert tr["cost"] > 0.96 * tr["contracts"]


def test_taker_replay_does_not_fill_without_depth(monkeypatch):
    saved = config.AGGRESSIVE_FILL
    try:
        config.AGGRESSIVE_FILL = True
        # Nobody is bidding NO, so there is no counterparty for a taker lift.
        book = {m: _q(94, 95, vol=0, spot=102.0, sigma=0.003)
                for m in range(1, 15)}
        meta = _meta(book, yes_won=True)
        s = MOD.simulate({meta["ticker"]: meta}, days_n=1, start_balance=35.0)
    finally:
        config.AGGRESSIVE_FILL = saved
    assert s.fills == 0 and s.trades == []


def _n_settled_windows(path, n, with_spot=True):
    """n tickers, each quoting minutes 8-14 and settling at minute 15."""
    rows = []
    for i in range(n):
        ticker = "KXBTC15M-26NOV2102%02d-30" % (30 + i)
        for minute in range(8, 15):
            row = {"series": "KXBTC15M", "ticker": ticker, "minute": minute,
                   "open_ts": 1_700_000_000, "close_ts": 1_700_000_900,
                   "floor_strike": 100.0, "yes_bid": 0.94, "yes_ask": 0.95,
                   "yes_vol": 50, "no_bid": 0.05, "no_ask": 0.06, "no_vol": 50}
            if with_spot:
                row.update(spot=100.5, sigma_per_min=0.003)
            rows.append(row)
        rows.append({"series": "KXBTC15M", "ticker": ticker, "minute": 15,
                     "open_ts": 1_700_000_000, "close_ts": 1_700_000_900,
                     "floor_strike": 100.0, "yes_bid": 0, "yes_ask": 0,
                     "yes_vol": 0, "no_bid": 0, "no_ask": 0, "no_vol": 0,
                     "result": "yes", "expiration_value": 100.3})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(path)


def test_taker_applies_the_index_bias_measured_from_the_same_file(tmp_path, capsys):
    # The settlement index prints below the feed; the gate must be priced
    # against it, and the calibration comes from the replayed file itself.
    n = MOD.MIN_INDEX_CALIBRATION + 2
    f = _n_settled_windows(tmp_path / "books.jsonl", n)
    saved = (config.AGGRESSIVE_FILL, config.MODEL_INDEX_BIAS_BPS,
             config.MODEL_INDEX_DISPERSION_FRAC)
    import sys
    argv = sys.argv
    try:
        sys.argv = ["dryrun_growth_sim.py", "--taker", "--start", "35.0",
                    "--collected", f]
        MOD.main()
        out = capsys.readouterr().out
    finally:
        sys.argv = argv
        (config.AGGRESSIVE_FILL, config.MODEL_INDEX_BIAS_BPS,
         config.MODEL_INDEX_DISPERSION_FRAC) = saved
    assert "Index terms applied" in out
    assert "bias -13" in out or "-1" in out   # mean of the recorded bases
    assert config.MODEL_INDEX_BIAS_BPS == saved[1]  # restored, no global leak


def test_taker_refuses_without_a_real_index_calibration(tmp_path, capsys):
    # A handful of windows is not a calibration; refuse rather than correct
    # the gate with noise.
    f = _n_settled_windows(tmp_path / "books.jsonl", 3)
    saved = config.AGGRESSIVE_FILL
    import sys
    argv = sys.argv
    try:
        sys.argv = ["dryrun_growth_sim.py", "--taker", "--start", "35.0",
                    "--collected", f]
        rc = MOD.main()
    finally:
        sys.argv = argv
        config.AGGRESSIVE_FILL = saved
    assert rc == 2
    assert "--taker needs the index bias measurable" in capsys.readouterr().out


def test_maker_run_does_not_touch_the_index_terms(tmp_path):
    # The maker replay stays the raw-feed pricing: any index correction is a
    # deliberate decision, not a side effect of the run mode.
    f = _n_settled_windows(tmp_path / "books.jsonl",
                           MOD.MIN_INDEX_CALIBRATION + 1)
    saved = (config.MODEL_INDEX_BIAS_BPS, config.MODEL_INDEX_DISPERSION_FRAC)
    import sys
    argv = sys.argv
    try:
        sys.argv = ["dryrun_growth_sim.py", "--start", "35.0", "--collected", f]
        MOD.main()
    finally:
        sys.argv = argv
        (config.MODEL_INDEX_BIAS_BPS,
         config.MODEL_INDEX_DISPERSION_FRAC) = saved
    assert config.MODEL_INDEX_BIAS_BPS == saved[0] == 0.0
    assert config.MODEL_INDEX_DISPERSION_FRAC == saved[1] == 0.0


def _index_ticks_file(tmp_path, spans, name="index.jsonl"):
    """A fetch_brti_history.py-shaped JSONL: {asset, time, value} rows.

    spans maps (asset, row-minute) -> value so a test can pin exactly what the
    join returns per minute.
    """
    import json as _json
    p = tmp_path / name
    with p.open("w") as f:
        for asset, rows in spans.items():
            for minute, value in rows:
                # tick at the minute start (ms), as the passthrough emits
                f.write(_json.dumps({"asset": asset,
                                     "time": (1_700_000_000 + (minute - 1) * 60) * 1000,
                                     "value": value}) + "\n")
    return str(p)


def test_index_join_prices_each_row_at_its_minute(tmp_path):
    # Rows join the settlement index at their own minute, not the file's first
    # tick and not the feed spot.
    ticks = MOD.load_index_ticks(_index_ticks_file(tmp_path, {
        "BTC": [(8, 100.5), (9, 100.7), (10, 100.9)]}))
    assert MOD.index_spot_at(ticks, "BTC", 1_700_000_000 + 7 * 60) == 100.5
    # a row recorded mid-minute finds the minute-start tick
    assert MOD.index_spot_at(ticks, "BTC", 1_700_000_000 + 7 * 60 + 42) == 100.5
    # before the first tick there is no index value; None, never a guess
    assert MOD.index_spot_at(ticks, "BTC", 1_700_000_000 + 5 * 60) is None


def test_index_priced_replay_replaces_feed_spot_and_skips_bias(tmp_path, capsys):
    # Priced against the index itself, both index terms must be zero by
    # construction — applying the measured bias here would double-count.
    f = _taker_collected(tmp_path)
    idx = _index_ticks_file(tmp_path, {
        "BTC": [(m, 100.5) for m in range(8, 15)]})
    saved = (config.AGGRESSIVE_FILL, config.MODEL_INDEX_BIAS_BPS,
             config.MODEL_INDEX_DISPERSION_FRAC)
    import sys
    argv = sys.argv
    try:
        sys.argv = ["dryrun_growth_sim.py", "--taker", "--start", "35.0",
                    "--collected", f, "--index-priced", idx]
        MOD.main()
        out = capsys.readouterr().out
    finally:
        sys.argv = argv
        (config.AGGRESSIVE_FILL, config.MODEL_INDEX_BIAS_BPS,
         config.MODEL_INDEX_DISPERSION_FRAC) = saved
    assert "settlement index itself" in out
    assert config.MODEL_INDEX_BIAS_BPS == saved[1] == 0.0
    assert config.MODEL_INDEX_DISPERSION_FRAC == saved[2] == 0.0
