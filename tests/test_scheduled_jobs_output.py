import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import scheduled_jobs


def test_large_child_output_stays_bounded_and_preserves_tails(monkeypatch):
    def no_buffered_run(*args, **kwargs):
        raise AssertionError("capture_output=True buffering must not be used")

    monkeypatch.setattr(scheduled_jobs.subprocess, "run", no_buffered_run)
    script = (
        "import json,sys;"
        f"sys.stdout.write(' ' * {scheduled_jobs.STDOUT_TAIL_BYTES * 3});"
        "sys.stdout.write(json.dumps({'alert': False}));"
        "sys.stdout.flush();"
        f"sys.stderr.write('e' * {scheduled_jobs.STDERR_TAIL_BYTES * 3} + 'stderr-tail');"
        "sys.stderr.flush(); sys.exit(3)"
    )

    result = scheduled_jobs.run_job(["-c", script], timeout=5.0)

    assert result["alert"] is True
    assert len(result["stdout"].encode()) <= 800
    assert len(result["stderr"].encode()) <= 800
    assert result["error"] == "-c exited 3"
    assert result["stderr"].endswith("stderr-tail")


def test_timeout_returns_alert_without_running_child_to_completion():
    result = scheduled_jobs.run_job(
        ["-c", "import time; time.sleep(10)"], timeout=0.05)

    assert result["alert"] is True
    assert "timed out" in result["error"]


def test_timeout_kills_grandchild_and_returns_within_deadline(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess,sys;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)']);"
        f"open({str(pid_file)!r},'w').write(str(p.pid));p.wait()"
    )
    results = []

    def run():
        results.append(scheduled_jobs.run_job(["-c", script], timeout=0.05))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(2.0)
    try:
        assert not worker.is_alive(), "grandchild kept pipe readers blocked after timeout"
        assert results[0]["alert"] is True
        assert "timed out" in results[0]["error"]
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except ProcessLookupError:
                pass
        worker.join(6.0)
