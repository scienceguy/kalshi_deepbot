"""C3 acceptance: advisory leases for shared-tree actions.

Spec AGENT_TASKS_v7.md C3: a second acquire for the same name fails until
release or TTL expiry; an expired lease can be taken; the deploy helper
refuses to run without the lease.
"""
import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

import leases

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_leases_db(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPERBOT_LEASES_DB",
                       str(tmp_path / "leases.sqlite3"))


def _load_deploy():
    spec = importlib.util.spec_from_file_location(
        "deploy_c3", REPO / "scripts" / "deploy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_second_acquire_same_name_fails_until_release():
    first = leases.acquire("deploy", "agent-a", ttl_s=60.0, now=1000.0)
    assert first["holder"] == "agent-a"
    with pytest.raises(leases.LeaseHeld) as exc:
        leases.acquire("deploy", "agent-b", ttl_s=60.0, now=1010.0)
    assert exc.value.holder == "agent-a"
    assert exc.value.expires_at == 1060.0
    assert leases.release("deploy", "agent-a") is True
    second = leases.acquire("deploy", "agent-b", ttl_s=60.0, now=1020.0)
    assert second["holder"] == "agent-b"


def test_expired_lease_can_be_taken():
    leases.acquire("deploy", "agent-a", ttl_s=10.0, now=1000.0)
    # 11 seconds later: the original TTL has expired, takes over atomically
    taken = leases.acquire("deploy", "agent-b", ttl_s=10.0, now=1011.0)
    assert taken["holder"] == "agent-b"


def test_same_holder_retaking_extends():
    leases.acquire("deploy", "agent-a", ttl_s=10.0, now=1000.0)
    extended = leases.acquire("deploy", "agent-a", ttl_s=30.0, now=1005.0)
    assert extended["expires_at"] == 1035.0


def test_only_holder_can_release():
    leases.acquire("deploy", "agent-a", ttl_s=60.0, now=1000.0)
    assert leases.release("deploy", "agent-b") is False
    assert leases.info("deploy", now=1001.0)["holder"] == "agent-a"
    assert leases.release("deploy", "agent-a") is True
    assert leases.info("deploy", now=1002.0) is None


def test_renew_extends_only_for_holder():
    leases.acquire("migrate", "agent-a", ttl_s=10.0, now=1000.0)
    assert leases.renew("migrate", "agent-b", ttl_s=50.0, now=1001.0) is False
    assert leases.renew("migrate", "agent-a", ttl_s=50.0, now=1001.0) is True
    assert leases.info("migrate", now=1002.0)["expires_at"] == 1051.0


def test_info_reports_expiry_flag():
    leases.acquire("migrate", "agent-a", ttl_s=5.0, now=1000.0)
    live = leases.info("migrate", now=1004.0)
    dead = leases.info("migrate", now=1006.0)
    assert live["expired"] is False
    assert dead["expired"] is True


def test_names_are_independent():
    leases.acquire("deploy", "agent-a", ttl_s=60.0, now=1000.0)
    restart = leases.acquire("restart:poly_us_trader", "agent-b",
                             ttl_s=60.0, now=1000.0)
    assert restart["name"] == "restart:poly_us_trader"


def test_guard_releases_on_exception():
    with pytest.raises(RuntimeError):
        with leases.guard("migrate", "agent-a", ttl_s=60.0):
            raise RuntimeError("boom")
    # lease was released: someone else can take it immediately
    taken = leases.acquire("migrate", "agent-b", ttl_s=60.0)
    assert taken["holder"] == "agent-b"


def test_guard_refuses_when_held():
    now = time.time()
    leases.acquire("deploy", "agent-a", ttl_s=60.0, now=now)
    with pytest.raises(leases.LeaseHeld):
        with leases.guard("deploy", "agent-b", ttl_s=60.0):
            raise AssertionError("never runs")


def test_deploy_helper_runs_when_lease_free(monkeypatch):
    mod = _load_deploy()
    announced = []

    def fake_run(cmd, **kw):
        announced.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    rc = mod.main(["--by", "ubuntu:opencode:kalshi_deepbot"])
    assert rc == 0
    topics = [cmd[cmd.index("--topic") + 1] for cmd in announced]
    assert topics == ["lease-start", "lease-end"]
    # lease was released after the block
    assert leases.info("deploy") is None


def test_deploy_helper_refuses_without_lease(monkeypatch):
    mod = _load_deploy()
    announced = []

    def fake_run(cmd, **kw):
        announced.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    leases.acquire("deploy", "agent-a", ttl_s=60.0)
    rc = mod.main(["--by", "agent-b"])
    assert rc == 2
    assert announced == []  # no start/end events when refused
