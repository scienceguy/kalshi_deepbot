"""Tests for trend_maker.py (paper-only maker round trips)."""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

import config
import fees
import gates
import market_finder
import trend_maker as tm


@dataclass
class M:
    ticker: str = "KXBTC15M-26NOV090415-15"
    series: str = "KXBTC15M"
    strike: float = 95000.0
    yes_bid: int = 49
    yes_ask: int = 51
    no_bid: int = 49
    no_ask: int = 51
    minutes_left: float = 9.0

    @property
    def mid_yes(self):
        return (self.yes_bid + self.yes_ask) / 2


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "TREND_MAKER_MODELS", "gbm,momentum")
    monkeypatch.setattr(config, "TREND_MAKER_MIN_MODELS", 2)
    monkeypatch.setattr(config, "TREND_MAKER_MIN_SAMPLES", 3)
    monkeypatch.setattr(config, "TREND_MAKER_MIN_EDGE_CENTS", 4.0)
    monkeypatch.setattr(config, "TREND_MAKER_MIN_SLOPE", 0.5)
    monkeypatch.setattr(config, "TREND_MAKER_TP_CENTS", 3)
    monkeypatch.setattr(config, "TREND_MAKER_STOP_CENTS", 6)
    monkeypatch.setattr(config, "TREND_MAKER_MAX_POSITIONS", 2)
    monkeypatch.setattr(config, "TREND_MAKER_MAX_CONTRACTS", 2)
    monkeypatch.setattr(config, "TREND_MAKER_BANKROLL_FRACTION", 0.05)


# ── trend gauge ───────────────────────────────────────────────────────
def test_slope_rising_and_warmup():
    t = tm.TrendTracker()
    assert t.slope("X", 100, 180) == (None, 0)
    for i in range(5):
        t.add("X", 100 + i * 15, 50 + i)        # +1c per 15s = +4c/min
    s, n = t.slope("X", 160, 180)
    assert n == 5 and s == pytest.approx(4.0)


def test_slope_ignores_old_points_and_none_mid():
    t = tm.TrendTracker()
    t.add("X", 0, 10)
    t.add("X", 500, None)
    t.add("X", 501, 50)
    t.add("X", 516, 50)
    s, n = t.slope("X", 520, 60)
    assert n == 2 and s == pytest.approx(0.0)


# ── models ────────────────────────────────────────────────────────────
def test_gbm_yes_direction_and_none():
    assert tm.gbm_yes(95500, 95000, 5, 0.0007) > 0.5
    assert tm.gbm_yes(94500, 95000, 5, 0.0007) < 0.5
    assert tm.gbm_yes(95500, 95000, 5, None) is None
    assert tm.gbm_yes(95500, 95000, 0, 0.0007) is None


def test_fair_yes_fails_closed_with_one_model():
    m = M()
    with patch.object(gates, "spot_for", return_value=None), \
         patch.object(tm, "realized_sigma_frac", return_value=None):
        q, parts = tm.fair_yes(m, slope=2.0)
    assert q is None and list(parts) == ["momentum"]


def test_fair_yes_averages_enabled_models():
    m = M()
    with patch.object(gates, "spot_for", return_value=95100.0), \
         patch.object(tm, "realized_sigma_frac", return_value=0.0007):
        q, parts = tm.fair_yes(m, slope=1.0)
    assert set(parts) == {"gbm", "momentum"}
    assert q == pytest.approx(sum(parts.values()) / 2)


def test_ml_model_is_optional_and_failure_is_a_missing_model(monkeypatch):
    monkeypatch.setattr(config, "TREND_MAKER_MODELS", "momentum,ml")
    m = M()

    def boom(_m):
        raise RuntimeError("bad weights")
    q, parts = tm.fair_yes(m, slope=1.0, ml_fair=boom)
    assert q is None and "ml" not in parts
    q, parts = tm.fair_yes(m, slope=1.0, ml_fair=lambda _m: 0.60)
    assert parts["ml"] == 0.60 and q is not None


