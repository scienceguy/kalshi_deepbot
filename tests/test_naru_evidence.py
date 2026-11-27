import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fees
import naru_sweep as sweep
import naru_tick
import naru_momentum
import dryrun_10k


def test_official_outcome_is_required_and_never_inferred_from_quotes():
    assert sweep._result_yes("yes") is True
    assert sweep._result_yes(" NO ") is False
    assert sweep._result_yes("") is None
    assert sweep._result_yes(None) is None
    assert sweep._result_yes("50") is None


def test_bounded_previous_join_rejects_prefirst_future_and_stale_matches():
    indices = sweep._bounded_previous_indices(
        np.array([100, 101, 103]),
        np.array([99, 100, 100.5, 102.1, 104.1]),
        tolerance_s=1.0,
    )
    assert indices.tolist() == [-1, 0, 0, -1, -1]


def test_production_cash_helper_is_used_for_series_fees(monkeypatch):
    calls = []
    real = fees.buy_cash_out

    def spy(quantity, price, **kwargs):
        calls.append((quantity, price, kwargs))
        return real(quantity, price, **kwargs)

    monkeypatch.setattr(sweep.fees, "buy_cash_out", spy)
    result = sweep.production_buy_cash(2.0, 0.5, "KXBTC15M")

    assert result == real(2.0, 0.5, taker=True, series="KXBTC15M")
    assert calls == [(2.0, 0.5, {"taker": True, "series": "KXBTC15M"})]


def test_missing_depth_rejects_fill_and_compound_result():
    assert sweep.supported_buy_cash(1.0, 0.5, "KXBTC15M", depth=None) is None
    frame = pd.DataFrame({
        "ticker": ["KXBTC15M-X"], "entry_ts": [100], "close_s": [1000],
        "yes_win": [True], "net": [0.1],
    })

    observations = sweep.evaluate(frame, start=100.0, frac=0.5)

    assert observations["n"] == 1
    assert observations["mean_net_c"] == pytest.approx(10.0)
    assert "final" not in observations
    assert "maxdd" not in observations


def test_tick_compound_requires_and_caps_each_rows_depth():
    base = {
        "ticker": ["KXBTC15M-X", "KXBTC15M-Y"],
        "entry_ts": [0, 101], "close_s": [100, 200], "price": [0.5, 0.5],
        "yes_win": [True, True],
    }
    missing = naru_tick.compound(pd.DataFrame(base), start=10.0, frac=1.0)
    assert missing["fills"] == 0
    assert missing["final"] is None

    supported = naru_tick.compound(
        pd.DataFrame({**base, "depth": [0.5, 2.0]}), start=10.0, frac=1.0)
    first_buy = fees.buy_cash_out(0.5, 0.5, taker=True, series="KXBTC15M")
    after_first = 10.0 - first_buy + 0.5
    second_contracts = min(after_first / 0.5, 2.0)
    second_buy = fees.buy_cash_out(
        second_contracts, 0.5, taker=True, series="KXBTC15M")
    expected = after_first - second_buy + second_contracts
    assert supported["fills"] == 2
    assert supported["final"] == pytest.approx(expected)


def test_tick_overlap_cannot_reuse_cash_before_settlement():
    frame = pd.DataFrame({
        "ticker": ["KXBTC15M-A", "KXBTC15M-B"],
        "entry_ts": [0, 1], "close_s": [100, 200],
        "price": [0.5, 0.5], "yes_win": [True, True],
        "depth": [100.0, 100.0],
    })

    result = naru_tick.compound(frame, start=100.0, frac=1.0)

    assert result["fills"] == 1
    assert result["final"] == pytest.approx(148.25)


def test_tick_fractional_settlement_uses_production_cash_rounding(monkeypatch):
    monkeypatch.setattr(fees, "_cash_grid", lambda: 0.01)
    frame = pd.DataFrame({
        "ticker": ["KXBTC15M-X"], "entry_ts": [0], "close_s": [100],
        "price": [0.5], "yes_win": [True], "depth": [0.505],
    })

    result = naru_tick.compound(frame, start=100.0, frac=1.0)

    buy = fees.buy_cash_out(0.505, 0.5, taker=True, series="KXBTC15M")
    payout = fees.sell_cash_in(0.505, 1.0, taker=False, series="KXBTC15M")
    assert payout == 0.50
    assert result["final"] == pytest.approx(100.0 - buy + payout)


def test_hf_gate_index_import_has_no_execution_io():
    sys.modules.pop("hf_gate_index", None)
    with (patch("importlib.util.spec_from_file_location") as spec,
          patch("argparse.ArgumentParser.parse_args") as parse_args,
          patch("builtins.open") as open_file):
        importlib.import_module("hf_gate_index")

    assert not [call for call in spec.call_args_list if "hf_gate_sweep" in str(call)]
    parse_args.assert_not_called()
    open_file.assert_not_called()


def test_dryrun_row_without_depth_cannot_create_contracts():
    row = pd.Series({"ticker": "KXBTC15M-X", "price": 0.5})
    assert dryrun_10k.fill_row(row, balance=10.0, frac=1.0) is None


