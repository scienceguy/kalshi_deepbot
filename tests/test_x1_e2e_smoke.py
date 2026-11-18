"""X1 — smoke test wrapping scripts/e2e_smoke.py.

Runs the importable run_all() against an isolated temporary LocalDB and
asserts every pillar passes (a SKIP sub-check still counts as ok). Fast,
no n8n, no network beyond loopback.
"""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import e2e_smoke


def test_all_pillars_pass():
    results = e2e_smoke.run_all()
    assert results, "run_all() returned no results"
    failures = [(p, d) for p, ok, d in results if not ok]
    assert not failures, "pillars failed: %s" % "; ".join(
        f"{p}: {d}" for p, d in failures)


def test_isolation_survives_hostile_env(tmp_path, monkeypatch):
    """A shell exporting SUPERBOT_* must not redirect the smoke's DB writes."""
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    decoy_db = decoy_dir / "decoy.db"
    monkeypatch.setenv("SUPERBOT_DATA_DIR", str(decoy_dir))
    monkeypatch.setenv("SUPERBOT_LOCAL_DB_PATH", str(decoy_db))

    # Re-executing the module top must force-set (not setdefault) the env.
    importlib.reload(e2e_smoke)
    assert not decoy_db.exists()
    assert list(decoy_dir.iterdir()) == []

    results = e2e_smoke.run_all()
    assert results, "run_all() returned no results"
    failures = [(p, d) for p, ok, d in results if not ok]
    assert not failures, "pillars failed: %s" % "; ".join(
        f"{p}: {d}" for p, d in failures)
    # Nothing was created at the decoy path by the reload or by run_all().
    assert not decoy_db.exists()
    assert list(decoy_dir.iterdir()) == []


REPO = Path(__file__).resolve().parent.parent


def test_smoke_subprocess_ignores_hostile_env(tmp_path):
    """scripts/e2e_smoke.py in a subprocess must force-point the env at a temp
    dir before local_db resolves its path — a hostile export must not win."""
    import os
    import subprocess

    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    decoy_db = decoy_dir / "decoy.db"
    env = {**os.environ,
           "SUPERBOT_DATA_DIR": str(decoy_dir),
           "SUPERBOT_LOCAL_DB_PATH": str(decoy_db)}

    proc = subprocess.run([sys.executable, "scripts/e2e_smoke.py"],
                          cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"smoke exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    assert "SUMMARY: 5/5 pillars passed" in proc.stdout
    assert list(decoy_dir.glob("*.db")) == []
    assert not decoy_db.exists()


def test_import_time_force_set_wins_over_hostile_env(tmp_path):
    """Importing the module must overwrite an exported SUPERBOT_* value."""
    import os
    import subprocess

    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    decoy_db = decoy_dir / "decoy.db"
    env = {**os.environ,
           "SUPERBOT_DATA_DIR": str(decoy_dir),
           "SUPERBOT_LOCAL_DB_PATH": str(decoy_db)}

    snippet = (
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('e2e_smoke_sub', 'scripts/e2e_smoke.py')\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "print(m.os.environ['SUPERBOT_LOCAL_DB_PATH'])\n"
        "print(m.os.environ['SUPERBOT_DATA_DIR'])\n"
    )
    proc = subprocess.run([sys.executable, "-c", snippet],
                          cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"import exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    assert len(lines) >= 2, f"expected 2 env lines, got: {proc.stdout!r}"
    assert str(decoy_dir) not in lines[0]
    assert str(decoy_dir) not in lines[1]
