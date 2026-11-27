"""Schema tests only; these fixtures are not a study or edge evidence."""
import hashlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parent.parent
EVALUATOR_PATH = ROOT / "scripts" / "pm_us_protocol_evaluator.py"
MANIFEST_PATH = ROOT / "config" / "pm_us_protocol_evaluator_v1.json"


def _evaluator():
    spec = importlib.util.spec_from_file_location("pm_us_protocol_evaluator", EVALUATOR_PATH)
    assert spec is not None, "canonical tracked evaluator is missing"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row():
    return {
        "market": "fixture-market",
        "settled_day": "2026-11-26",
        "quantity": 1.0,
        "pnl": 0.01,
        "side": "no",
        "real_fill": False,
        "fill_provenance": "book_simulated",
        "held_to_settlement": True,
        "entry_ts": 99.9,
        "entry_price": 0.75,
        "official_outcome": 1.0,
        "production_fee_dollars": 0.001,
        "real_depth": 2.0,
        "venue_ts": 100.0,
        "order_arrival_ts": 100.1,
        "outcome_provenance": "official_settlement",
        "fee_provenance": "production",
        "depth_provenance": "official_book",
    }


def test_manifest_registers_exact_protocol_and_current_evaluator_hash():
    manifest = json.loads(MANIFEST_PATH.read_text())
    assert manifest["schema_version"] == 2
    assert manifest["evaluator"]["path"] == "scripts/pm_us_protocol_evaluator.py"
    assert manifest["evaluator"]["sha256"] == hashlib.sha256(EVALUATOR_PATH.read_bytes()).hexdigest()
    assert manifest["utc_scope"] == {
        "start": "2026-11-25T00:00:00Z",
        "end": "2026-11-27T00:00:00Z",
        "mode": "dry_research",
    }
    assert manifest["study"]["exact_m"] == 100
    assert manifest["inference"] == {
        "seed": 17008, "repetitions": 10000,
        "minimum_markets": 300, "minimum_utc_days": 14,
    }
    assert manifest["required_evidence"]["fill_provenance"] == "book_simulated"
    assert manifest["required_evidence"]["real_fill"] is False
    assert manifest["promotion"]["human_versioned_evaluation_required"] is False
    assert manifest["promotion"]["technical_gates_required"] is True
    assert manifest["promotion"]["live_orders"] == "separately_human_gated"
    assert manifest["promotion"]["positive_result_implied"] is False


def test_tracked_evaluator_calls_strict_helper_and_rejects_legacy_fallback():
    evaluator = _evaluator()
    with patch.object(evaluator.execution_realism, "protocol_clustered_lower_bound",
                      return_value=-0.01) as strict:
        with patch.object(evaluator.execution_realism, "clustered_lower_bound",
                          side_effect=AssertionError("legacy fallback called")):
            assert evaluator.evaluate_rows([_row()]) == -0.01
    strict.assert_called_once_with([_row()], exact_m=100)


def test_strict_none_does_not_invoke_legacy_fallback():
    evaluator = _evaluator()
    with patch.object(evaluator.execution_realism, "protocol_clustered_lower_bound",
                      return_value=None):
        with patch.object(evaluator.execution_realism, "clustered_lower_bound",
                          side_effect=AssertionError("legacy fallback called")):
            assert evaluator.evaluate_rows([_row()]) is None


def test_single_real_fill_is_insufficient_without_study_execution():
    evaluator = _evaluator()
    assert evaluator.evaluate_rows([_row()]) is None


def test_book_simulation_needs_no_fills_table(tmp_path):
    evaluator = _evaluator()
    assert not (tmp_path / "fills.db").exists()
    with patch.object(evaluator.execution_realism, "protocol_clustered_lower_bound",
                      return_value=-0.01) as strict:
        assert evaluator.evaluate_rows([_row()]) == -0.01
    strict.assert_called_once_with([_row()], exact_m=100)


def test_unlabelled_non_fill_is_not_production_evidence():
    evaluator = _evaluator()
    row = _row()
    del row["fill_provenance"]
    with patch.object(evaluator.execution_realism, "protocol_clustered_lower_bound") as strict:
        assert evaluator.evaluate_rows([row]) is None
    strict.assert_not_called()


def test_depth_below_book_simulated_quantity_rejects():
    evaluator = _evaluator()
    row = _row()
    row["real_depth"] = row["quantity"] - 0.01
    with patch.object(evaluator.execution_realism, "protocol_clustered_lower_bound") as strict:
        assert evaluator.evaluate_rows([row]) is None
    strict.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("official_outcome", None),
    ("production_fee_dollars", float("nan")),
    ("real_depth", 0.0),
    ("venue_ts", float("nan")),
    ("order_arrival_ts", 99.0),
    ("outcome_provenance", "spot_proxy"),
])
def test_invalid_evidence_returns_none_before_strict_helper(field, value):
    evaluator = _evaluator()
    row = _row()
    row[field] = value
    with patch.object(evaluator.execution_realism, "protocol_clustered_lower_bound") as strict:
        assert evaluator.evaluate_rows([row]) is None
    strict.assert_not_called()
