"""Test the /tmp-not-writable system constraint.

On this termux/proot device, /tmp is a tmpfs owned by shell:shell mode 0771 and
the app user (u0_a718) gets only ``--x`` (traverse) permission — no read, no
write. Any attempt to create or modify a file under /tmp fails with
``Permission denied``.

Temp files must use $TMPDIR (/data/data/com.termux/files/home/tmp) or
/data/data/com.termux/files/usr/tmp/opencode instead.

This test documents and enforces the constraint: if the environment ever
changes such that /tmp becomes writable, this test will surface it rather than
silently allowing code to depend on /tmp.
"""
import os
import stat
import tempfile
from pathlib import Path

TMP = Path("/tmp")
TMPDIR = Path(os.environ.get("TMPDIR", "/data/data/com.termux/files/home/tmp"))
OPENCODE_TMP = Path("/data/data/com.termux/files/usr/tmp/opencode")


def _can_write(path: Path) -> bool:
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(path))
        os.close(fd)
        os.remove(tmp_name)
        return True
    except OSError:
        return False


def test_tmp_not_writable():
    """/tmp must NOT be writable."""
    assert not _can_write(TMP), "/tmp should not be writable"
    # Confirm the permission bits explain why: not writable by "other".
    mode = stat.S_IMODE(TMP.stat().st_mode)
    assert not (mode & stat.S_IWOTH), f"/tmp unexpectedly writable by other: {oct(mode)}"


def test_tmpdir_writable():
    """"$TMPDIR must be writable (the approved alternative)."""
    assert _can_write(TMPDIR), f"$TMPDIR {TMPDIR} should be writable"


def test_opencode_tmp_writable():
    """/data/data/com.termux/files/usr/tmp/opencode must be writable."""
    assert _can_write(OPENCODE_TMP), f"{OPENCODE_TMP} should be writable"
