#!/usr/bin/env python3
"""C3 deploy helper / V1 release-directory deploy.

C3 (default, no --release): announcement-only no-op proving the lease path.
Refuses to run without the 'deploy' advisory lease.

V1 (--release <sha>): deploy an immutable release directory from a commit.
`/opt/superbot-releases/<sha>` is created with `git worktree add` (never the
dev working tree, so uncommitted edits can never ship). Runtime data stays
OUTSIDE the release: the release holds symlinks `.env` and `local_data` to the
shared copies, and the unit's WorkingDirectory is the `current` symlink into
the releases root. The deploy:

  1. takes the C3 'deploy' lease (refuses with exit 2 if held elsewhere),
  2. announces start on the logstream (advisory),
  3. resolves <sha> in SUPERBOT_DEPLOY_REPO, creates the release worktree,
     (re)creates the .env / local_data symlinks inside it,
  4. runs the verify command in the release dir (default: compileall + the
     full suite, seed 42, network blocked by tests/conftest.py — the same
     hermetic tree V3 gates on; SUPERBOT_DATA_DIR pointed at the smoke dir),
  5. runs a smoke command (default: --once --dry in an isolated data dir),
  6. atomically swaps the `current` symlink to the new release,
  7. copies the unit file, daemon-reloads, restarts the unit,
  8. waits for a healthy heartbeat (reported_state:<key> in the local bot_state
     with ts >= restart time — the same signal the watchdog reads),
  9. if the heartbeat never appears (or the restart fails), ALSO rolls
     `current` back to the previous release and restarts again.

Exit codes: 0 healthy, 2 lease refused, 3 heartbeat/restart failed and rolled
back (or nothing to roll back to), 4 verify failed (current untouched),
5 smoke failed (current untouched), 6 usage/git error.

Env knobs (defaults match the VPS):
  SUPERBOT_RELEASES_DIR    /opt/superbot-releases
  SUPERBOT_DEPLOY_REPO     /opt/superbot
  SUPERBOT_DEPLOY_UNIT     poly-us-dry.service
  SUPERBOT_UNIT_DIR        /etc/systemd/system
  SUPERBOT_SYSTEMCTL       systemctl
  SUPERBOT_VENV_PY         /opt/superbot/.venv/bin/python
  SUPERBOT_VERIFY_CMD      sh -c '"$SUPERBOT_VENV_PY" -m compileall -q .
                           && "$SUPERBOT_VENV_PY" -m pytest tests/ -q
                              --randomly-seed=42'
  SUPERBOT_SMOKE_CMD       sh -c 'SUPERBOT_DATA_DIR=<releases>/smoke_data
                           "$SUPERBOT_VENV_PY" poly_us_trader.py --once --dry'
  SUPERBOT_DATA_SRC        /opt/superbot/local_data
  SUPERBOT_ENV_SRC         /opt/superbot/.env
  SUPERBOT_HEARTBEAT_KEY   reported_state:poly_us
  SUPERBOT_HEARTBEAT_TIMEOUT  120 (seconds)
  SUPERBOT_HEARTBEAT_POLL  2 (seconds)
  SUPERBOT_HEARTBEAT_DB    exact bot_state DB path (default: derived from
                           SUPERBOT_LOCAL_DB_DIR or /opt/superbot/local_data
                           + DRY_RUN suffix)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # when run from elsewhere

try:
    import agent_identity
    _DEFAULT_AGENT = agent_identity.CODING_AGENT
except Exception:  # never let identity import break the deploy path
    _DEFAULT_AGENT = "ubuntu:opencode:kalshi_deepbot"

import leases  # noqa: E402


def _announce(kind: str, name: str, holder: str) -> None:
    """Append a lease-<start|end|fail> status event. Advisory: never raises."""
    cmd = [
        "python3", "-m", "mempalace", "logstream", "append",
        "--type", "status",
        "--stream", "project/kalshi_deepbot",
        "--room", "status",
        "--topic", f"lease-{kind}",
        "--from-agent", holder,
        "--to-agent", "*",
        "--body", json.dumps({"lease": name, "event": kind,
                              "holder": holder}),
    ]
    try:
        subprocess.run(cmd, timeout=10, check=False, capture_output=True)
    except Exception:
        pass  # announcements are advisory; the lease row is the record


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--by", default=_DEFAULT_AGENT,
                    help="issuing agent identity (default: coding agent)")
    ap.add_argument("--ttl", type=float, default=600.0,
                    help="lease TTL seconds (default 600; C3 no-op mode used 300)")
    ap.add_argument("--release", metavar="SHA",
                    help="git sha to deploy as an immutable release")
    args = ap.parse_args(argv)
    try:
        with leases.guard("deploy", args.by, ttl_s=args.ttl):
            _announce("start", "deploy", args.by)
            if not args.release:
                # C3: announcement-only no-op proving the lease path.
                _announce("end", "deploy", args.by)
                return 0
            rc = _deploy_release(args)
            _announce("end" if rc == 0 else "fail", "deploy", args.by)
            return rc
    except leases.LeaseHeld as e:
        print(json.dumps({"accepted": False, "reason": str(e)}),
              file=sys.stderr)
        return 2


# ── V1: release-directory deploy ────────────────────────────────────

def _run(cmd, cwd=None, env=None, shell=False):
    return subprocess.run(cmd, cwd=cwd, env=env, shell=shell,
                          check=False, capture_output=True, text=True)


def _render_unit(text: str, releases: Path) -> str:
    """Point a unit at the configured release root.

    The unit files ship with the default path; a custom SUPERBOT_RELEASES_DIR
    would otherwise install a unit that runs someone else's tree.
    """
    return text.replace("/opt/superbot-releases", str(releases))


def _swap_current(releases: Path, target: Path) -> None:
    """Atomically re-point releases/current at target."""
    tmp = releases / "current.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, releases / "current")  # atomic on POSIX


def _heartbeat_db() -> str:
    override = os.environ.get("SUPERBOT_HEARTBEAT_DB")
    if override:
        return override
    base = os.environ.get("SUPERBOT_LOCAL_DB_DIR") or "/opt/superbot/local_data"
    try:
        import config
        suffix = "_dry" if getattr(config, "DRY_RUN", True) else "_live"
    except Exception:
        suffix = "_dry"
    return os.path.join(base, f"superbot_local{suffix}.db")


def _heartbeat_fresh(key: str, since: datetime) -> bool | None:
    """True when bot_state[key] exists and its ts is at/after `since`.

    None when the DB cannot be read (treated as "not yet", keeps polling).
    """
    try:
        import local_db
        payload = local_db.get_local_db(db_path=_heartbeat_db()).get_bot_state(key)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return False
    ts = payload.get("ts") or ""
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed >= since


def _deploy_release(args) -> int:
    sha_ref = args.release
    releases = Path(os.environ.get("SUPERBOT_RELEASES_DIR", "/opt/superbot-releases"))
    repo = Path(os.environ.get("SUPERBOT_DEPLOY_REPO", "/opt/superbot"))
    unit = os.environ.get("SUPERBOT_DEPLOY_UNIT", "poly-us-dry.service")
    unit_dir = Path(os.environ.get("SUPERBOT_UNIT_DIR", "/etc/systemd/system"))
    systemctl = os.environ.get("SUPERBOT_SYSTEMCTL", "systemctl")
    venv_py = os.environ.get("SUPERBOT_VENV_PY", "/opt/superbot/.venv/bin/python")
    data_src = os.environ.get("SUPERBOT_DATA_SRC", "/opt/superbot/local_data")
    env_src = os.environ.get("SUPERBOT_ENV_SRC", "/opt/superbot/.env")
    smoke_dir = releases / "smoke_data"

    r = _run(["git", "-C", str(repo), "rev-parse", "--verify", f"{sha_ref}^{{commit}}"])
    if r.returncode != 0:
        print(json.dumps({"ok": False, "error": f"unknown sha {sha_ref!r}: {r.stderr.strip()}"}),
              file=sys.stderr)
        return 6
    sha = r.stdout.strip()

    releases.mkdir(parents=True, exist_ok=True)
    release_dir = releases / sha
    if release_dir.exists():
        # Reuse only if this is the same commit — never deploy stale content.
        head = _run(["git", "-C", str(release_dir), "rev-parse", "HEAD"]).stdout.strip()
        if head != sha:
            print(json.dumps({"ok": False,
                              "error": f"{release_dir} exists but holds {head}"}),
                  file=sys.stderr)
            return 6
    else:
        r = _run(["git", "-C", str(repo), "worktree", "add", "--detach",
                   str(release_dir), sha])
        if r.returncode != 0:
            print(json.dumps({"ok": False, "error": f"worktree add failed: {r.stderr.strip()}"}),
                  file=sys.stderr)
            return 6

    # Runtime data stays outside the release: symlink to the shared copies.
    for link_name, src in ((".env", env_src), ("local_data", data_src)):
        if not os.path.exists(src):
            print(json.dumps({"ok": False, "error": f"runtime source missing: {src}"}),
                  file=sys.stderr)
            return 6
        link = release_dir / link_name
        if link.is_symlink() or link.exists():
            link.unlink() if link.is_symlink() or link.is_file() else shutil.rmtree(link)
        link.symlink_to(src)

    env = dict(os.environ)
    env["SUPERBOT_DATA_DIR"] = str(smoke_dir)  # keep tests + trader state out of the release
    smoke_dir.mkdir(parents=True, exist_ok=True)
    env["SUPERBOT_VENV_PY"] = venv_py

    # Verify: compileall + tests inside the release (network blocked by conftest).
    verify_cmd = os.environ.get(
        "SUPERBOT_VERIFY_CMD",
        f'"{venv_py}" -m compileall -q . && "{venv_py}" -m pytest tests/ -q --randomly-seed=42')
    r = _run(["sh", "-c", verify_cmd], cwd=release_dir, env=env)
    if r.returncode != 0:
        print(json.dumps({"ok": False, "step": "verify", "tail": r.stdout[-2000:] + r.stderr[-1000:]}),
              file=sys.stderr)
        return 4  # current untouched

    # Smoke: one cycle, dry — proves the release starts and exits cleanly.
    smoke_cmd = os.environ.get(
        "SUPERBOT_SMOKE_CMD",
        f'"{venv_py}" poly_us_trader.py --once --dry')
    r = _run(["sh", "-c", smoke_cmd], cwd=release_dir, env=env)
    if r.returncode != 0:
        print(json.dumps({"ok": False, "step": "smoke", "tail": r.stdout[-2000:] + r.stderr[-1000:]}),
              file=sys.stderr)
        return 5  # current untouched

    # Swap current -> new release (remember the old target for rollback).
    current = releases / "current"
    previous = None
    try:
        previous = os.readlink(current) if current.is_symlink() else None
    except OSError:
        previous = None
    _swap_current(releases, release_dir)

    # Install the unit from the release, reload, restart.
    unit_src = release_dir / "deploy" / unit
    if unit_src.exists():
        unit_dir.mkdir(parents=True, exist_ok=True)
        dest = unit_dir / unit
        shutil.copy2(unit_src, dest)
        text = dest.read_text()
        rendered = _render_unit(text, releases)
        if rendered != text:
            dest.write_text(rendered)
    r = _run([systemctl, "daemon-reload"])
    if r.returncode != 0:
        return _rollback(systemctl, current, previous, releases, unit, args, "daemon-reload failed")
    restart_wall = datetime.now(timezone.utc)
    r = _run([systemctl, "restart", unit])
    if r.returncode != 0:
        return _rollback(systemctl, current, previous, releases, unit, args, "restart failed")

    # Wait for a healthy heartbeat: ts at/after the restart, fresh within the timeout.
    import time as _time
    key = os.environ.get("SUPERBOT_HEARTBEAT_KEY", "reported_state:poly_us")
    deadline = _time.monotonic() + float(os.environ.get("SUPERBOT_HEARTBEAT_TIMEOUT", "120"))
    poll = float(os.environ.get("SUPERBOT_HEARTBEAT_POLL", "2"))
    while _time.monotonic() < deadline:
        if _heartbeat_fresh(key, restart_wall):
            print(json.dumps({"ok": True, "release": sha, "unit": unit}))
            return 0
        _time.sleep(poll)
    return _rollback(systemctl, current, previous, releases, unit, args,
                     f"no healthy heartbeat on {key} within timeout")


def _rollback(systemctl, current, previous, releases, unit, args, reason):
    """Re-point current at the previous release and restart. Returns 3."""
    if previous and Path(previous).exists():
        _swap_current(releases, Path(previous))
        _run([systemctl, "restart", unit])
        print(json.dumps({"ok": False, "rolled_back_to": previous, "reason": reason}),
              file=sys.stderr)
    else:
        # No previous release — leave current as-is, be loud about it.
        print(json.dumps({"ok": False, "rolled_back_to": None,
                          "reason": f"{reason}; no previous release to roll back to"}),
              file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
