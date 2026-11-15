"""B9: LLM eval payload hygiene, fail-open streak alerting, calibration ledger.

- The prompt sent to providers carries no absolute balances and no
  mempalace checkpoint (other agents' inbox content used to ride along).
- Free-text fields are length-limited and stripped of control characters.
- The block log line prints the min_confidence ARGUMENT, not the config
  global (the two diverge when a caller tightens the threshold).
- N consecutive fail-open events fire the alert hook exactly once per streak.
- llm_prob/hold_conf are recorded with settlement outcomes and the Brier
  report only claims skill when the ledger beats chance.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.argv = ["trader.py"]  # trader.py parses argv at import time

import config
import llm_eval


@pytest.fixture(autouse=True)
def _defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_EVAL_FAIL_OPEN", True)
    monkeypatch.setattr(config, "LLM_FAIL_OPEN_ALERT_STREAK", 3)
    monkeypatch.setattr(config, "LLM_CALIBRATION_FILE",
                        str(tmp_path / "llm_calibration.jsonl"))
    llm_eval._FAIL_OPEN_STREAK = 0
    llm_eval.FAIL_OPEN_ALERT_HOOK = None
    yield
    llm_eval._FAIL_OPEN_STREAK = 0
    llm_eval.FAIL_OPEN_ALERT_HOOK = None


def _capture_evaluate(monkeypatch, signal_info):
    """Run evaluate_trade with a fake layer; return (result, user_prompt_text)."""
    captured = {}

    def fake_call_layer(layer, messages, deadline=None):
        captured["messages"] = messages
        return {"probability_of_net_profit": 0.9,
                "confidence_hold_to_expiry": 0.8,
                "reasoning": "ok", "layer": layer["name"], "model": "fake"}

    with patch.object(llm_eval, "_call_layer", side_effect=fake_call_layer):
        result = llm_eval.evaluate_trade(signal_info)
    user_prompt = captured["messages"][-1]["content"]
    return result, user_prompt


# ── payload privacy ────────────────────────────────────────────────────

def test_payload_has_no_balance_or_checkpoint_keys(monkeypatch):
    """Acceptance: the payload to a fake provider has no balance/checkpoint keys."""
    info = {
        "ticker": "KXBTC15M-26NOV15-45", "side": "yes", "entry_price": 95,
        "mid_side": 94, "spot_price": 95000.0, "strike_price": 95000.0,
        "spot_diff": 0.0, "minutes_left": 6, "signal_reason": "momentum",
        "contracts": 10, "dollar_risk": 9.5, "skew": 3,
        "cash": 12345.67, "exposure": 100.0, "total_value": 12000.0,
        "mempalace_checkpoint": {"secret": "other-agent inbox content"},
    }
    _, user_prompt = _capture_evaluate(monkeypatch, info)
    payload = json.loads(user_prompt)
    for banned in ("cash", "exposure", "total_value", "mempalace_checkpoint"):
        assert banned not in payload, f"payload leaked {banned}"
    assert payload["ticker"] == info["ticker"]


def test_reason_truncated_and_decontrolled(monkeypatch):
    """Acceptance: a 10,000-character reason is truncated; control chars gone."""
    reason = "x" * 10000 + "\x00\x07\r\n\t"
    info = {"ticker": "T", "side": "yes", "signal_reason": reason}
    _, user_prompt = _capture_evaluate(monkeypatch, info)
    payload = json.loads(user_prompt)
    assert len(payload["signal_reason"]) <= 2000
    assert "\x00" not in payload["signal_reason"]
    assert "\x07" not in payload["signal_reason"]


# ── fail-open streak alert ─────────────────────────────────────────────

def test_fail_open_streak_triggers_alert_hook(monkeypatch):
    """Acceptance: the fail-open streak triggers the alert hook."""
    alerts = []
    monkeypatch.setattr(llm_eval, "FAIL_OPEN_ALERT_HOOK",
                        lambda streak: alerts.append(streak))
    with patch.object(llm_eval, "evaluate_trade", return_value=None):
        for _ in range(3):
            llm_eval.should_block_trade({"ticker": "T"})
    assert alerts == [3]


def test_streak_resets_after_successful_eval(monkeypatch):
    alerts = []
    monkeypatch.setattr(llm_eval, "FAIL_OPEN_ALERT_HOOK",
                        lambda streak: alerts.append(streak))
    ok = {"probability_of_net_profit": 0.9, "confidence_hold_to_expiry": 0.8,
          "layer": "fake", "model": "fake"}
    with patch.object(llm_eval, "evaluate_trade", side_effect=[None, None, ok, None, None, None]):
        for _ in range(6):
            llm_eval.should_block_trade({"ticker": "T"})
    assert alerts == [3]  # the success reset the count; second streak never reached 3


# ── block log line ─────────────────────────────────────────────────────

def test_block_log_prints_min_confidence_argument(monkeypatch, caplog):
    """The block log line prints the argument used, not the config global."""
    import logging
    monkeypatch.setattr(config, "LLM_EVAL_MIN_CONFIDENCE", 0.45)
    eval_result = {"probability_of_net_profit": 0.9,
                   "confidence_hold_to_expiry": 0.10, "layer": "fake",
                   "model": "fake", "reasoning": ""}
    with caplog.at_level(logging.INFO):
        with patch.object(llm_eval, "evaluate_trade", return_value=eval_result):
            llm_eval.should_block_trade({"ticker": "T"},
                                        min_probability=0.65, min_confidence=0.50)
    assert "min_conf=0.50" in caplog.text
    assert "0.45" not in caplog.text


# ── calibration ledger ─────────────────────────────────────────────────

def test_no_ledger_means_no_confidence_override():
    """Without recorded outcomes the report does not beat chance -> override off."""
    assert llm_eval.calibration_beats_chance() is False


def test_record_outcome_feeds_brier_report():
    for i in range(30):
        won = i % 2 == 0
        llm_eval.record_outcome(f"T{i}", 0.9 if won else 0.1, 0.9, won)
    report = llm_eval.calibration_report()
    assert report is not None
    assert report["n"] == 30
    assert report["brier"] < 0.1          # confident + correct -> far under 0.25
    assert llm_eval.calibration_beats_chance() is True


def test_calibration_file_uses_config_path(_defaults, tmp_path):
    p = Path(config.LLM_CALIBRATION_FILE)
    llm_eval.record_outcome("T", 0.7, 0.7, True)
    assert p.exists()
    row = json.loads(p.read_text().splitlines()[0])
    assert row["llm_prob"] == 0.7 and row["outcome_won"] is True
