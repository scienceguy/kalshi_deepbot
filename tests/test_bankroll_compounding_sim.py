"""Tests for scripts/bankroll_compounding_sim.py (A12)."""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bankroll_compounding_sim.py"


@pytest.fixture(scope="module")
def sim():
    import sys
    spec = importlib.util.spec_from_file_location("bankroll_compounding_sim", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_jsonl(path: Path, records) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def test_breakeven_includes_fee_and_slippage(sim):
    """Break-even p must exceed the ask: slippage and the taker fee sit on top."""
    be = sim.breakeven_win_rate(0.95)
    assert 0.95 < be < 0.98


def test_drift_negative_when_win_rate_below_breakeven(sim):
    """A12 acceptance: P_WIN below the band's break-even produces negative drift."""
    rates = {lo: sim.breakeven_win_rate((lo + hi) / 2.0) - 0.05
             for lo, hi in sim.BANDS}
    for lo, hi in sim.BANDS:
        mid = (lo + hi) / 2.0
        assert rates[lo] < sim.breakeven_win_rate(mid)
    rng = np.random.default_rng(1234)
    finals = [sim.run_one(rng, sim.FRACTION_3PCT, win_rates=rates,
                          shock_prob=0.0, max_trades=400).final_balance
              for _ in range(20)]
    assert float(np.mean(finals)) < sim.START


def test_common_factor_shock_correlates_losses(sim):
    """A12 acceptance: a shocked window loses every concurrent rung together."""
    # With p=1 no independent trade ever loses, so all losses come from shocks
    # and must arrive in full batches of `concurrent`.
    for seed in (1, 2, 3, 4, 5):
        wins = sim._trade_wins(np.random.default_rng(seed), [1.0] * 20,
                               concurrent=5, shock_prob=0.5)
        losses = int((~wins).sum())
        assert losses % 5 == 0
    total = sum(int((~sim._trade_wins(np.random.default_rng(seed), [1.0] * 20,
                                      concurrent=5, shock_prob=0.5)).sum())
                for seed in (1, 2, 3, 4, 5))
    assert total > 0  # the shock actually fires in this seeded sample


def test_shock_disabled_leaves_trades_independent(sim):
    wins = sim._trade_wins(np.random.default_rng(7), [1.0] * 20,
                           concurrent=5, shock_prob=0.0)
    assert wins.all()


def test_liquidity_cap_limits_contracts(sim):
    """Size is capped by the assumed liquidity, not only by the fraction."""
    rng = np.random.default_rng(5)
    result = sim.run_one(rng, 0.90, start=100_000.0, shock_prob=0.0,
                         max_trades=50, liquidity=3)
    assert result.trades >= 1
    # 0.90 * 100k / entry would be tens of thousands of contracts; the cap binds.
    assert result.max_contracts <= 3


def test_ledger_observations_reads_settlements_and_calibration(sim, tmp_path):
    paper = _write_jsonl(tmp_path / "paper.jsonl", [
        {"event": "settlement", "slug": "a", "side": "no", "qty": 2,
         "entry_price": 0.93, "payout": 2.0, "pnl": 0.12},
        {"event": "settlement", "slug": "b", "side": "no", "qty": 2,
         "entry_price": 0.93, "payout": 0.0, "pnl": -1.88},
        {"event": "open", "slug": "c", "price": 0.96, "qty": 1},
    ])
    cal = _write_jsonl(tmp_path / "cal.jsonl", [
        {"slug": "d", "side": "yes", "entry_price": 0.98, "outcome": 1},
        {"slug": "e", "side": "no", "entry_price": 0.98, "outcome": 1},
        {"slug": "f", "side": "yes", "entry_price": 0.98, "outcome": None},
    ])
    obs = sim.ledger_observations([paper, cal])
    assert obs[0.92] == [1, 2]          # 0.93 band: one win, one loss
    assert obs[0.97] == [1, 2]          # YES won; NO lost; null outcome skipped


def test_wilson_lower_bound_below_measured_rate(sim):
    lcb = sim.wilson_lower_bound(95, 100)
    assert 0.85 < lcb < 0.95


def test_planning_rates_use_measured_lcb_when_enough_samples(sim):
    obs = {0.92: [95, 100], 0.95: [0, 0], 0.97: [0, 0]}
    rates = sim.planning_win_rates(obs)
    assert rates[0.92][0] == pytest.approx(sim.wilson_lower_bound(95, 100))
    # Bands without enough samples fall back to the documented assumption.
    assert rates[0.95][0] == sim.DEFAULT_WIN_RATES[0.95][0]