"""H15 — the OCI launcher opens SSH only to the requested CIDR, and no Termux
path survives in scripts/ or deploy/.

The rule builder is pure (no SDK call), so this runs without `oci` installed.
The CLI check spawns the script with no CIDR and asserts a non-zero exit and a
message naming the flag.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import oci_launch  # noqa: E402


def test_ingress_is_port_22_from_the_requested_cidr_only():
    specs = oci_launch.ssh_ingress_specs("198.51.100.7/32")
    assert specs == [{"protocol": "6", "source": "198.51.100.7/32",
                      "port_min": 22, "port_max": 22}]
    # a host address is normalised to its network, never widened
    assert oci_launch.ssh_ingress_specs("198.51.100.7/24")[0]["source"] == \
        "198.51.100.0/24"


def test_world_open_ssh_needs_the_explicit_flag():
    for world in ("0.0.0.0/0", "::/0"):
        with pytest.raises(ValueError, match="world-open"):
            oci_launch.ssh_ingress_specs(world)
        assert oci_launch.ssh_ingress_specs(world, allow_world=True)[0]["source"] == world


def test_unparseable_cidr_is_rejected():
    with pytest.raises(ValueError, match="not a CIDR"):
        oci_launch.ssh_ingress_specs("198.51.100.7/99")
    with pytest.raises(ValueError, match="no --ssh-allow-cidr"):
        oci_launch.ssh_ingress_specs("")


def test_launcher_without_a_cidr_exits_with_a_clear_message():
    env = {k: v for k, v in os.environ.items() if k != "OCI_SSH_ALLOW_CIDR"}
    p = subprocess.run([sys.executable, str(SCRIPTS / "oci_launch.py")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode != 0
    assert "--ssh-allow-cidr" in p.stderr
    assert "usage:" in p.stderr  # argparse's error path, not a traceback


def test_no_termux_paths_remain():
    bad = []
    for base in (SCRIPTS, REPO / "deploy"):
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            text = path.read_text(errors="replace")
            if any(m in text for m in ("com.termux", "termux-job-scheduler",
                                       "~/.termux")):
                bad.append(str(path.relative_to(REPO)))
    assert bad == [], f"Termux-only paths remain (dead on the VPS): {bad}"


def test_creds_guard_runs_from_a_systemd_timer():
    service = REPO / "deploy" / "superbot-creds-guard.service"
    timer = REPO / "deploy" / "superbot-creds-guard.timer"
    assert service.is_file() and timer.is_file()
    assert "n8n_creds_guard.sh" in service.read_text()
    assert "superbot-creds-guard.service" in timer.read_text()


@pytest.mark.skipif(shutil.which("systemd-analyze") is None,
                    reason="systemd-analyze not installed")
@pytest.mark.parametrize("unit", ["superbot-creds-guard.service",
                                  "superbot-creds-guard.timer",
                                  "superbot-retrain.service",
                                  "superbot-retrain.timer"])
def test_new_units_verify(unit):
    r = subprocess.run(["systemd-analyze", "verify", str(REPO / "deploy" / unit)],
                       capture_output=True, text=True, timeout=60)
    ours = [ln for ln in (r.stdout + r.stderr).splitlines() if unit in ln]
    assert r.returncode == 0, ours
    assert ours == []
