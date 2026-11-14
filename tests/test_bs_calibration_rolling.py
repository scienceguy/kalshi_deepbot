"""Tests for Task 2: rolling Brier calibration monitor + adaptive volatility scaler.

Covers scripts/bs_calibration.py:
  * RollingCalibrationMonitor: rolling-50 Brier, 92c-99c price-band calibration,
    trailing-10 win rate, circuit breaker (Brier > 0.150, win rate < 95%).
  * AdaptiveVolatilityScaler: sigma/min_z scaling from Brier drift, clamping.
"""

import json
import math
import os
import tempfile

from scripts.bs_calibration import (
    AdaptiveVolatilityScaler,
    RollingCalibrationMonitor,
)

OUTCOME_YES = 1
OUTCOME_NO = 0


def _make_ledger(tmp_path):
    return os.path.join(str(tmp_path), "bs_calib_ledger.jsonl")


def _settle(mon, slug, outcome, entry_price=0.96, predicted_p=0.97, z=2.0):
    """Record an entry then settle it; return the settled record."""
    mon.record_entry(
        slug=slug, side="yes", price_dollars=entry_price,
        predicted_p=predicted_p, z=z, strike=100.0, minutes_left=60.0,
        sigma=0.80,
    )
    return mon.reconcile_settlement(slug, outcome)


def test_rolling_brier_last_50():
    """snapshot_rolling Brier covers only the last 50 settled trades."""
    with tempfile.TemporaryDirectory() as tmp:
        mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
        # 70 trades: p=0.97, outcome=1 -> Brier = (0.03)^2 = 0.0009 for all.
        for i in range(70):
            _settle(mon, f"s{i}", OUTCOME_YES, entry_price=0.96, predicted_p=0.97, z=2.0)
        snap = mon.snapshot_rolling()
        assert snap["n"] == 50, f"expected rolling n=50, got {snap['n']}"
        assert abs(snap["rolling_brier"] - 0.0009) < 1e-9, snap["rolling_brier"]
        assert snap["overall_win_rate"] == 1.0


def test_price_band_calibration_92_99c():
    """Price-band calibration reports mean predicted p vs observed win rate in band."""
    with tempfile.TemporaryDirectory() as tmp:
        mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
        # 10 band trades @ p=0.96, all win -> gap = 0.96 - 1.00 = -0.04 = -4pp.
        for i in range(10):
            _settle(mon, f"b{i}", OUTCOME_YES, entry_price=0.96, predicted_p=0.96, z=2.0)
        # 5 out-of-band trades (entry 0.80) ignored by band.
        for i in range(5):
            _settle(mon, f"o{i}", OUTCOME_YES, entry_price=0.80, predicted_p=0.80, z=2.0)
        snap = mon.snapshot_rolling()
        band = snap["price_band_92_99c"]
        assert band is not None
        assert band["n"] == 10
        assert abs(band["mean_predicted_p"] - 0.96) < 1e-9
        assert abs(band["observed_win_rate"] - 1.0) < 1e-9
        assert abs(band["calibration_gap_pp"] - (-4.0)) < 1e-9


def test_circuit_breaker_brier_halts():
    """Halt when rolling Brier > 0.150."""
    with tempfile.TemporaryDirectory() as tmp:
        mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
        # p=0.50, outcome=0 -> Brier = 0.25 > 0.150 -> halt.
        for i in range(10):
            _settle(mon, f"c{i}", OUTCOME_NO, entry_price=0.50, predicted_p=0.50, z=1.0)
        halt, reason = mon.circuit_breaker()
        assert halt is True
        assert "0.150" in reason or "Brier" in reason, reason


def test_circuit_breaker_win_rate_halts():
    """Halt when trailing 10 win rate < 95%."""
    with tempfile.TemporaryDirectory() as tmp:
        mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
        # 10 trades, 8 wins (80% < 95%) with low Brier so only win-rate trips.
        for i in range(10):
            outcome = OUTCOME_YES if i < 8 else OUTCOME_NO
            # p near 1.0 -> small Brier contribution; mix outcomes keeps Brier < 0.150.
            predicted_p = 0.99 if outcome else 0.01
            _settle(mon, f"w{i}", outcome, entry_price=0.96, predicted_p=predicted_p, z=2.0)
        halt, reason = mon.circuit_breaker()
        assert halt is True
        assert "95%" in reason or "win rate" in reason, reason