# ── decision ──────────────────────────────────────────────────────────
def test_decide_joins_yes_bid_when_trend_and_edge_agree():
    d = tm.decide(M(), 0.60, slope=2.0, n_samples=6)
    assert d.side == "yes" and d.price == 49 and d.target == 52
    assert d.price < M().yes_ask                      # never crosses
    assert d.edge == pytest.approx(11.0)


def test_decide_picks_no_on_downtrend():
    d = tm.decide(M(), 0.40, slope=-2.0, n_samples=6)
    assert d.side == "no" and d.price == 49 and d.edge == pytest.approx(11.0)


def test_decide_rejects_trend_against_edge():
    d = tm.decide(M(), 0.60, slope=-2.0, n_samples=6)
    assert d.side is None


@pytest.mark.parametrize("kw", [
    dict(fair=None, slope=2.0, n=6),        # no models
    dict(fair=0.60, slope=None, n=6),       # no trend
    dict(fair=0.60, slope=2.0, n=2),        # warm-up
    dict(fair=0.52, slope=2.0, n=6),        # edge too small
    dict(fair=0.60, slope=0.1, n=6),        # trend too weak
])
def test_decide_rejections(kw):
    assert tm.decide(M(), kw["fair"], kw["slope"], kw["n"]).side is None


def test_decide_rejects_band_spread_and_expiry():
    assert tm.decide(M(yes_bid=88, yes_ask=90, no_bid=10, no_ask=12), 0.99, 2.0, 6).side is None
    assert tm.decide(M(yes_bid=44, yes_ask=52, no_bid=48, no_ask=56), 0.70, 2.0, 6).side is None
    assert tm.decide(M(minutes_left=2.0), 0.60, 2.0, 6).side is None


def test_size_contracts_caps_and_compounds():
    assert tm.size_contracts(100, 50) == 2          # 0.05*100/0.5 = 10 -> capped at 2
    assert tm.size_contracts(10, 50) == 1           # 0.05*10/0.5 = 1
    assert tm.size_contracts(5, 50) == 0            # too small to size
    assert tm.size_contracts(0, 50) == 0


# ── engine (paper) ────────────────────────────────────────────────────
class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def tick(self, s=15):
        self.t += s


def _engine(tmp_path, clock):
    return tm.TrendMakerEngine(log_path=tmp_path / "log.jsonl", now_fn=clock)


def _rising_markets(clock, eng, n=6, start_bid=45):
    """Feed a steadily rising market so slope and edge both clear the bar."""
    m = None
    for i in range(n):
        b = start_bid + i
        m = M(yes_bid=b, yes_ask=b + 2, no_bid=100 - b - 2, no_ask=100 - b)
        with patch.object(gates, "spot_for", return_value=95400.0), \
             patch.object(tm, "realized_sigma_frac", return_value=0.0007):
            eng.step([m], balance=100.0)
        clock.tick()
    return m


def _patched_models():
    return patch.multiple(gates, spot_for=lambda a: 95400.0), \
        patch.object(tm, "realized_sigma_frac", return_value=0.0007)


def test_full_maker_round_trip_take_profit_no_fee(tmp_path):
    clock = Clock()
    eng = _engine(tmp_path, clock)
    m = _rising_markets(clock, eng)
    assert eng.entries, "should have posted a bid"
    e = next(iter(eng.entries.values()))
    price, target, c = e["price"], e["target"], e["contracts"]
    # Ask comes down through our bid -> fill.
    m2 = M(yes_bid=price - 1, yes_ask=price, no_bid=100 - price, no_ask=101 - price)
    eng.step([m2], balance=100.0); clock.tick()
    assert eng.positions and not eng.entries
    # Bid rises to target -> maker exit.
    m3 = M(yes_bid=target, yes_ask=target + 2, no_bid=98 - target, no_ask=100 - target)
    eng.step([m3], balance=100.0)
    assert not eng.positions
    assert eng.realized_total == pytest.approx((target - price) * c / 100.0)
    assert eng.round_trips == 1


