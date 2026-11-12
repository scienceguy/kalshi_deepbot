"""Single-instance lock must actually stay held for the process lifetime.

Regression: the PID write used `with os.fdopen(lock_fd, "w")`, which closed the
descriptor and released the flock immediately, so a second bot could start and
trade the same account. The lock no longer writes a PID at all, but the fd must
still stay open for the process lifetime.
"""
import fcntl
import os
import sys

import pytest

# Set sys.argv before importing trader to avoid argparse SystemExit
sys.argv = ["trader.py"]

import trader


def test_lock_stays_held_while_fd_open(tmp_path):
    path = tmp_path / ".lock"
    fd = trader.acquire_instance_lock(path)

    # A second acquisition attempt must fail while the first fd is open.
    fd2 = os.open(path, os.O_CREAT | os.O_WRONLY)
    try:
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            pass
        else:
            raise AssertionError("second flock succeeded — lock was not held")
    finally:
        os.close(fd2)
        os.close(fd)


def test_exhausted_retries_refuse_to_start(tmp_path, monkeypatch):
    """The safety promise: if the lock is held, the bot must not start.

    A held lock must make 5 attempts fail and then raise SystemExit, never fall
    through and run a second bot against the same account.
    """
    path = tmp_path / ".lock"
    holder = os.open(path, os.O_CREAT | os.O_WRONLY)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(trader.time, "sleep", lambda _s: None)  # no real 4s wait
    try:
        with pytest.raises(SystemExit):
            trader.acquire_instance_lock(path)
    finally:
        os.close(holder)
