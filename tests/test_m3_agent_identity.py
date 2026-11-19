"""M3 acceptance: one canonical agent-identity module.

- constants: the coding agent and the two service processes, distinct
- presence/topic helper generates identity-scoped names
- bridge: per-identity singletons; default bridge is the coding agent
- events: make_event maps kalshi_trader/poly_us_trader sources to the
  service identities; explicit agent_id still wins; unknown sources fall
  back to host:pid
- local.py and n8n_coordination.py route through the module
- repo-wide grep: the stale pre-VPS identity string appears nowhere
  under the repo except docs/ (historical records)
"""
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
STALE = "termux" + ":" + "opencode"  # keep this file out of its own grep net


def test_constants_are_distinct_and_well_formed():
    import agent_identity
    assert agent_identity.CODING_AGENT == "ubuntu:opencode:kalshi_deepbot"
    assert agent_identity.KALSHI_TRADER == "ubuntu:service:kalshi_trader"
    assert agent_identity.POLY_TRADER == "ubuntu:service:poly_us_trader"
    for ident in agent_identity.ALL:
        assert re.fullmatch(r"ubuntu:[a-z_]+:[a-z0-9_]+", ident)
    assert len(set(agent_identity.ALL)) == 3


def test_presence_topic_helper():
    import agent_identity
    assert agent_identity.presence_topic(agent_identity.CODING_AGENT) == \
        "presence-ubuntu:opencode:kalshi_deepbot"
    assert agent_identity.presence_topic(agent_identity.KALSHI_TRADER) == \
        "presence-ubuntu:service:kalshi_trader"
    assert agent_identity.presence_topic("x") == "presence-x"


def test_local_module_identity_is_the_coding_agent():
    import local
    import agent_identity
    assert local.AGENT_IDENTITY == agent_identity.CODING_AGENT


def test_bridge_per_identity_singletons(monkeypatch):
    import mempalace_bridge
    import agent_identity
    monkeypatch.setattr(mempalace_bridge, "_DEFAULT_BRIDGES", {})
    a = mempalace_bridge.get_default_bridge()
    b = mempalace_bridge.get_default_bridge(agent_identity.KALSHI_TRADER)
    c = mempalace_bridge.get_default_bridge(agent_identity.POLY_TRADER)
    assert a is mempalace_bridge.get_default_bridge()  # same identity, same bridge
    assert a is not b and b is not c and a is not c
    assert (a.agent_identity, b.agent_identity, c.agent_identity) == \
        (agent_identity.CODING_AGENT,
         agent_identity.KALSHI_TRADER,
         agent_identity.POLY_TRADER)


def test_make_event_maps_source_to_service_identity():
    import agent_identity
    import events
    ev = events.make_event("kalshi_trader", "dry", "settlement", {})
    assert ev.agent_id == agent_identity.KALSHI_TRADER
    ev = events.make_event("poly_us_trader", "dry", "settlement", {})
    assert ev.agent_id == agent_identity.POLY_TRADER


def test_make_event_explicit_agent_id_wins():
    import events
    ev = events.make_event("kalshi_trader", "dry", "settlement", {},
                           agent_id="custom:identity:x")
    assert ev.agent_id == "custom:identity:x"


def test_make_event_unknown_source_falls_back_to_host():
    import events
    ev = events.make_event("something_else", "dry", "settlement", {})
    host, sep, pid = (ev.agent_id or "").rpartition(":")
    assert sep == ":" and host and pid.isdigit()


def test_n8n_coordination_uses_module_identity():
    text = (REPO_ROOT / "n8n_coordination.py").read_text()
    assert "agent_identity.CODING_AGENT" in text
    assert STALE not in text


def test_trader_passes_service_identities():
    src = (REPO_ROOT / "trader.py").read_text()
    assert "get_default_bridge(agent_identity.KALSHI_TRADER)" in src
    assert "on_session_start(agent_identity.KALSHI_TRADER)" in src
    poly = (REPO_ROOT / "poly_us_trader.py").read_text()
    assert "get_default_bridge(agent_identity.POLY_TRADER)" in poly


def test_no_stale_identity_anywhere_outside_docs():
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT,
        capture_output=True, check=True).stdout.decode().split("\0")
    offenders = []
    for rel in tracked:
        if not rel or rel.startswith("docs/"):
            continue
        p = REPO_ROOT / rel
        try:
            if p.is_file() and STALE in p.read_text(errors="ignore"):
                offenders.append(rel)
        except (OSError, UnicodeDecodeError):
            continue
    assert offenders == [], \
        f"stale identity {STALE!r} outside docs/: {offenders}"