def test_stop_pays_taker_fee(tmp_path):
    clock = Clock()
    eng = _engine(tmp_path, clock)
    _rising_markets(clock, eng)
    e = next(iter(eng.entries.values())); price, c = e["price"], e["contracts"]
    eng.step([M(yes_bid=price - 1, yes_ask=price, no_bid=100 - price, no_ask=101 - price)], 100.0)
    stop_bid = price - config.TREND_MAKER_STOP_CENTS
    eng.step([M(yes_bid=stop_bid, yes_ask=stop_bid + 2,
                no_bid=98 - stop_bid, no_ask=100 - stop_bid)], 100.0)
    fee = fees.taker_fee_dollars(c, stop_bid / 100.0)
    assert not eng.positions
    assert eng.realized_total == pytest.approx(-(config.TREND_MAKER_STOP_CENTS) * c / 100.0 - fee)


def test_flatten_clock_forces_taker_exit(tmp_path):
    clock = Clock()
    eng = _engine(tmp_path, clock)
    _rising_markets(clock, eng)
    e = next(iter(eng.entries.values())); price = e["price"]
    eng.step([M(yes_bid=price - 1, yes_ask=price, no_bid=100 - price, no_ask=101 - price)], 100.0)
    late = M(yes_bid=price, yes_ask=price + 2, no_bid=98 - price, no_ask=100 - price,
             minutes_left=config.TREND_MAKER_FLATTEN_MINUTES - 0.5)
    eng.step([late], 100.0)
    assert not eng.positions and eng.round_trips == 1


def test_unfilled_entry_cancels_on_ttl(tmp_path):
    clock = Clock()
    eng = _engine(tmp_path, clock)
    m = _rising_markets(clock, eng)
    assert eng.entries
    clock.tick(config.TREND_MAKER_ENTRY_TTL_SECONDS + 1)
    eng.step([m], 100.0)
    assert not eng.entries and not eng.positions


def test_halts_after_consecutive_losses_and_resets_next_day(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TREND_MAKER_MAX_CONSEC_LOSSES", 2)
    clock = Clock()
    eng = _engine(tmp_path, clock)
    eng._roll_day(clock())
    eng.consec_losses = 2
    assert eng.halted()
    m = _rising_markets(clock, eng)
    assert not eng.entries                           # halted: no new entries
    clock.tick(86400)
    eng.step([m], 100.0)
    assert eng.consec_losses == 0 and not eng.halted()


def test_daily_loss_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TREND_MAKER_DAILY_LOSS_STOP", 1.0)
    eng = _engine(tmp_path, Clock())
    eng.realized_today = -1.01
    assert "daily loss" in eng.halted()


def test_engine_never_touches_a_client_and_logs_events(tmp_path):
    clock = Clock()
    eng = _engine(tmp_path, clock)
    _rising_markets(clock, eng)
    assert not hasattr(eng, "client")
    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert lines and all('"paper": true' in l for l in lines)


# ── market_finder extra band ──────────────────────────────────────────
class _MidClient:
    def get_markets(self, series_ticker=None, status="open", limit=50):
        ct = datetime.now(timezone.utc) + timedelta(minutes=10)
        return [{"ticker": f"{series_ticker}-26NOV090415-15",
                 "series_ticker": series_ticker, "status": "active",
                 "close_time": ct.isoformat(), "floor_strike": 95000.0,
                 "yes_bid_dollars": "0.49", "yes_ask_dollars": "0.51",
                 "no_bid_dollars": "0.49", "no_ask_dollars": "0.51",
                 "yes_bid_size_fp": "100", "no_bid_size_fp": "100"}]

    def get_orderbook(self, ticker, depth=5):
        return {"yes": [[49, 50]], "no": [[49, 50]]}

    def invalidate_markets(self, series_ticker=None):
        pass


def test_extra_band_returns_mid_markets_without_leaking_into_favourites(monkeypatch):
    monkeypatch.setattr(config, "TARGET_SERIES", ["KXBTC15M"])
    monkeypatch.setattr(config, "MAX_HEDGE_PER_SERIES", 0)
    plain = market_finder.find_active_markets(_MidClient())
    assert len(plain) == 0 and plain.extra == []
    wide = market_finder.find_active_markets(_MidClient(), extra_band=(30, 70))
    assert len(wide) == 0                       # favourite list is unchanged
    assert [m.ticker for m in wide.extra] == ["KXBTC15M-26NOV090415-15"]
