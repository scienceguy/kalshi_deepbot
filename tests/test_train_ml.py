"""Regression: train_from_history must parse nested BidAskDistribution candles.

Before the fix it compared raw dicts (TypeError: dict < dict) and so never
trained a model -- the reason ML was never continuously trained.
"""
import importlib.util
from datetime import datetime
from pathlib import Path

import crossvenue_history as ch
import ml_model

_SPEC = importlib.util.spec_from_file_location(
    "train_ml",
    Path(__file__).resolve().parent.parent / "scripts" / "train_ml.py",
)
tm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tm)


def test_train_from_history_parses_nested_candles(monkeypatch, tmp_path):
    close_ts = int(datetime.fromisoformat("2026-01-01T00:10:00+00:00").timestamp())
    start_ts = close_ts - 600
    strikes = {99000.0: "T1", 99500.0: "T2", 100500.0: "T3", 101000.0: "T4"}
    ev = {"expiration_value": 100000.0,
          "close_time": "2026-01-01T00:10:00Z",
          "strikes": strikes}

    candle_ts = [start_ts + 60 * k for k in range(1, 9)]
    candles = [{"end_period_ts": t,
                "yes_bid": {"close": 0.50},
                "yes_ask": {"close": 0.52}} for t in candle_ts]

    monkeypatch.setattr(ch, "SERIES_ASSET", {"KXBTCD": "BTC"})
    monkeypatch.setattr(ch, "settled_events", lambda series, limit: [ev])
    monkeypatch.setattr(ch, "candles", lambda ticker, series, a, b: candles)
    # Half the minutes carry a None sigma (too few klines): the trainer must
    # fall back to the default, not hand None to prob_above.
    monkeypatch.setattr(ch, "sigma_curve",
                        lambda *a, **k: {t: (0.0007 if i % 2 else None)
                                         for i, t in enumerate(candle_ts)})
    monkeypatch.setattr(ch, "spot_closes",
                        lambda symbol, a, b: {t - 60: 100000.0 for t in candle_ts})

    out = tmp_path / "ml_model.pt"
    model = ml_model.train_from_history(series="KXBTCD", events=1, minutes=10,
                                        epochs=1, out=str(out))
    assert model is not None
    assert out.exists()


def test_train_ml_wrapper_defaults():
    args = tm.build_parser().parse_args([])
    assert args.series == "KXBTCD"
    assert args.out.startswith("data/") and args.out.endswith(".pt")