def test_circuit_breaker_no_halt_when_healthy():
    """No halt when Brier <= 0.150 and trailing 10 win rate >= 95%."""
    with tempfile.TemporaryDirectory() as tmp:
        mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
        for i in range(12):
            _settle(mon, f"h{i}", OUTCOME_YES, entry_price=0.96, predicted_p=0.97, z=2.0)
        halt, reason = mon.circuit_breaker()
        assert halt is False, reason
        assert "within limits" in reason, reason


def test_circuit_breaker_no_data():
    """No halt with no settled data."""
    with tempfile.TemporaryDirectory() as tmp:
        mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
        halt, reason = mon.circuit_breaker()
        assert halt is False
        assert reason == "no settled data yet"


def test_adaptive_vol_scaler_degradation_widens():
    """Brier drift > 0 scales sigma up and widens min_z."""
    scaler = AdaptiveVolatilityScaler(base_sigma=0.80, base_min_z=1.80, gain=2.0)
    # First snapshot sets reference (brier=0.05); drift == 0 since reference==brier.
    scale, sigma, min_z, drift = scaler.scale(0.05)
    assert scale == 1.0
    assert sigma == 0.80
    assert min_z == 1.80
    assert abs(drift - 0.0) < 1e-9
    # Worse brier (0.10) -> drift = (0.10-0.05)/0.05 = 1.0 -> scale = 1 + 2*1 = 3, clamped to 2.0.
    scale, sigma, min_z, drift = scaler.scale(0.10)
    assert scale == 2.0  # clamped at scale_max
    assert abs(sigma - 1.60) < 1e-9
    # min_z: z_hurdle_scale = 1 + 2*1 = 3, capped at z_hurdle_max/base_min_z = 2.5/1.8.
    assert abs(min_z - 2.5) < 1e-9, min_z
    assert abs(drift - 1.0) < 1e-9


def test_adaptive_vol_scaler_improvement_narrows():
    """Brier drift < 0 scales sigma down (narrower effective hurdle); min_z stays at base."""
    scaler = AdaptiveVolatilityScaler(base_sigma=0.80, base_min_z=1.80, gain=2.0)
    scaler.scale(0.10)  # set reference
    scale, sigma, min_z, drift = scaler.scale(0.05)  # drift = (0.05-0.10)/0.10 = -0.5
    assert abs(drift - (-0.5)) < 1e-9
    # scale = 1 + 2*(-0.5) = 0, clamped to scale_min=0.5.
    assert scale == 0.5
    assert abs(sigma - 0.40) < 1e-9
    assert abs(min_z - 1.80) < 1e-9  # min_z never narrows below base


def test_adaptive_vol_scaler_clamping():
    """Extreme drifts are clamped; reference set on first non-None brier."""
    scaler = AdaptiveVolatilityScaler(base_sigma=0.80, base_min_z=1.80)
    # None brier -> no-op, reference stays None.
    scale, sigma, min_z, drift = scaler.scale(None)
    assert scale == 1.0 and sigma == 0.80 and min_z == 1.80 and drift is None
    assert scaler.reference_brier is None
    # Huge improvement -> drift -1.0 -> scale clamped to 0.5.
    scaler.scale(0.20)  # reference
    scale, sigma, min_z, _ = scaler.scale(0.01)  # drift ~ -0.95 -> scale ~ 1-1.9=-0.9 -> 0.5
    assert scale == 0.5
    assert abs(sigma - 0.40) < 1e-9


def test_idempotent_resettle_and_persistence(tmp_path):
    """Re-settling the same slug is idempotent; ledger persists across instances."""
    ledger = _make_ledger(tmp_path)
    mon1 = RollingCalibrationMonitor(ledger_path=ledger)
    mon1.record_entry(slug="dup", side="yes", price_dollars=0.96, predicted_p=0.97, z=2.0)
    r1 = mon1.reconcile_settlement("dup", OUTCOME_YES)
    r2 = mon1.reconcile_settlement("dup", OUTCOME_YES)  # idempotent: second returns None
    assert r1 is not None and r1["win"] == 1
    assert r2 is None, "re-settle of same slug must be idempotent (None)"
    snap1 = mon1.snapshot_rolling()
    assert snap1["n"] == 1

    # New instance loads the same ledger.
    mon2 = RollingCalibrationMonitor(ledger_path=ledger)
    snap2 = mon2.snapshot_rolling()
    assert snap2["n"] == 1, f"persisted ledger not loaded: {snap2}"
