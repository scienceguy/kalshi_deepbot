"""V1: release-directory deploys (AGENT_TASKS_v7.md §V1).

Acceptance (all hermetic — temp dirs, fake systemctl, in-process git):
  * a failing smoke test leaves `current` unchanged
  * a deploy where the heartbeat never appears rolls back
  * uncommitted changes in the dev tree never reach a release
  * (happy path) releases/<sha> is created, `current` swapped, unit installed,
    heartbeat fresh after restart
  * a deploy refuses without the C3 deploy lease
"""

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_deploy():
    spec = importlib.util.spec_from_file_location(
        "deploy_v1", REPO / "scripts" / "deploy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Small dev repo: two commits (sha1, sha2) + deployment assets."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "marker.txt").write_text("base\n")
    (repo / "deploy").mkdir()
    (repo / "deploy" / "poly-us-dry.service").write_text(
        "[Unit]\nDescription=test\n[Service]\nType=notify\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    sha1 = _git(repo, "rev-parse", "HEAD")
    (repo / "marker.txt").write_text("v2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "v2")
    sha2 = _git(repo, "rev-parse", "HEAD")
    # Uncommitted changes — must never reach a release.
    (repo / "marker.txt").write_text("DIRTY UNCOMMITTED\n")
    (repo / "untracked_marker.txt").write_text("DIRTY UNTRACKED\n")
    return repo, sha1, sha2


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    """Fake systemctl + env pointing every deploy path into tmp dirs."""
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    ctl_log = tmp_path / "systemctl.log"
    ctl = tmp_path / "systemctl"
    ctl.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sqlite3, sys\n"
        "from datetime import datetime, timezone\n"
        "log = os.environ.get('SUPERBOT_SYSTEMCTL_LOG')\n"
        "if log:\n"
        "    with open(log, 'a') as f:\n"
        "        f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['restart'] and os.environ.get('SUPERBOT_FAKE_HEARTBEAT') == '1':\n"
        "    db = os.environ['SUPERBOT_HEARTBEAT_DB']\n"
        "    key = os.environ.get('SUPERBOT_HEARTBEAT_KEY', 'reported_state:poly_us')\n"
        "    ts = datetime.now(timezone.utc).isoformat()\n"
        "    conn = sqlite3.connect(db)\n"
        "    conn.execute('CREATE TABLE IF NOT EXISTS bot_state "
        "(key TEXT PRIMARY KEY, value_json TEXT, ts TEXT, synced INTEGER)')\n"
        "    payload = json.dumps({'state': 'trading', 'ts': ts})\n"
        "    conn.execute('INSERT OR REPLACE INTO bot_state (key, value_json, ts, synced) "
        "VALUES (?, ?, ?, 0)', (key, payload, ts))\n"
        "    conn.commit()\n"
        "    conn.close()\n"
        "sys.exit(0)\n")
    ctl.chmod(0o755)
    env_file = tmp_path / "shared.env"
    env_file.write_text("DRY_RUN=true\n")
    data_dir = tmp_path / "shared_data"
    data_dir.mkdir()
    env = {
        "SUPERBOT_RELEASES_DIR": str(tmp_path / "releases"),
        "SUPERBOT_DEPLOY_REPO": None,  # filled per test
        "SUPERBOT_DEPLOY_UNIT": "poly-us-dry.service",
        "SUPERBOT_UNIT_DIR": str(unit_dir),
        "SUPERBOT_SYSTEMCTL": str(ctl),
        "SUPERBOT_SYSTEMCTL_LOG": str(ctl_log),
        "SUPERBOT_ENV_SRC": str(env_file),
        "SUPERBOT_DATA_SRC": str(data_dir),
        "SUPERBOT_LEASES_DB": str(tmp_path / "leases.sqlite3"),
        "SUPERBOT_HEARTBEAT_DB": str(tmp_path / "hb.db"),
        "SUPERBOT_HEARTBEAT_KEY": "reported_state:poly_us",
        "SUPERBOT_HEARTBEAT_TIMEOUT": "3",
        "SUPERBOT_HEARTBEAT_POLL": "0.2",
        "SUPERBOT_VERIFY_CMD": "true",
        "SUPERBOT_SMOKE_CMD": "true",
        "SUPERBOT_FAKE_HEARTBEAT": "1",
    }
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    return {"unit_dir": unit_dir, "ctl_log": ctl_log, "ctl": ctl, "env": env}


def _ctl_calls(fakes):
    log = fakes["ctl_log"]
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _deploy(mod, monkeypatch, sha, *extra):
    monkeypatch.setattr(mod, "_announce", lambda *a, **k: None)  # keep logstream clean
    return mod.main(["--by", "ubuntu:opencode:kalshi_deepbot", "--release", sha, *extra])


def test_happy_path_deploys_and_reports_healthy(repo, fakes, monkeypatch, tmp_path):
    repo, sha1, _ = repo
    monkeypatch.setenv("SUPERBOT_DEPLOY_REPO", str(repo))
    mod = _load_deploy()
    rc = _deploy(mod, monkeypatch, sha1)
    assert rc == 0
    current = Path(fakes["env"]["SUPERBOT_RELEASES_DIR"]) / "current"
    assert Path(os.readlink(current)) == Path(fakes["env"]["SUPERBOT_RELEASES_DIR"]) / sha1
    release = Path(fakes["env"]["SUPERBOT_RELEASES_DIR"]) / sha1
    assert (release / "marker.txt").read_text() == "base\n"
    assert (release / ".env").is_symlink() and os.readlink(release / ".env") == fakes["env"]["SUPERBOT_ENV_SRC"]
    assert (release / "local_data").is_symlink()
    unit_copy = fakes["unit_dir"] / "poly-us-dry.service"
    assert unit_copy.exists()
    calls = _ctl_calls(fakes)
    assert any(c[0] == "daemon-reload" for c in calls)
    assert [c[0] for c in calls].count("restart") == 1


def test_failing_smoke_leaves_current_unchanged(repo, fakes, monkeypatch, tmp_path):
    repo, sha1, sha2 = repo
    monkeypatch.setenv("SUPERBOT_DEPLOY_REPO", str(repo))
    mod = _load_deploy()
    assert _deploy(mod, monkeypatch, sha1) == 0
    monkeypatch.setenv("SUPERBOT_SMOKE_CMD", "false")
    before = os.readlink(Path(fakes["env"]["SUPERBOT_RELEASES_DIR"]) / "current")
    assert _deploy(mod, monkeypatch, sha2) == 5  # smoke failed
    assert os.readlink(Path(fakes["env"]["SUPERBOT_RELEASES_DIR"]) / "current") == before
    restarts = [c for c in _ctl_calls(fakes) if c[0] == "restart"]
    assert len(restarts) == 1  # only the first (good) deploy restarted


def test_heartbeat_never_appears_rolls_back(repo, fakes, monkeypatch, tmp_path):
    repo, sha1, sha2 = repo
    monkeypatch.setenv("SUPERBOT_DEPLOY_REPO", str(repo))
    mod = _load_deploy()
    assert _deploy(mod, monkeypatch, sha1) == 0  # heartbeat row written by fake
    monkeypatch.setenv("SUPERBOT_FAKE_HEARTBEAT", "0")  # trader comes up but never reports
    assert _deploy(mod, monkeypatch, sha2) == 3
    current = Path(fakes["env"]["SUPERBOT_RELEASES_DIR"]) / "current"
    assert Path(os.readlink(current)) == Path(fakes["env"]["SUPERBOT_RELEASES_DIR"]) / sha1
    restarts = [c for c in _ctl_calls(fakes) if c[0] == "restart"]
    assert len(restarts) == 3  # sha1 deploy + failed sha2 deploy + rollback restart


def test_uncommitted_dev_tree_changes_never_ship(repo, fakes, monkeypatch, tmp_path):
    repo, sha1, _ = repo
    monkeypatch.setenv("SUPERBOT_DEPLOY_REPO", str(repo))
    mod = _load_deploy()
    assert _deploy(mod, monkeypatch, sha1) == 0
    release = Path(fakes["env"]["SUPERBOT_RELEASES_DIR"]) / sha1
    assert (release / "marker.txt").read_text() == "base\n"  # not "DIRTY UNCOMMITTED"
    assert not (release / "untracked_marker.txt").exists()
    assert _git(release, "rev-parse", "HEAD") == sha1


def test_deploy_refuses_without_lease(repo, fakes, monkeypatch, tmp_path):
    import leases
    repo, sha1, _ = repo
    monkeypatch.setenv("SUPERBOT_DEPLOY_REPO", str(repo))
    import time
    leases.acquire("deploy", "agent-a", ttl_s=60.0, now=time.time())
    mod = _load_deploy()
    announced = []
    monkeypatch.setattr(mod, "_announce", lambda *a, **k: announced.append(a))
    rc = mod.main(["--by", "agent-b", "--release", sha1])
    assert rc == 2
    assert announced == []
    releases = Path(fakes["env"]["SUPERBOT_RELEASES_DIR"])
    assert not releases.exists() or not (releases / sha1).exists()


def test_release_of_unknown_sha_refused(repo, fakes, monkeypatch, tmp_path):
    repo, _, _ = repo
    monkeypatch.setenv("SUPERBOT_DEPLOY_REPO", str(repo))
    mod = _load_deploy()
    assert _deploy(mod, monkeypatch, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef") == 6


def test_c3_no_arg_path_unchanged(monkeypatch, tmp_path):
    """Without --release, the C3 announcement-only no-op contract holds."""
    monkeypatch.setenv("SUPERBOT_LEASES_DB", str(tmp_path / "leases.sqlite3"))
    mod = _load_deploy()
    announced = []
    monkeypatch.setattr(mod, "_announce", lambda *a, **k: announced.append(a))
    rc = mod.main(["--by", "ubuntu:opencode:kalshi_deepbot"])
    assert rc == 0
    assert [a[0] for a in announced] == ["start", "end"]
