"""Single-instance lock must actually stay held for the process lifetime.

Regression: the PID write used `with os.fdopen(lock_fd, "w")`, which closed the
descriptor and released the flock immediately, so a second bot could start and
trade the same account.
"""
import fcntl
import os
import sys

# Set sys.argv before importing trader to avoid argparse SystemExit
sys.argv = ["trader.py"]

import trader


def test_lock_stays_held_after_pid_write(tmp_path):
    path = tmp_path / ".lock"
    fd = trader.acquire_instance_lock(path)

    # The PID was written without closing the fd.
    with open(path) as f:
        assert f.read() == str(os.getpid())

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


def test_pid_rewrite_does_not_leave_stale_digits(tmp_path):
    path = tmp_path / ".lock"
    path.write_text("9999999")  # longer than any future PID text
    fd = trader.acquire_instance_lock(path)
    try:
        with open(path) as f:
            assert f.read() == str(os.getpid())
    finally:
        os.close(fd)
