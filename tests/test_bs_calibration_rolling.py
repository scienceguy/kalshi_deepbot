"""Tests for Task 2: rolling Brier calibration monitor + adaptive volatility scaler.

Covers scripts/bs_calibration.py:
  * RollingCalibrationMonitor: rolling-50 Brier, 92c-99c price-band calibration,
    trailing-10 win rate, circuit breaker (Brier > 0.150, win rate < 95%).
  * AdaptiveVolatilityScaler: sigma/min_z scaling from Brier drift, clamping.
"""

import os
import tempfile
from datetime import datetime, timezone

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
    """Halt when rolling Brier > 0.150 with enough samples (>= 20)."""
    # Set config to match test expectations
    import config
    original_brier_min = config.CAL_BRIER_MIN_SAMPLES
    original_brier_thresh = config.CAL_BRIER_HALT_THRESHOLD
    config.CAL_BRIER_MIN_SAMPLES = 20
    config.CAL_BRIER_HALT_THRESHOLD = 0.150
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
            # p=0.50, outcome=0 -> Brier = 0.25 > 0.150 -> halt (20 trades >= min 20).
            for i in range(20):
                _settle(mon, f"c{i}", OUTCOME_NO, entry_price=0.50, predicted_p=0.50, z=1.0)
            halt, reason, probe = mon.circuit_breaker()
            assert halt is True
            assert "Brier" in reason, reason
            assert probe is False
    finally:
        config.CAL_BRIER_MIN_SAMPLES = original_brier_min
        config.CAL_BRIER_HALT_THRESHOLD = original_brier_thresh


def test_circuit_breaker_brier_no_halt_below_min_samples():
    """No halt when Brier is high but fewer than min_samples rows."""
    import config
    original_brier_min = config.CAL_BRIER_MIN_SAMPLES
    original_brier_thresh = config.CAL_BRIER_HALT_THRESHOLD
    config.CAL_BRIER_MIN_SAMPLES = 20
    config.CAL_BRIER_HALT_THRESHOLD = 0.150
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
            # Only 5 trades with terrible Brier — below min_samples=20 -> no halt.
            for i in range(5):
                _settle(mon, f"c{i}", OUTCOME_NO, entry_price=0.50, predicted_p=0.50, z=1.0)
            halt, reason, probe = mon.circuit_breaker()
            assert halt is False, f"unexpected halt: {reason}"
            assert probe is False
    finally:
        config.CAL_BRIER_MIN_SAMPLES = original_brier_min
        config.CAL_BRIER_HALT_THRESHOLD = original_brier_thresh


def test_circuit_breaker_win_rate_halts():
    """Halt when trailing win rate Wilson LB < floor with enough samples (>= 30)."""
    import config
    original_wr_min = config.CAL_WR_MIN_SAMPLES
    original_wr_floor = config.CAL_WR_FLOOR
    original_trailing = config.CAL_TRAILING_WIN_N
    original_wilson = config.CAL_WILSON_Z
    config.CAL_WR_MIN_SAMPLES = 30
    config.CAL_WR_FLOOR = 0.95
    config.CAL_TRAILING_WIN_N = 10  # test expects trailing 10
    config.CAL_WILSON_Z = 1.96    # 95% confidence
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
            # 30 trades, 27 wins (90% raw, Wilson LB well below 0.95).
            for i in range(30):
                outcome = OUTCOME_YES if i < 27 else OUTCOME_NO
                predicted_p = 0.99 if outcome else 0.01
                _settle(mon, f"w{i}", outcome, entry_price=0.96, predicted_p=predicted_p, z=2.0)
            halt, reason, probe = mon.circuit_breaker()
            assert halt is True, f"expected halt: {reason}"
            assert "win rate" in reason or "Wilson" in reason, reason
            assert probe is False
    finally:
        config.CAL_WR_MIN_SAMPLES = original_wr_min
        config.CAL_WR_FLOOR = original_wr_floor
        config.CAL_TRAILING_WIN_N = original_trailing
        config.CAL_WILSON_Z = original_wilson


