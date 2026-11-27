"""Package metadata must install the runtime documented in requirements.txt."""
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


def test_pyproject_contains_requirements_runtime_dependencies():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    declared = set(project["dependencies"])

    required = set()
    for raw in (root / "requirements.txt").read_text().splitlines():
        if raw.startswith("# Test + pre-commit"):
            break
        requirement = raw.split("#", 1)[0].strip()
        if requirement:
            required.add(requirement)

    assert required
    assert required <= declared


def test_wheel_installs_both_traders_and_runtime_resources(tmp_path):
    root = Path(__file__).resolve().parents[1]
    wheelhouse = tmp_path / "wheelhouse"
    subprocess.run([
        sys.executable, "-m", "pip", "wheel", "--no-deps",
        "--no-build-isolation", "--wheel-dir", str(wheelhouse), str(root),
    ], check=True, capture_output=True, text=True, timeout=120)
    wheel = next(wheelhouse.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "cfb_monitor.py" not in names
    for module in ("trader", "poly_us_trader", "tick_bus", "sizing", "agent_identity"):
        assert f"{module}.py" in names
    assert "config/risk_shield.yaml" in names
    assert "migrations/001_evidence_tables.sql" in names
    assert "migrations/002_outbox_views.sql" in names
    assert "migrations/003_pnl_ledger_source.sql" in names
    assert "scripts/pm_us_protocol_evaluator.py" in names
    assert "config/pm_us_protocol_evaluator_v1.json" in names
    for module in ("execution_realism", "kalshi_ws", "historical_ingestion_catalog"):
        assert f"{module}.py" in names
    prohibited = ("__pycache__", ".pyc", ".db", ".db-wal", ".db-shm", ".log")
    assert not any(part in name for name in names for part in prohibited)
    assert not any(name.startswith(("legacy/", "build/", "dist/", "local_data/", "logs/"))
                   for name in names)

    installed = tmp_path / "installed"
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--no-deps",
        "--target", str(installed), str(wheel),
    ], check=True, capture_output=True, text=True, timeout=120)
    env = dict(os.environ, HOME=str(tmp_path / "home"),
               SUPERBOT_DATA_DIR=str(tmp_path / "data"), PYTHONPATH="")
    for module in ("trader", "poly_us_trader"):
        entry_smoke = (
            "import runpy, sys; "
            f"sys.path.insert(0, {str(installed)!r}); "
            f"sys.argv=[{module!r}, '--help']; "
            f"runpy.run_module({module!r}, run_name='__main__')"
        )
        result = subprocess.run([
            sys.executable, "-I", "-c", entry_smoke,
        ], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr[-2000:]
        assert "usage" in result.stdout.lower()
    resource_check = (
        "import sys; "
        f"sys.path.insert(0, {str(installed)!r}); "
        "import local_db, risk; "
        "assert risk._RISK_SHIELD; "
        f"db=local_db.LocalDB(db_path={str(tmp_path / 'installed.db')!r}, auto_sync=False); "
        "rows=db._get_conn().execute('SELECT version FROM schema_migrations ORDER BY version').fetchall(); "
        "assert [r[0] for r in rows] == [1, 2, 3]"
    )
    result = subprocess.run([
        sys.executable, "-I", "-c", resource_check,
    ], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    depth_check = (
        "import runpy, sys; "
        f"sys.path.insert(0, {str(installed)!r}); "
        "import execution_realism, kalshi_ws, historical_ingestion_catalog; "
        f"module=runpy.run_path({str(installed / 'scripts' / 'pm_us_protocol_evaluator.py')!r}); "
        "assert module['_load_manifest']() is not None"
    )
    result = subprocess.run([
        sys.executable, "-I", "-c", depth_check,
    ], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]


def test_pytest_randomly_is_a_test_dependency():
    root = Path(__file__).resolve().parents[1]
    test_requirements = (root / "requirements.txt").read_text().split(
        "# Test + pre-commit", 1)[1]
    assert "pytest-randomly" in test_requirements


def test_pytest_is_only_an_optional_test_dependency():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    assert "pytest" not in project["dependencies"]
    assert "pytest" in project["optional-dependencies"]["test"]
    assert {"pytest-randomly", "pyflakes>=3.1", "mutmut==3.8.1"} <= set(
        project["optional-dependencies"]["test"])


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
