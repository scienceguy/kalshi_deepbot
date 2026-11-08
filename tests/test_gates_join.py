"""Tests for scripts/gates_join.py — gate-decision vs markout cohort join."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gates_join.py"


@pytest.fixture(scope="module")
def gj():
    spec = importlib.util.spec_from_file_location("gates_join", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, records) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def test_load_jsonl_skips_blank_and_corrupt(gj, tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text('{"a": 1}\n\nnot-json\n{"a": 2}\n')
    assert gj.load_jsonl(path) == [{"a": 1}, {"a": 2}]
    assert gj.load_jsonl(tmp_path / "missing.jsonl") == []


def test_join_uses_latest_gate_per_candidate(gj):
    gates = [
        {"ticker": "T1", "side": "yes", "decision": "would_block"},
        {"ticker": "T1", "side": "yes", "decision": "allow"},
        {"ticker": "T2", "side": "no", "decision": "would_block"},
    ]
    markouts = [{"ticker": "T1", "side": "yes", "markout_cents": 10}]
    rows = {(r["gate"]["ticker"], r["gate"]["side"]): r
            for r in gj.join(gates, markouts)}
    assert rows[("T1", "yes")]["gate"]["decision"] == "allow"
    assert len(rows[("T1", "yes")]["fills"]) == 1
    assert rows[("T2", "no")]["fills"] == []


def test_summarize_cohorts_and_fill_rate(gj):
    rows = [
        {"gate": {"decision": "allow"}, "fills": [{"markout_cents": 10},
                                                  {"markout_cents": 0}]},
        {"gate": {"decision": "would_block"}, "fills": [{"markout_cents": -5}]},
        {"gate": {"decision": "would_block"}, "fills": []},
    ]
    cohorts = gj.summarize(rows)
    allow = cohorts["allow"]
    blocked = cohorts["would_block"]
    assert allow["candidates"] == 1 and allow["fills"] == 2
    assert allow["mean_markout_cents"] == pytest.approx(5.0)
    assert allow["median_markout_cents"] == pytest.approx(5.0)
    assert allow["win_rate"] == pytest.approx(0.5)
    assert allow["fill_rate"] == pytest.approx(2.0)  # two fills / one candidate
    assert blocked["candidates"] == 2 and blocked["fills"] == 1
    assert blocked["fill_rate"] == pytest.approx(0.5)
    assert blocked["mean_markout_cents"] == pytest.approx(-5.0)


def test_main_prints_report(gj, tmp_path, capsys):
    gates = _write(tmp_path / "g.jsonl", [
        {"ticker": "T1", "side": "yes", "decision": "allow"},
        {"ticker": "T2", "side": "no", "decision": "would_block"},
    ])
    markout = _write(tmp_path / "m.jsonl", [
        {"ticker": "T1", "side": "yes", "markout_cents": 4},
    ])
    assert gj.main(["--gates", str(gates), "--markout", str(markout)]) == 0
    out = capsys.readouterr().out
    assert "would_block" in out and "allow" in out
    assert "T1" not in out  # aggregate report, not per-candidate spam
