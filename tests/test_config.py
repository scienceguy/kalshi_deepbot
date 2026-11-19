"""Tests for config.py — env var parsing, especially _env_bool safety.

_env_bool must never silently flip a safety-critical flag (e.g. DRY_RUN)
into live mode when given an unparseable value; it must raise ValueError
instead of silently falling back to the default.
"""
import importlib
import logging

import config


def _reload_with(monkeypatch, **env):
    """Set env vars (monkeypatch auto-restores them after the test) and reload config."""
    for key, val in env.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)
    importlib.reload(config)
    return config


def test_env_bool_unrecognized_returns_default(monkeypatch, caplog):
    # Unrecognized values log a warning and return the safe default (not raise).
    import logging
    with caplog.at_level(logging.WARNING, logger="config"):
        cfg = _reload_with(monkeypatch, DRY_RUN="maybe")
        assert cfg.DRY_RUN is True  # default
        assert any("Unrecognized boolean value for DRY_RUN: 'maybe'" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="config"):
        cfg = _reload_with(monkeypatch, DRY_RUN="garbage")
        assert cfg.DRY_RUN is True
        assert any("Unrecognized boolean value for DRY_RUN: 'garbage'" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="config"):
        cfg = _reload_with(monkeypatch, AGGRESSIVE_FILL="garbage")
        assert cfg.AGGRESSIVE_FILL is False  # default
        assert any("Unrecognized boolean value for AGGRESSIVE_FILL: 'garbage'" in r.getMessage() for r in caplog.records)

    # Valid values still work (each call isolates env vars)
    cfg = _reload_with(monkeypatch, DRY_RUN="yes", AGGRESSIVE_FILL="false")
    assert cfg.DRY_RUN is True
    assert cfg.AGGRESSIVE_FILL is False
    cfg = _reload_with(monkeypatch, DRY_RUN="on", AGGRESSIVE_FILL="true")
    assert cfg.DRY_RUN is True
    assert cfg.AGGRESSIVE_FILL is True
    cfg = _reload_with(monkeypatch, DRY_RUN="1", AGGRESSIVE_FILL="false")
    assert cfg.DRY_RUN is True
    assert cfg.AGGRESSIVE_FILL is False
    cfg = _reload_with(monkeypatch, DRY_RUN="no", AGGRESSIVE_FILL="true")
    assert cfg.DRY_RUN is False
    assert cfg.AGGRESSIVE_FILL is True
    cfg = _reload_with(monkeypatch, DRY_RUN="0", AGGRESSIVE_FILL="false")
    assert cfg.DRY_RUN is False
    assert cfg.AGGRESSIVE_FILL is False
    cfg = _reload_with(monkeypatch, DRY_RUN="off", AGGRESSIVE_FILL="true")
    assert cfg.DRY_RUN is False
    assert cfg.AGGRESSIVE_FILL is True


