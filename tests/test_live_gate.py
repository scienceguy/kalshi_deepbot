"""Behaviour of the live-arming gate (config.validate_dry_run)."""
import os

import config


def _set(monkeypatch, dry_run, confirm):
    monkeypatch.setattr(config, "DRY_RUN", dry_run, raising=False)
    if confirm is None:
        monkeypatch.delenv("KALSHI_CONFIRM_LIVE", raising=False)
    else:
        monkeypatch.setenv("KALSHI_CONFIRM_LIVE", confirm)


def test_dry_run_without_live_flag_ok(monkeypatch):
    _set(monkeypatch, True, None)
    assert config.validate_dry_run(live_explicit=False) is True


def test_dry_run_with_live_flag_but_no_confirm_blocked(monkeypatch):
    _set(monkeypatch, True, None)
    assert config.validate_dry_run(live_explicit=True) is False


def test_dry_run_with_live_flag_and_confirm_ok(monkeypatch):
    _set(monkeypatch, True, "true")
    assert config.validate_dry_run(live_explicit=True) is True


def test_live_env_without_flag_blocked(monkeypatch):
    _set(monkeypatch, False, "true")
    assert config.validate_dry_run(live_explicit=False) is False


def test_live_env_with_flag_and_confirm_ok(monkeypatch):
    _set(monkeypatch, False, "true")
    assert config.validate_dry_run(live_explicit=True) is True


def test_confirm_case_insensitive(monkeypatch):
    _set(monkeypatch, True, "TRUE")
    assert config.validate_dry_run(live_explicit=True) is True


def test_confirm_wrong_value_blocked(monkeypatch):
    for bad in ("", "1", "yes", "false"):
        _set(monkeypatch, True, bad)
        assert config.validate_dry_run(live_explicit=True) is False, bad
