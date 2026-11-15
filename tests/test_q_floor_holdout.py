"""B17: measured-q floor holdout measurement.

GATES_MEASURED_Q=0.9928 came from n=557 in-sample quote rows (553 wins) at
95-99c: quotes, not fills, and not time-ordered. The Wilson 95% lower bound is
~0.982, so the constant flatters the model by ~+1.2c before fees. The fix is a
holdout measurement from settled fills plus a gate that uses the lower bound
from that measurement, never the raw constant.

Red-first coverage:
  (a) floor disabled (default) -> gate output identical to pre-B17
      (no q_floor_n/q_floor_source keys, model q untouched);
  (b) floor enabled with a measurement file -> gate floors at the file's
      Wilson lower bound, not at 0.9928;
  (c) measurement present but below min n -> floor refused (treated as
      disabled);
  plus the script: time-ordered train/holdout split, Wilson lower bound,
  refusal to print a rate without a sample.

While the floor is disabled the gate reads no file and adds no metric keys, so
production behaviour is byte-identical to before B17.
"""

import json
import math
import sqlite3
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import config
import gates
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "q_floor_holdout.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("q_floor_holdout", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _MockMarket:
    """Minimal stand-in for MockMarket (mirrors tests/test_gates.py)."""

    def __init__(self):
        self.ticker = "KXBTC15M-26NOV090415-15"
        self.series = "KXBTC15M"
        self.strike = 95000.0
        self.minutes_left = 8.0
        self.yes_bid = 75
        self.yes_ask = 77
        self.raw = {"ticker": self.ticker, "event_ticker": "KXBTC15M"}
        self.close_time = datetime.now(timezone.utc)


def _evaluate(tmp_path, *, floor_enabled=None, q_file_body=None, q_file_name="q_floor.json"):
    """Run evaluate_entry with the floor config patched.

    q_file_body None + a *name* means the file is absent (no measurement).
    """
    market = _MockMarket()
    q_file = tmp_path / q_file_name
    if q_file_body is not None:
        q_file.write_text(q_file_body if isinstance(q_file_body, str) else json.dumps(q_file_body))
    with patch.object(gates, "_Q_FLOOR_FILE", q_file), \
         patch.object(gates, "normal_cdf", return_value=0.50), \
         patch.object(gates, "spot_for", return_value=market.strike + 50.0), \
         patch.object(gates, "atr_1m", return_value=10.0), \
         patch.object(config, "GATES_ATR_ENABLED", False), \
         patch.object(config, "GATES_EV_ENABLED", False), \
         patch.object(config, "GATES_MEASURED_Q", 0.9928), \
         patch.object(config, "GATES_MEASURED_FLOOR_ENABLED", floor_enabled):
        return gates.evaluate_entry(
            market, spot_state={}, orderbook=Mock(),
            side="yes", entry_price_cents=97)


# ── (a) floor disabled (default): output identical to pre-B17 ──────────

def test_floor_disabled_adds_no_metrics_and_leaves_q_alone(tmp_path):
    res = _evaluate(tmp_path, floor_enabled=False, q_file_name="absent.json")
    assert "q_floor_n" not in res.metrics
    assert "q_floor_source" not in res.metrics
    assert "q_measured_floor" not in res.metrics
    assert res.metrics["q"] == pytest.approx(0.50)


def test_floor_default_flag_is_false():
    # The floor must stay opt-in out of the box.
    assert config.GATES_MEASURED_FLOOR_ENABLED is False


# ── (b) floor enabled, valid measurement -> Wilson lower bound ─────────

def test_floor_enabled_uses_wilson_lower_bound_not_constant(tmp_path):
    body = {"q_floor": 0.9817, "q_floor_n": 150,
            "q_floor_source": "orders.settled_fills:fixture"}
    res = _evaluate(tmp_path, floor_enabled=True, q_file_body=body)
    assert res.metrics["q"] == pytest.approx(0.9817)
    assert res.metrics["q"] < 0.9928
    assert res.metrics["q_floor_n"] == 150
    assert res.metrics["q_floor_source"] == "orders.settled_fills:fixture"
    assert res.metrics["q_measured_floor"] is True


# ── (c) measurement below minimum n -> floor refused ───────────────────

def test_floor_below_min_n_is_refused(tmp_path):
    body = {"q_floor": 0.99, "q_floor_n": 10, "q_floor_source": "orders.settled_fills:tiny"}
    res = _evaluate(tmp_path, floor_enabled=True, q_file_body=body)
    assert res.metrics["q"] == pytest.approx(0.50)  # model q, floor not applied
    assert "q_measured_floor" not in res.metrics
    assert res.metrics["q_floor_n"] == 10
    assert res.metrics["q_floor_source"].startswith("refused")


def test_floor_corrupt_measurement_is_refused(tmp_path):
    res = _evaluate(tmp_path, floor_enabled=True, q_file_body="{not json")
    assert res.metrics["q"] == pytest.approx(0.50)
    assert "q_measured_floor" not in res.metrics
    assert res.metrics["q_floor_source"].startswith("refused")


# ── no measurement at all: pre-B17 opt-in behaviour, loudly ────────────

