"""Tests for gates.py — ATR hurdle, fee-adjusted EV, time-decay cushion."""
import math
from types import SimpleNamespace

import pytest

import gates


def _market(strike=100000.0, minutes=10.0,
            ticker="KXBTC15M-26NOV08-1200"):
    return SimpleNamespace(
        ticker=ticker, series="KXBTC15M", strike=strike,
        minutes_left=minutes, yes_bid=50, yes_ask=52, no_bid=48, no_ask=50,
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No network, no repo log file, blocking (non-shadow) by default."""
    monkeypatch.setattr(gates.config, "GATES_SHADOW_MODE", False)
    monkeypatch.setattr(gates.config, "GATES_LOG_FILE", tmp_path / "gates.jsonl")
    monkeypatch.setattr(gates, "spot_for", lambda asset: None)
    monkeypatch.setattr(gates, "atr_1m", lambda asset: None)
    monkeypatch.setattr(gates, "_last_decision", {})


def test_atr_hurdle_pass():
    # required 10 < expected 10*sqrt(10)=31.6 -> ratio 0.32
    res = gates.evaluate_entry(
        _market(strike=100010.0, minutes=10.0),
        {"spot": 100000.0, "atr_1m": 10.0},
        side="yes", entry_price_cents=30,
    )
    assert res.allowed, res.reasons
    assert res.metrics["hurdle_ratio"] == pytest.approx(0.3162, abs=1e-3)
    assert res.metrics["decision"] == "allow"


def test_atr_hurdle_fail():
    # required 200 > expected 31.6 -> ratio 6.3, blocked on gate1
    res = gates.evaluate_entry(
        _market(strike=100200.0, minutes=10.0),
        {"spot": 100000.0, "atr_1m": 10.0},
        side="yes", entry_price_cents=30,
    )
    assert not res.allowed
    assert any(r.startswith("gate1:") for r in res.reasons)
    assert res.metrics["hurdle_ratio"] == pytest.approx(6.324, abs=1e-2)


def test_fee_at_50c_and_90c():
    # Parabolic taker fee: 0.07 * p * (1-p), rounded up to 6dp in fees.py
    # (float representation can add 1e-6, hence the tolerance).
    assert gates.entry_fee(0.50) == pytest.approx(0.0175, abs=2e-6)
    assert gates.entry_fee(0.90) == pytest.approx(0.0063, abs=2e-6)
    # Break-even q is price + fee.
    assert gates.breakeven_q(0.50, gates.entry_fee(0.50)) == pytest.approx(0.5175, abs=2e-6)
    assert gates.breakeven_q(0.90, gates.entry_fee(0.90)) == pytest.approx(0.9063, abs=2e-6)


def test_ev_sign_flip():
    p = 0.90
    fee = gates.entry_fee(p)
    be = gates.breakeven_q(p, fee)
    assert gates.net_ev(be + 0.01, p, fee) > 0
    assert gates.net_ev(be - 0.01, p, fee) < 0
    # No fee: the break-even probability is exactly the price.
    assert gates.net_ev(0.51, 0.50, 0.0) > 0
    assert gates.net_ev(0.49, 0.50, 0.0) < 0


def test_cushion_z_decay():
    # spot 100, strike 99.9, sigma $0.10/sqrt-min -> z shrinks as time grows
    kwargs = dict(spot=100.0, strike=99.9, sigma=0.1, side="yes")
    z10 = gates.cushion_z(minutes=10.0, **kwargs)
    z2 = gates.cushion_z(minutes=2.0, **kwargs)
    z05 = gates.cushion_z(minutes=0.5, **kwargs)
    assert z10 == pytest.approx(0.1 / (0.1 * math.sqrt(10.0)))
    assert z2 == pytest.approx(0.1 / (0.1 * math.sqrt(2.0)))
    assert z05 == pytest.approx(0.1 / (0.1 * math.sqrt(0.5)))
    assert z10 < z2 < z05
    # NO is the mirror: same cushion, opposite sign.
    assert gates.cushion_z(minutes=10.0, **{**kwargs, "side": "no"}) == pytest.approx(-z10)


def test_warmup_fail_closed(monkeypatch):
    # ATR feed returns None (too few candles): deny, never crash.
    monkeypatch.setattr(gates, "atr_1m", lambda asset: None)
    res = gates.evaluate_entry(
        _market(), {"spot": 100000.0},
        side="yes", entry_price_cents=30,
    )
    assert not res.allowed
    assert any("insufficient candles" in r for r in res.reasons)
    assert res.metrics["atr_1m"] is None


def test_zero_time_guard():
    res = gates.evaluate_entry(
        _market(minutes=0.0), {"spot": 100000.0, "atr_1m": 10.0},
        side="yes", entry_price_cents=30,
    )
    assert not res.allowed
    assert any("no minutes remaining" in r for r in res.reasons)
    assert any("no time remaining" in r for r in res.reasons)
    assert res.metrics["q"] is None
    # Must not raise ZeroDivisionError on a negative/zero clock either.
    res_neg = gates.evaluate_entry(
        _market(minutes=-0.5), {"spot": 100000.0, "atr_1m": 10.0},
        side="yes", entry_price_cents=30,
    )
    assert not res_neg.allowed


def test_gate1_side_aware_ignores_in_the_money_distance():
    # YES already above the strike: no move is needed to win, so Gate 1 passes
    # instead of reading the 200-point cushion as a 6.3x hurdle.
    res = gates.evaluate_entry(
        _market(strike=99800.0), {"spot": 100000.0, "atr_1m": 10.0},
        side="yes", entry_price_cents=30,
    )
    assert not any(r.startswith("gate1:") for r in res.reasons)
    assert res.metrics["hurdle_ratio"] == 0.0
    # NO in the same market is 200 points out of the money -> still blocked.
    res_no = gates.evaluate_entry(
        _market(strike=99800.0), {"spot": 100000.0, "atr_1m": 10.0},
        side="no", entry_price_cents=70,
    )
    assert any(r.startswith("gate1:") for r in res_no.reasons)


def test_gate1_spec_literal_mode_is_side_agnostic(monkeypatch):
    monkeypatch.setattr(gates.config, "GATES_ATR_SIDE_AWARE", False)
    res = gates.evaluate_entry(
        _market(strike=99800.0), {"spot": 100000.0, "atr_1m": 10.0},
        side="yes", entry_price_cents=30,
    )
    assert any(r.startswith("gate1:") for r in res.reasons)
    assert res.metrics["hurdle_ratio"] == pytest.approx(6.324, abs=1e-2)


def test_shadow_mode_logs_but_allows(monkeypatch):
    monkeypatch.setattr(gates.config, "GATES_SHADOW_MODE", True)
    res = gates.evaluate_entry(
        _market(strike=100200.0), {"spot": 100000.0, "atr_1m": 10.0},
        side="yes", entry_price_cents=30,
    )
    assert res.allowed  # would block, but shadow mode never blocks
    assert res.metrics["decision"] == "would_block"
    assert res.reasons  # diagnostics still recorded


def test_gate2_blocks_above_break_even_price():
    # Win probability ~0.376 but paying 40c: net EV is negative after fees.
    res = gates.evaluate_entry(
        _market(strike=100010.0), {"spot": 100000.0, "atr_1m": 10.0},
        side="yes", entry_price_cents=40,
    )
    assert not res.allowed
    assert any(r.startswith("gate2:") for r in res.reasons)
    assert res.metrics["net_ev"] < 0
    assert res.metrics["breakeven_q"] > res.metrics["q"]


def test_log_has_outcome_join_keys():
    import json
    from pathlib import Path

    ticker = "KXBTC15M-26NOV08-1200"
    gates.evaluate_entry(
        _market(ticker=ticker), {"spot": 100000.0, "atr_1m": 10.0},
        side="yes", entry_price_cents=30,
    )
    rec = json.loads(Path(gates.config.GATES_LOG_FILE).read_text().splitlines()[-1])
    for key in ("ts", "ticker", "event_ticker", "close_time", "side",
                "entry_price_cents", "strike", "spot", "atr_1m",
                "minutes_left", "hurdle_ratio", "cushion_z", "q", "fee",
                "net_ev", "breakeven_q", "high_gamma_zone", "decision", "reasons"):
        assert key in rec, key
    # Join keys: with no raw event_ticker, the market ticker is the fallback;
    # close_time is ISO (or None for a stub without one).
    assert rec["event_ticker"] == ticker



def test_all_live_series_are_scored():
    # Every Kalshi 15M series the bot trades has a spot/klines feed, so none
    # of them may be denied as an unsupported underlying.
    for series, asset in gates.SERIES_ASSET.items():
        assert gates.asset_for(_market_for_series(series)) == asset
    assert gates.asset_for(_market_for_series("KXADA15M")) is None


def _market_for_series(series):
    from types import SimpleNamespace
    return SimpleNamespace(ticker=f"{series}-26NOV08-1200", series=series)


def test_log_written_only_when_decision_changes():
    from pathlib import Path

    market = _market(strike=100010.0)
    # 30c: passes; repeated evaluations must not append duplicate lines.
    for _ in range(3):
        gates.evaluate_entry(market, {"spot": 100000.0, "atr_1m": 10.0},
                             side="yes", entry_price_cents=30)
    log_path = Path(gates.config.GATES_LOG_FILE)
    assert len(log_path.read_text().splitlines()) == 1
    # 40c flips the decision (negative EV) -> exactly one new line.
    for _ in range(2):
        gates.evaluate_entry(market, {"spot": 100000.0, "atr_1m": 10.0},
                             side="yes", entry_price_cents=40)
    assert len(log_path.read_text().splitlines()) == 2


def test_sub_dollar_reason_is_not_rounded_to_zero():
    # DOGE ~ $0.07: a 2-decimal format printed "required 0.00 > ... 0.00".
    res = gates.evaluate_entry(
        _market(strike=0.0730, minutes=10.0), {"spot": 0.0696, "atr_1m": 0.0002},
        side="yes", entry_price_cents=30,
    )
    reason = next(r for r in res.reasons if r.startswith("gate1:"))
    assert "required 0.00 " not in reason and "move 0.00 " not in reason
    assert "0.0034" in reason


def test_prefetch_warms_each_asset_once(monkeypatch):
    calls = []
    monkeypatch.setattr(gates, "spot_for",
                        lambda asset: calls.append(("spot", asset)))
    monkeypatch.setattr(gates, "atr_1m",
                        lambda asset: calls.append(("atr", asset)))
    gates.prefetch(["BTC", "ETH", "BTC", None])
    assert sorted(calls) == [("atr", "BTC"), ("atr", "ETH"),
                             ("spot", "BTC"), ("spot", "ETH")]