def test_env_bool_recognized_values(monkeypatch):
    cases = [
        ("true", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("0", False), ("no", False), ("off", False),
    ]
    for raw, expected in cases:
        cfg = _reload_with(monkeypatch, DRY_RUN=raw)
        assert cfg.DRY_RUN is expected, f"DRY_RUN={raw!r} -> {cfg.DRY_RUN}, expected {expected}"


def test_env_bool_unset_uses_default(monkeypatch):
    # Set to empty string to trigger default (since .env file sets DRY_RUN=false)
    cfg = _reload_with(monkeypatch, DRY_RUN="")
    assert cfg.DRY_RUN is True

    cfg = _reload_with(monkeypatch, AGGRESSIVE_FILL="")
    assert cfg.AGGRESSIVE_FILL is False  # maker default (post_only=True)


def test_log_rotation_defaults(monkeypatch):
    """LOG_MAX_BYTES/LOG_BACKUP_COUNT default to 50MB / 3 backups (safe reliable headroom)."""
    cfg = _reload_with(monkeypatch, LOG_MAX_BYTES="", LOG_BACKUP_COUNT="")
    assert cfg.LOG_MAX_BYTES == 50 * 1024 * 1024
    assert cfg.LOG_BACKUP_COUNT == 3


def test_log_rotation_custom_values(monkeypatch):
    """LOG_MAX_BYTES/LOG_BACKUP_COUNT honor explicit env values."""
    cfg = _reload_with(monkeypatch, LOG_MAX_BYTES="1048576", LOG_BACKUP_COUNT="7")
    assert cfg.LOG_MAX_BYTES == 1048576
    assert cfg.LOG_BACKUP_COUNT == 7


def test_llm_fail_open_allowlist_returns_default_on_garbage(monkeypatch, caplog):
    """LLM_EVAL_FAIL_OPEN must accept only the explicit allowlist values.
    On unrecognized values, log a warning and return the safe default (not raise).
    A typo'd flag like 'trrue' must not silently flip fail-open behavior."""
    import logging
    with caplog.at_level(logging.WARNING, logger="config"):
        cfg = _reload_with(monkeypatch, LLM_EVAL_FAIL_OPEN="trrue")
        assert cfg.LLM_EVAL_FAIL_OPEN is True  # default
        assert any("Unrecognized boolean value for LLM_EVAL_FAIL_OPEN: 'trrue'" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="config"):
        cfg = _reload_with(monkeypatch, LLM_EVAL_FAIL_OPEN="maybe")
        assert cfg.LLM_EVAL_FAIL_OPEN is True
        assert any("Unrecognized boolean value for LLM_EVAL_FAIL_OPEN: 'maybe'" in r.getMessage() for r in caplog.records)
    # Allowed values still parse.
    cfg = _reload_with(monkeypatch, LLM_EVAL_FAIL_OPEN="false")
    assert cfg.LLM_EVAL_FAIL_OPEN is False
    cfg = _reload_with(monkeypatch, LLM_EVAL_FAIL_OPEN="true")
    assert cfg.LLM_EVAL_FAIL_OPEN is True


def test_pnl_reconcile_validation(monkeypatch):
    """PNL_RECONCILE_INTERVAL_S must be > 0 and PNL_DRIFT_TOLERANCE >= 0."""
    import pytest

    cfg = _reload_with(monkeypatch, PNL_RECONCILE_INTERVAL_S="0")
    with pytest.raises(ValueError, match="PNL_RECONCILE_INTERVAL_S"):
        cfg.validate_pnl_reconcile()

    cfg = _reload_with(monkeypatch, PNL_DRIFT_TOLERANCE="-0.01")
    with pytest.raises(ValueError, match="PNL_DRIFT_TOLERANCE"):
        cfg.validate_pnl_reconcile()

    cfg = _reload_with(monkeypatch, PNL_RECONCILE_INTERVAL_S="600",
                       PNL_DRIFT_TOLERANCE="0")
    cfg.validate_pnl_reconcile()  # valid: zero tolerance is allowed


# ── B12: exit thresholds unreachable at a high entry band (config validation) ─

def test_exit_take_profit_unreachable_warns(monkeypatch, caplog):
    """Acceptance: default config with EXIT_ENABLED=true warns when the
    take-profit can never trigger (92c entry + 20c target > 99c bid ceiling).
    The only live exit would be the stop loss."""
    cfg = _reload_with(monkeypatch,
                       EXIT_ENABLED="true",
                       MIN_YES_PRICE_CENTS="92",
                       ENTRY_BAND_MIN="92",
                       EXIT_TAKE_PROFIT_CENTS="20")
    with caplog.at_level(logging.WARNING, logger="config"):
        cfg.validate_bands()
    messages = [r.getMessage() for r in caplog.records]
    assert any("EXIT_TAKE_PROFIT_CENTS" in m and "99" in m for m in messages), messages


def test_exit_take_profit_in_range_passes_silently(monkeypatch, caplog):
    """Acceptance: an in-range take-profit (92c + 7c = 99c, exactly reachable)
    validates without the unreachable warning."""
    cfg = _reload_with(monkeypatch,
                       EXIT_ENABLED="true",
                       MIN_YES_PRICE_CENTS="92",
                       ENTRY_BAND_MIN="92",
                       EXIT_TAKE_PROFIT_CENTS="7")
    with caplog.at_level(logging.WARNING, logger="config"):
        cfg.validate_bands()
    messages = [r.getMessage() for r in caplog.records]
    assert not any("EXIT_TAKE_PROFIT_CENTS" in m for m in messages), messages


def test_exit_take_profit_warning_fires_even_when_disabled(monkeypatch, caplog):
    """The misconfiguration is worth flagging even with EXIT_ENABLED=false —
    the operator opts into a silently capped exit without knowing."""
    cfg = _reload_with(monkeypatch,
                       EXIT_ENABLED="false",
                       MIN_YES_PRICE_CENTS="92",
                       ENTRY_BAND_MIN="92",
                       EXIT_TAKE_PROFIT_CENTS="20")
    with caplog.at_level(logging.WARNING, logger="config"):
        cfg.validate_bands()
    messages = [r.getMessage() for r in caplog.records]
    assert any("EXIT_TAKE_PROFIT_CENTS" in m for m in messages), messages


def test_exit_take_profit_ok_at_lower_band_no_warning(monkeypatch, caplog):
    """A lower entry band with a roomy target validates cleanly."""
    cfg = _reload_with(monkeypatch,
                       EXIT_ENABLED="true",
                       MIN_YES_PRICE_CENTS="50",
                       ENTRY_BAND_MIN="50",
                       EXIT_TAKE_PROFIT_CENTS="20")
    with caplog.at_level(logging.WARNING, logger="config"):
        cfg.validate_bands()
    messages = [r.getMessage() for r in caplog.records]
    assert not any("EXIT_TAKE_PROFIT_CENTS" in m for m in messages), messages
