import os
import subprocess
import sys
import zipfile
from pathlib import Path


def test_wheel_contains_and_imports_log_sanitizer(tmp_path):
    root = Path(__file__).resolve().parents[1]
    wheelhouse = tmp_path / "wheelhouse"
    subprocess.run([
        sys.executable, "-m", "pip", "wheel", "--no-deps",
        "--no-build-isolation", "--wheel-dir", str(wheelhouse), str(root),
    ], check=True, capture_output=True, text=True, timeout=120)
    wheel = next(wheelhouse.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert "log_sanitizer.py" in archive.namelist()

    installed = tmp_path / "installed"
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--no-deps",
        "--target", str(installed), str(wheel),
    ], check=True, capture_output=True, text=True, timeout=120)
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(installed)!r}); "
        "from log_sanitizer import SanitizingFormatter; "
        "import logging; "
        "r=logging.LogRecord('x',20,'',1,'token=FAKE_SYNTHETIC',(),None); "
        "assert 'FAKE_SYNTHETIC' not in SanitizingFormatter('%(message)s').format(r)"
    )
    result = subprocess.run([
        sys.executable, "-I", "-c", code,
    ], cwd=tmp_path, env=dict(os.environ, PYTHONPATH=""), capture_output=True,
        text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