def test_floor_without_measurement_falls_back_to_config_constant(tmp_path):
    # Kept for compatibility with the pre-B17 opt-in contract (verified by
    # tests/test_gates.py::TestMeasuredFloorSide): absent file -> old constant,
    # but the metrics say so explicitly instead of passing off as a measurement.
    res = _evaluate(tmp_path, floor_enabled=True, q_file_name="absent.json")
    assert res.metrics["q"] == pytest.approx(0.9928)
    assert res.metrics["q_measured_floor"] is True
    assert res.metrics["q_floor_n"] == 0
    assert res.metrics["q_floor_source"] == "config:GATES_MEASURED_Q"


def test_measured_q_floor_helper_branches(tmp_path):
    f = tmp_path / "q_floor.json"
    with patch.object(gates, "_Q_FLOOR_FILE", f):
        assert gates._measured_q_floor() == (0.9928, 0, "config:GATES_MEASURED_Q")
    f.write_text(json.dumps({"q_floor": 0.9817, "q_floor_n": 150, "q_floor_source": "holdout"}))
    with patch.object(gates, "_Q_FLOOR_FILE", f):
        assert gates._measured_q_floor() == (0.9817, 150, "holdout")
    f.write_text(json.dumps({"q_floor": 0.99, "q_floor_n": 10, "q_floor_source": "holdout"}))
    with patch.object(gates, "_Q_FLOOR_FILE", f):
        floor, n, src = gates._measured_q_floor()
        assert floor is None and n == 10 and src.startswith("refused")


# ── script: Wilson lower bound math ────────────────────────────────────

def test_wilson_lower_matches_known_value():
    m = _load_script()
    lb = m.wilson_lower(553, 557)
    assert lb == pytest.approx(0.982, abs=0.002)
    assert lb < 553 / 557
    assert m.wilson_lower(0, 0) is None
    assert m.wilson_lower(10, 10) < 1.0


def _make_fills_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE orders (
            side TEXT, fill_price_cents INTEGER, outcome_won INTEGER,
            settled_at TEXT, order_id TEXT
        )
    """)
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _time_order_rows(n_in_band=200):
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(n_in_band):
        ts = (base + timedelta(minutes=15 * i)).isoformat()
        won = 1 if i < 100 else (1 if i % 2 == 0 else 0)  # train 100%, holdout 50%
        rows.append(("yes", 97, won, ts, f"ord-{i}"))
    # Out-of-band rows that must be excluded from the 95-99c measurement.
    rows.append(("yes", 100, 1, base.isoformat(), "oob-yes-100"))
    rows.append(("no", 97, 1, base.isoformat(), "oob-no-97"))
    return rows


def test_holdout_report_splits_time_ordered(tmp_path):
    m = _load_script()
    db = tmp_path / "fills.db"
    _make_fills_db(db, _time_order_rows(200))
    rep = m.holdout_report(db, band_min=95, band_max=99)
    assert rep["train_n"] == 100
    assert rep["holdout_n"] == 100
    assert rep["train_rate"] == pytest.approx(1.0)
    assert rep["holdout_rate"] == pytest.approx(0.5)
    assert rep["wilson_lower"] == pytest.approx(m.wilson_lower(50, 100))
    assert rep["wilson_lower"] < rep["holdout_rate"]


def test_holdout_report_refuses_without_a_sample(tmp_path):
    m = _load_script()
    db = tmp_path / "empty.db"
    _make_fills_db(db, [])
    rep = m.holdout_report(db, band_min=95, band_max=99)
    assert rep["holdout_n"] == 0
    assert rep["holdout_rate"] is None
    assert rep["wilson_lower"] is None


def test_load_settled_fills_dedupes_order_id_keep_last(tmp_path):
    m = _load_script()
    db = tmp_path / "dup.db"
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    rows = [
        ("yes", 97, 1, base.isoformat(), "ord-1"),                    # t1: won
        ("yes", 97, 0, (base + timedelta(minutes=1)).isoformat(), "ord-1"),  # t2: re-settled, lost
        ("yes", 97, 1, (base + timedelta(minutes=2)).isoformat(), "ord-2"),
    ]
    _make_fills_db(db, rows)
    fills = m.load_settled_fills(db)
    assert [(f["yes_equiv"], f["yes_outcome"]) for f in fills] == [(97, 0), (97, 1)]


def test_main_writes_gate_consumable_json(tmp_path):
    m = _load_script()
    db = tmp_path / "main.db"
    out = tmp_path / "q_floor.json"
    _make_fills_db(db, _time_order_rows(200))
    rc = m.main(["--db", str(db), "--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["q_floor_n"] == 100
    assert 0.0 < data["q_floor"] < 1.0
    assert data["q_floor_source"]
    assert data["wilson_lower_95"] == pytest.approx(data["q_floor"])


def test_main_refuses_to_print_without_a_sample(tmp_path, capsys):
    m = _load_script()
    db = tmp_path / "empty.db"
    out = tmp_path / "q_floor.json"
    _make_fills_db(db, [])
    rc = m.main(["--db", str(db), "--out", str(out)])
    assert rc == 1
    captured = capsys.readouterr().out
    assert "n=0" in captured
    assert "wilson" not in captured.lower()  # no bound printed without a sample
