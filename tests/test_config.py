"""Tests for config.py — env var parsing, especially _env_bool safety.

_env_bool must never silently flip a safety-critical flag (e.g. DRY_RUN)
into live mode when given an unparseable value; it must fall back to the
declared default and log a warning instead.
"""
import importlib

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


def test_env_bool_unrecognized_defaults_safe(monkeypatch):
    # DRY_RUN=garbage must fall back to the safe default (True = dry-run),
    # NOT False (live). Previously returned False → silently enabled live trading.
    cfg = _reload_with(monkeypatch, DRY_RUN="maybe")
    assert cfg.DRY_RUN is True

    cfg = _reload_with(monkeypatch, DRY_RUN="yes")
    assert cfg.DRY_RUN is True
    cfg = _reload_with(monkeypatch, DRY_RUN="on")
    assert cfg.DRY_RUN is True

    cfg = _reload_with(monkeypatch, AGGRESSIVE_FILL="garbage")
    assert cfg.AGGRESSIVE_FILL is False


def test_env_bool_recognized_values(monkeypatch):
    cases = [
        ("true", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("0", False), ("no", False), ("off", False),
    ]
    for raw, expected in cases:
        cfg = _reload_with(monkeypatch, DRY_RUN=raw)
        assert cfg.DRY_RUN is expected, f"DRY_RUN={raw!r} -> {cfg.DRY_RUN}, expected {expected}"


def test_env_bool_unset_uses_default(monkeypatch):
    cfg = _reload_with(monkeypatch, DRY_RUN=None)
    assert cfg.DRY_RUN is True

    cfg = _reload_with(monkeypatch, AGGRESSIVE_FILL=None)
    assert cfg.AGGRESSIVE_FILL is False