def test_circuit_breaker_win_rate_no_halt_below_min_samples():
    """No halt when win rate is bad but fewer than min_samples trades."""
    import config
    original_wr_min = config.CAL_WR_MIN_SAMPLES
    original_wr_floor = config.CAL_WR_FLOOR
    original_trailing = config.CAL_TRAILING_WIN_N
    original_wilson = config.CAL_WILSON_Z
    config.CAL_WR_MIN_SAMPLES = 30
    config.CAL_WR_FLOOR = 0.95
    config.CAL_TRAILING_WIN_N = 10
    config.CAL_WILSON_Z = 1.96
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
            # 5 trades, all lose (0% win rate) — below min_samples=30 -> no halt.
            for i in range(5):
                _settle(mon, f"w{i}", OUTCOME_NO, entry_price=0.96, predicted_p=0.97, z=2.0)
            halt, reason, probe = mon.circuit_breaker()
            assert halt is False, f"unexpected halt: {reason}"
            assert probe is False
    finally:
        config.CAL_WR_MIN_SAMPLES = original_wr_min
        config.CAL_WR_FLOOR = original_wr_floor
        config.CAL_TRAILING_WIN_N = original_trailing
        config.CAL_WILSON_Z = original_wilson


def test_circuit_breaker_no_halt_when_healthy():
    """No halt when Brier <= 0.150 and trailing win rate >= WR_FLOOR."""
    import config
    original_brier_min = config.CAL_BRIER_MIN_SAMPLES
    original_brier_thresh = config.CAL_BRIER_HALT_THRESHOLD
    original_wr_min = config.CAL_WR_MIN_SAMPLES
    original_wr_floor = config.CAL_WR_FLOOR
    original_trailing = config.CAL_TRAILING_WIN_N
    original_wilson = config.CAL_WILSON_Z
    config.CAL_BRIER_MIN_SAMPLES = 20
    config.CAL_BRIER_HALT_THRESHOLD = 0.150
    config.CAL_WR_MIN_SAMPLES = 30
    config.CAL_WR_FLOOR = 0.95
    config.CAL_TRAILING_WIN_N = 10
    config.CAL_WILSON_Z = 1.96
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
            for i in range(12):
                _settle(mon, f"h{i}", OUTCOME_YES, entry_price=0.96, predicted_p=0.97, z=2.0)
            halt, reason, probe = mon.circuit_breaker()
            assert halt is False, reason
            assert probe is False
            assert "within limits" in reason, reason
    finally:
        config.CAL_BRIER_MIN_SAMPLES = original_brier_min
        config.CAL_BRIER_HALT_THRESHOLD = original_brier_thresh
        config.CAL_WR_MIN_SAMPLES = original_wr_min
        config.CAL_WR_FLOOR = original_wr_floor
        config.CAL_TRAILING_WIN_N = original_trailing
        config.CAL_WILSON_Z = original_wilson


def test_circuit_breaker_no_data():
    """No halt with no settled data."""
    with tempfile.TemporaryDirectory() as tmp:
        mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
        halt, reason, probe = mon.circuit_breaker()
        assert halt is False
        assert probe is False
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


# ── A1: time-bounded halt + probe + shadow calibration tests ──


def test_circuit_breaker_halt_expires_after_halt_minutes():
    """A halt that trips expires after CAL_HALT_MINUTES and enters probe mode."""
    import config
    original_brier_min = config.CAL_BRIER_MIN_SAMPLES
    original_brier_thresh = config.CAL_BRIER_HALT_THRESHOLD
    original_halt_min = config.CAL_HALT_MINUTES
    config.CAL_BRIER_MIN_SAMPLES = 20
    config.CAL_BRIER_HALT_THRESHOLD = 0.150
    config.CAL_HALT_MINUTES = 240
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
            # Trip the halt with 20 terrible trades.
            for i in range(20):
                _settle(mon, f"c{i}", OUTCOME_NO, entry_price=0.50, predicted_p=0.50, z=1.0)
            halt, reason, probe = mon.circuit_breaker()
            assert halt is True
            assert probe is False

            # Simulate time passing: set halt_started_at to 241 minutes ago.
            mon.halt_started_at = datetime.now(timezone.utc).timestamp() - (241 * 60)
            mon.halt_reason = reason
            mon.in_probe = False
            mon.probe_outcomes_count = 0
            mon._halt_state_persist()

            halt, reason2, probe = mon.circuit_breaker()
            assert halt is True, f"should still be in probe (halted=True): {reason2}"
            assert probe is True, f"should be probing after expiry: {reason2}"
            assert "probing" in reason2.lower(), reason2
    finally:
        config.CAL_BRIER_MIN_SAMPLES = original_brier_min
        config.CAL_BRIER_HALT_THRESHOLD = original_brier_thresh
        config.CAL_HALT_MINUTES = original_halt_min


