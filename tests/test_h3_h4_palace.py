"""H3/H4: optional mempalace import and one shared palace directory.

H3: importing the trader must work with the mempalace package absent, using a
no-op bridge that reports degraded instead of raising.

H4: MEMPALACE_DIR / MEMPALACE_PALACE_PATH select the palace; a declared
MEMPALACE_EXPECTED_DIR that differs is reported as degraded; two identities
pointed at one directory see each other's events.

No ``importlib.reload`` here on purpose: reloading redefines
``MemPalaceBridge`` and would break ``from mempalace_bridge import
MemPalaceBridge`` in other test modules for the rest of the session.
"""

import subprocess
import sys
import time
from pathlib import Path

import pytest

import mempalace_bridge


def test_import_trader_without_mempalace():
    """H3: trader imports and the no-op bridge is used when mempalace is gone."""
    script = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'mempalace' or name.startswith('mempalace.'):\n"
        "            raise ImportError('blocked by test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "import trader\n"
        "import mempalace_bridge as mb\n"
        "assert mb._MEMPALACE_AVAILABLE is False\n"
        "assert 'not installed' in mb.degraded_reason()\n"
        "b = mb.get_default_bridge('test-identity')\n"
        "b.heartbeat(1)\n"
        "b.checkpoint({'probe': 1})\n"
        "b.stop(timeout=2)\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "OK" in proc.stdout


def test_palace_dir_from_env():
    """H4: MEMPALACE_DIR (or the CLI's MEMPALACE_PALACE_PATH) wins."""
    assert mempalace_bridge._palace_dir_from_env(
        {"MEMPALACE_DIR": "/shared/palace"}) == "/shared/palace"
    assert mempalace_bridge._palace_dir_from_env(
        {"MEMPALACE_PALACE_PATH": "/cli/palace"}) == "/cli/palace"
    assert mempalace_bridge._palace_dir_from_env({}).endswith(".mempalace")


def test_expected_dir_mismatch_is_degraded():
    """H4: a different palace than the declared one reports degraded."""
    assert "mismatch" in mempalace_bridge.degraded_reason(
        palace_dir="/tmp/here", expected_dir="/tmp/there")
    assert mempalace_bridge.degraded_reason(
        palace_dir="/tmp/here", expected_dir="/tmp/here") == ""
    assert mempalace_bridge.degraded_reason(
        palace_dir="/tmp/here", expected_dir="") == ""


def test_missing_library_is_degraded(monkeypatch):
    """H3: absent mempalace degrades the bridge instead of raising."""
    monkeypatch.setattr(mempalace_bridge, "_MEMPALACE_AVAILABLE", False)
    assert "not installed" in mempalace_bridge.degraded_reason()


@pytest.mark.skipif(
    not mempalace_bridge._MEMPALACE_AVAILABLE, reason="mempalace not installed"
)
def test_two_identities_share_one_palace(monkeypatch, tmp_path):
    """H4: A's event is visible to B's inbound subscriber on one directory."""
    db = tmp_path / "logstream.sqlite3"
    monkeypatch.setattr(mempalace_bridge, "_LOGSTREAM_DB", str(db))
    a = mempalace_bridge.MemPalaceBridge("ubuntu:test:a")
    b = mempalace_bridge.MemPalaceBridge("ubuntu:test:b")
    a.start()
    b.start()
    try:
        # Let B's subscriber register its wait cursor before A writes.
        time.sleep(1.0)
        a.status("working", {"probe": "h4"})
        deadline = time.time() + 15
        got = []
        while time.time() < deadline and not got:
            got = b.drain_tasks()
            if not got:
                time.sleep(0.25)
        assert got, "B never received A's event from the shared palace"
    finally:
        a.stop(timeout=5)
        b.stop(timeout=5)