@pytest.mark.parametrize("depth", [None, 0.0, -1.0, float("nan")])
def test_invalid_depth_cannot_create_contracts_or_equity(depth):
    row = pd.Series({"ticker": "KXBTC15M-X", "price": 0.5, "depth": depth})
    assert dryrun_10k.fill_row(row, balance=10.0, frac=1.0) is None
    frame = pd.DataFrame({"ticker": ["KXBTC15M-X"], "entry_ts": [0],
                          "close_s": [100], "price": [0.5], "yes_win": [True],
                          "depth": [depth]})
    result = naru_tick.compound(frame, start=10.0, frac=1.0)
    assert result["fills"] == 0
    assert result["final"] == 10.0


def test_tick_first_entry_does_not_borrow_depth_from_second_tick():
    frame = pd.DataFrame({
        "ticker": ["KXBTC15M-X", "KXBTC15M-X"],
        "ts_ns": [1_000_000_000, 2_000_000_000],
        "yes_ask_cents": [95, 95], "no_ask_cents": [5, 5],
        "fair": [0.99, 0.99], "close_ts": [3_000_000_000, 3_000_000_000],
        "result_yes": [True, True], "depth": [None, 2.5],
    })

    entries = naru_tick.first_entries(frame, 90, 99, 0.0)

    assert len(entries) == 1
    assert pd.isna(entries.iloc[0].depth)


def test_open_winning_position_settles_with_balance_below_one():
    frame = pd.DataFrame({
        "ticker": ["KXBTC15M-X"], "entry_ts": [0], "close_s": [100],
        "price": [0.99], "yes_win": [True], "depth": [100.0],
    })

    result = naru_tick.compound(frame, start=100.0, frac=1.0)

    buy = fees.buy_cash_out(100.0, 0.99, taker=True, series="KXBTC15M")
    assert buy == pytest.approx(99.07, abs=0.001)
    assert result["final"] == pytest.approx(100.0 - buy + 100.0)
    assert result["final"] == pytest.approx(100.93, abs=0.001)


def test_ticks_for_day_preserves_official_depth_and_timestamps_to_compound(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "KDATA", tmp_path)
    monkeypatch.setattr(sweep, "_brti_day", lambda date: (
        np.array([1, 2]), np.array([110.0, 110.0]), np.array([0.01, 0.01])))
    rows = pd.DataFrame({
        "ticker": ["KXBTC15M-X", "KXBTC15M-X"],
        "ts_ns": [1_000_000_000, 2_000_000_000],
        "yes_ask_cents": [95, 95], "no_ask_cents": [5, 5],
        "strike_price": [100.0, 100.0], "result": ["yes", "yes"],
        "yes_ask_depth": [2.5, 2.5], "no_ask_depth": [3.5, 3.5],
        "depth_ts_ns": [999, 1_999], "received_ts_ns": [1_000, 2_000],
        "venue_ts_ms": [1, 2],
    })
    rows.to_parquet(tmp_path / "2026-01-01.parquet")

    ticks = naru_tick.ticks_for_day("2026-01-01")
    assert ticks is not None
    assert ticks.iloc[0].yes_ask_depth == 2.5
    assert ticks.iloc[0].depth_ts_ns == 999
    entries = naru_tick.first_entries(ticks, 90, 99, 0.0)
    assert entries.iloc[0].depth == 2.5
    assert entries.iloc[0].depth_ts_ns == 999
    assert entries.iloc[0].received_ts_ns == 1_000
    assert entries.iloc[0].venue_ts_ms == 1
    assert naru_tick.compound(entries, start=10.0, frac=1.0)["fills"] == 1


def test_tick_entry_preserves_recorded_positive_depth():
    frame = pd.DataFrame({
        "ticker": ["KXBTC15M-X"], "ts_ns": [1_000_000_000],
        "yes_ask_cents": [95], "no_ask_cents": [5], "fair": [0.99],
        "close_ts": [2_000_000_000], "result_yes": [True], "depth": [2.5],
    })
    entries = naru_tick.first_entries(frame, 90, 99, 0.0)
    assert entries.iloc[0].depth == 2.5


def test_hf_evaluator_requires_positive_depth():
    import ast
    import polars as pl
    path = Path(__file__).resolve().parents[1] / "scripts" / "hf_gate_index.py"
    tree = ast.parse(path.read_text())
    node = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == "evaluate_with_depth"), None)
    assert node is not None, "depth-gated HF evaluator is missing"
    namespace = {"load_sweep": lambda: __import__("hf_gate_sweep"), "pl": pl}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    base = {"ticker": ["K1"], "minute": [1], "ask_c": [95.0],
            "fv_c": [99.0], "win": [True]}
    missing = namespace["evaluate_with_depth"](pl.DataFrame(base), 90, 99, 0, 5, 0.0)
    assert missing["n"] == 0
    assert missing["total_c"] == 0.0
    supported = namespace["evaluate_with_depth"](
        pl.DataFrame({**base, "depth": [2.5]}), 90, 99, 0, 5, 0.0)
    assert supported["n"] == 1


def test_momentum_reports_observations_without_depth_bankroll():
    frame = pd.DataFrame({
        "ticker": ["KXBTC15M-X"] * 15,
        "minute": list(range(15)),
        "yes_ask_cents": [80 + i for i in range(15)],
        "no_ask_cents": [20 - i for i in range(15)],
        "result_yes": [True] * 15,
    })
    result = naru_momentum.simulate(
        frame, look=1, hold=1, thr=1, b0=75, b1=99,
        start=100.0, frac=0.5)
    assert result["n"] > 0
    assert "final" not in result
    assert "maxdd" not in result
