"""Tests for config.py — env var parsing, especially _env_bool safety.

_env_bool must never silently flip a safety-critical flag (e.g. DRY_RUN)
into live mode when given an unparseable value; it must raise ValueError
instead of silently falling back to the default.
"""
import importlib

import config
import pytest


def _reload_with(monkeypatch, **env):
    """Set env vars (monkeypatch auto-restores them after the test) and reload config."""
    for key, val in env.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)
    importlib.reload(config)
    return config


def test_env_bool_unrecognized_raises(monkeypatch):
    # Unrecognized values must raise ValueError, NOT silently fall back to default.
    with pytest.raises(ValueError, match="Unrecognized boolean value"):
        _reload_with(monkeypatch, DRY_RUN="maybe")
    with pytest.raises(ValueError, match="Unrecognized boolean value"):
        _reload_with(monkeypatch, DRY_RUN="garbage")
    with pytest.raises(ValueError, match="Unrecognized boolean value"):
        _reload_with(monkeypatch, AGGRESSIVE_FILL="garbage")

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
    assert cfg.AGGRESSIVE_FILL is True  # final-minute taker default


def test_log_rotation_defaults(monkeypatch):
    """LOG_MAX_BYTES/LOG_BACKUP_COUNT default to 300MB / 3 backups (user-requested headroom)."""
    cfg = _reload_with(monkeypatch, LOG_MAX_BYTES="", LOG_BACKUP_COUNT="")
    assert cfg.LOG_MAX_BYTES == 300 * 1024 * 1024
    assert cfg.LOG_BACKUP_COUNT == 3


def test_log_rotation_custom_values(monkeypatch):
    """LOG_MAX_BYTES/LOG_BACKUP_COUNT honor explicit env values."""
    cfg = _reload_with(monkeypatch, LOG_MAX_BYTES="1048576", LOG_BACKUP_COUNT="7")
    assert cfg.LOG_MAX_BYTES == 1048576
    assert cfg.LOG_BACKUP_COUNT == 7


def test_llm_fail_open_allowlist_rejects_garbage(monkeypatch):
    """LLM_EVAL_FAIL_OPEN must accept only the explicit allowlist values and
    raise (never silently fall back) on an unrecognized value. A typo'd flag
    like 'trrue' must not silently flip fail-open behavior."""
    with pytest.raises(ValueError, match="Unrecognized boolean value"):
        _reload_with(monkeypatch, LLM_EVAL_FAIL_OPEN="trrue")
    with pytest.raises(ValueError, match="Unrecognized boolean value"):
        _reload_with(monkeypatch, LLM_EVAL_FAIL_OPEN="maybe")
    # Allowed values still parse.
    cfg = _reload_with(monkeypatch, LLM_EVAL_FAIL_OPEN="false")
    assert cfg.LLM_EVAL_FAIL_OPEN is False
    cfg = _reload_with(monkeypatch, LLM_EVAL_FAIL_OPEN="true")
    assert cfg.LLM_EVAL_FAIL_OPEN is True
