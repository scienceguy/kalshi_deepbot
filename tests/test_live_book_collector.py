"""Tests for scripts/live_book_collector.py — the live fill-data recorder.

Covers the three things the dry-run replay depends on: the minute index, the
complement price math (NO ask = 100 - YES bid, matching MarketInfo), and that
a ticker is only settled once the venue has posted a result.
"""
import importlib.util
import json
import time as real_time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "live_book_collector.py"


def _load_module():
    saved_time = real_time.time
    spec = importlib.util.spec_from_file_location("live_book_collector_ut", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        real_time.time = saved_time
    return mod


MOD = _load_module()


class _FakeClient:
    """One open window closing in 300s; fixed top-of-book."""

    def __init__(self, result="yes"):
        self.result = result

    def get_markets(self, series_ticker="", status="open"):
        if series_ticker != "KXBTC15M":     # production: other series, other tickers
            return []
        close = datetime.now(timezone.utc) + timedelta(seconds=300)
        return [{"ticker": "KXBTC15M-26NOV210230-30",
                 "close_time": close.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "floor_strike": 58106.16}]

    def get_orderbook(self, ticker, depth=5):
        # Best bids: YES 64c x 3326, NO 35c x 8907.
        return {"yes": [[64, 3326]], "no": [[35, 8907]]}

    def get_market(self, ticker):
        return {"result": self.result, "expiration_value": 58106.16}


def _run(tmp_path, result="yes"):
    """Drive collect() with a faked clock: one minute per poll, 15 minutes.

    A minute per iteration reproduces the live 60s poll, so exactly one row
    per minute is written and the loop terminates when the fake clock passes
    the requested span (a frozen clock would spin: collect only sleeps while
    the remaining span exceeds one poll interval).
    """
    out = tmp_path / "books.jsonl"
    MOD.STATE_PATH = tmp_path / "state.json"   # never touch the live run's state
    clock = {"t": real_time.time()}
    saved_time, saved_sleep = MOD.time.time, MOD.time.sleep
    MOD.time.time = lambda: clock["t"]
    MOD.time.sleep = lambda s: clock.__setitem__("t", clock["t"] + 60)
    try:
        MOD.collect(_FakeClient(result), str(out), minutes=15,
                    poll_interval=0)
    finally:
        MOD.time.time, MOD.time.sleep = saved_time, saved_sleep
    return [json.loads(x) for x in out.read_text().splitlines() if x.strip()]


def test_collector_records_contemporaneous_spot_and_sigma(tmp_path, monkeypatch):
    import gates
    monkeypatch.setattr(gates, "spot_for", lambda asset: 100.5)
    monkeypatch.setattr(gates, "sigma_per_min", lambda asset: 0.003)
    rows = _run(tmp_path)
    q = next(r for r in rows if not r.get("result"))
    # The taker replay prices spot vs strike; without the recorded feed
    # values it would silently price a past strike against today's spot.
    assert q["spot"] == 100.5 and q["sigma_per_min"] == 0.003


def test_collector_minute_index_and_complement_prices(tmp_path):
    rows = _run(tmp_path)
    quotes = [r for r in rows if not r.get("result")]
    assert {r["minute"] for r in quotes} == {11, 12, 13, 14, 15}
    q = quotes[0]
    assert q["ticker"] == "KXBTC15M-26NOV210230-30"
    # Dollars, like the HF parquet: build_tickers multiplies by 100 for cents.
    assert q["yes_bid"] == 0.64 and q["no_bid"] == 0.35
    assert q["yes_ask"] == 0.65  # 100 - NO bid 35, the market's YES ask
    assert q["no_ask"] == 0.36   # 100 - YES bid 64, the market's NO ask
    assert q["yes_vol"] == 3326 and q["no_vol"] == 8907  # per-side depth


def test_collector_writes_one_settlement_row_with_the_venue_result(tmp_path):
    rows = _run(tmp_path, result="yes")
    settles = [r for r in rows if r.get("result")]
    assert len(settles) == 1
    s = settles[0]
    assert s["minute"] == 15 and s["result"] == "yes"
    assert s["expiration_value"] == 58106.16
    assert s["yes_bid"] == 0 and s["no_vol"] == 0   # blank book on the settle row


def test_collector_does_not_settle_a_ticker_with_no_result(tmp_path):
    rows = _run(tmp_path, result="")
    assert not [r for r in rows if r.get("result")]  # quote only, stays pending