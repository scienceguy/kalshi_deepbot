"""H13: tie the trader unit files to the release layout deploy.py produces.
Releases root default is built inside _deploy_release() (env knob
SUPERBOT_RELEASES_DIR), not a module attribute, so import if possible and
otherwise parse the source.
"""
import os
import re
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_PY = ROOT / "scripts" / "deploy.py"
UNITS = ("superbot.service", "superbot-dry.service")


def _deploy_module():
    try:
        spec = importlib.util.spec_from_file_location("h13_deploy", DEPLOY_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except ImportError:
        return None


def _releases_root() -> str:
    mod = _deploy_module()
    for attr in ("RELEASES_DIR", "SUPERBOT_RELEASES_DIR", "RELEASES", "_RELEASES_DIR"):
        val = getattr(mod, attr, None) if mod else None
        if val:
            return str(val)
    src = DEPLOY_PY.read_text()
    m = re.search(r'SUPERBOT_RELEASES_DIR",\s*"([^"]+)"', src)
    assert m, "deploy.py: SUPERBOT_RELEASES_DIR default not found"
    return m.group(1)


def _unit_text(unit):
    return (ROOT / "deploy" / unit).read_text()


def _directive(text, name):
    m = re.search(rf"^{re.escape(name)}=(\S+)", text, re.M)
    assert m, f"{name}= not found"
    return m.group(1)


def test_working_directory_equals_deploy_current_symlink():
    expected = os.path.join(_releases_root(), "current")
    for unit in UNITS:
        wd = _directive(_unit_text(unit), "WorkingDirectory")
        assert wd == expected, f"{unit}: WorkingDirectory={wd!r}, expected {expected!r}"


def test_working_directory_not_legacy_dev_tree():
    for unit in UNITS:
        wd = _directive(_unit_text(unit), "WorkingDirectory")
        assert wd != "/opt/superbot", f"{unit}: regression to pre-H13 layout"


def test_execstart_script_resolvable_relative_to_workingdir():
    for unit in UNITS:
        text = _unit_text(unit)
        wd = _directive(text, "WorkingDirectory")
        tokens = re.search(r"^ExecStart=(.+)$", text, re.M).group(1).split()
        script = next((t for t in tokens[1:] if t.endswith(".py")), None)
        assert script, f"{unit}: no .py script in ExecStart"
        assert not os.path.isabs(script), \
            f"{unit}: ExecStart script {script!r} absolute; must resolve against {wd}"
        assert not script.startswith("/opt/superbot/"), \
            f"{unit}: ExecStart script {script!r} hardcodes the dev tree"
        # joined path stays inside the release checkout
        assert os.path.normpath(os.path.join(wd, script)).startswith(os.path.normpath(wd))


def test_deploy_creates_current_symlink_under_releases_root():
    src = DEPLOY_PY.read_text()
    root_default = _releases_root()
    assert f'"{root_default}"' in src or root_default in src
    # _swap_current points releases/current at the new release dir
    assert re.search(r'releases\s*/\s*"current"', src), "deploy.py no longer swaps releases/current"
    assert "mkdir(parents=True, exist_ok=True)" in src, "deploy.py no longer creates the releases root"


def test_render_unit_rewrites_a_custom_releases_root():
    """A custom SUPERBOT_RELEASES_DIR must be rendered into the copied unit."""
    mod = _deploy_module()
    if mod is None or not hasattr(mod, "_render_unit"):
        # deploy.py imports leases/local_db; if that fails, check the source
        src = DEPLOY_PY.read_text()
        assert 'text.replace("/opt/superbot-releases", str(releases))' in src
        return
    unit = '[Service]\nWorkingDirectory=/opt/superbot-releases/current\n'
    out = mod._render_unit(unit, Path("/srv/superbot-releases"))
    assert out == '[Service]\nWorkingDirectory=/srv/superbot-releases/current\n'
