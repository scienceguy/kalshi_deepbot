"""B15: one instance lock per trader regardless of mode (.lock_kalshi).

A dry and a live process used to run side by side, sharing the settlement
queue and state files. The lock name is now mode-independent, and a second
instance in either mode exits with a clear error while the first holds it.
"""
import sys
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.argv = ["trader.py"]

import trader


def test_lock_name_is_mode_independent():
    lock_name = trader.INSTANCE_LOCK_NAME  # AttributeError before B15
    assert lock_name == ".lock_kalshi"
    # Neither legacy per-mode name may remain in the source.
    src = Path(trader.__file__).read_text()
    assert ".lock_live" not in src
    assert ".lock_dry" not in src


def test_second_instance_exits_with_clear_error(tmp_path, monkeypatch):
    """Acceptance: a second instance (either mode) cannot take the lock while
    the first runs -- acquire raises SystemExit, never proceeds unlocked."""
    monkeypatch.setattr(trader.time, "sleep", lambda s: None)
    lock_path = tmp_path / ".lock_kalshi"
    fd1 = trader.acquire_instance_lock(lock_path)
    try:
        with pytest.raises(SystemExit) as exc:
            trader.acquire_instance_lock(lock_path)
        assert exc.value.code == 1
    finally:
        os.close(fd1)


def test_main_dry_once_acquires_lock_before_running(tmp_path, monkeypatch):
    """Dry --once replays and writes state, so it must use the same lock."""
    args = SimpleNamespace(
        status=False,
        sync_status=False,
        live=False,
        once=True,
        dry=True,
    )
    events = []
    lock_fd = os.open(tmp_path / ".lock_kalshi", os.O_CREAT | os.O_WRONLY)

    monkeypatch.setattr(trader, "parse_args", lambda: args)
    monkeypatch.setattr(trader, "_configure_logging", lambda: None)
    monkeypatch.setattr(trader, "get_state", lambda: None)
    monkeypatch.setattr(trader.se, "_load_bid_hist", lambda: None)
    monkeypatch.setattr(trader.config, "validate_all", lambda: None)
    monkeypatch.setattr(trader.config, "DRY_RUN", True)
    monkeypatch.setattr(trader.config, "SUPERBOT_DATA_DIR", tmp_path)

    def fake_acquire(path):
        events.append(("lock", path))
        return lock_fd

    monkeypatch.setattr(trader, "acquire_instance_lock", fake_acquire)
    monkeypatch.setattr(
        trader, "_run_loop", lambda actual: events.append(("run", actual))
    )

    trader.main()

    assert events == [
        ("lock", tmp_path / trader.INSTANCE_LOCK_NAME),
        ("run", args),
    ]
    with pytest.raises(OSError):
        os.fstat(lock_fd)