def test_circuit_breaker_probe_clears_after_n_outcomes():
    """Probe mode clears after CAL_PROBE_OUTCOMES_N new settled outcomes."""
    import config
    original_brier_min = config.CAL_BRIER_MIN_SAMPLES
    original_brier_thresh = config.CAL_BRIER_HALT_THRESHOLD
    original_halt_min = config.CAL_HALT_MINUTES
    original_probe_n = config.CAL_PROBE_OUTCOMES_N
    config.CAL_BRIER_MIN_SAMPLES = 20
    config.CAL_BRIER_HALT_THRESHOLD = 0.150
    config.CAL_HALT_MINUTES = 240
    config.CAL_PROBE_OUTCOMES_N = 5
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
            # Trip the halt.
            for i in range(20):
                _settle(mon, f"c{i}", OUTCOME_NO, entry_price=0.50, predicted_p=0.50, z=1.0)
            mon.circuit_breaker()

            # Force into probe mode with 0 outcomes.
            mon.halt_started_at = datetime.now(timezone.utc).timestamp() - (241 * 60)
            mon.in_probe = True
            mon.probe_outcomes_count = 0
            mon._halt_state_persist()

            # Settle probe outcomes one at a time; each calls probe_outcome_settled.
            for i in range(5):
                mon.probe_outcome_settled()
                halt, reason, probe = mon.circuit_breaker()
                if i < 4:
                    assert probe is True, f"should still probe after {i+1} outcomes: {reason}"
                else:
                    assert halt is False, f"should be cleared after 5 outcomes: {reason}"
                    assert probe is False
    finally:
        config.CAL_BRIER_MIN_SAMPLES = original_brier_min
        config.CAL_BRIER_HALT_THRESHOLD = original_brier_thresh
        config.CAL_HALT_MINUTES = original_halt_min
        config.CAL_PROBE_OUTCOMES_N = original_probe_n


def test_circuit_breaker_shadow_log_candidate():
    """Shadow log records candidates skipped due to a halt."""
    with tempfile.TemporaryDirectory() as tmp:
        mon = RollingCalibrationMonitor(ledger_path=_make_ledger(tmp))
        mon.shadow_log_candidate(
            slug="cpc-btc-above-hr-100k", side="yes", price=0.95,
            predicted_p=0.97, z=2.0, reason="calibration halted: win rate",
        )
        assert len(mon.shadow_log) == 1
        entry = mon.shadow_log[0]
        assert entry["slug"] == "cpc-btc-above-hr-100k"
        assert entry["side"] == "yes"
        assert entry["price"] == 0.95
        assert entry["predicted_p"] == 0.97
        assert entry["z"] == 2.0
        assert "halted" in entry["reason"]
        assert "logged_at" in entry


def test_wilson_lower_bound_conservative():
    """Wilson LB is below the raw rate for small n and approaches it for large n."""
    # Small n: Wilson LB should be noticeably lower than raw rate.
    lb = RollingCalibrationMonitor._wilson_lower_bound(wins=9, n=10, z=1.96)
    raw = 0.9
    assert lb < raw, f"Wilson LB {lb} should be < raw {raw} for small n"
    assert lb >= 0.0

    # With floor comparison at call site: LB below floor means halt.
    lb2 = RollingCalibrationMonitor._wilson_lower_bound(wins=0, n=30, z=1.96)
    assert lb2 < 0.95, f"Wilson LB {lb2} should be < 0.95 floor for 0/30"

    # Large n: Wilson LB approaches raw rate.
    lb3 = RollingCalibrationMonitor._wilson_lower_bound(wins=950, n=1000, z=1.96)
    assert abs(lb3 - 0.95) < 0.02, f"Wilson LB {lb3} should be near 0.95 for large n"
