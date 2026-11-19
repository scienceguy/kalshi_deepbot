import json
import logging
import subprocess
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_STORE = {}

CHECKPOINT_ENTITY = "agent_continuity_checkpoint"

# Agent identity: host:harness:project (per MemPalace protocol) — one
# canonical source for every identity in the repo (M3).
from agent_identity import CODING_AGENT as AGENT_IDENTITY

_MP = ""
# Try shutil.which first (works when PATH includes ~/bin)
_candidate = shutil.which("mempalace")
if _candidate and os.path.isfile(_candidate) and os.access(_candidate, os.X_OK):
    _MP = _candidate
# Also check next to the running python (venv bin), e.g. VPS where PATH may
# not include the venv directory.
if not _MP:
    _venv_mp = os.path.join(os.path.dirname(sys.executable), "mempalace")
    if os.path.isfile(_venv_mp) and os.access(_venv_mp, os.X_OK):
        _MP = _venv_mp
# Explicit candidates for restricted-PATH environments
if not _MP:
    for _p in (
        "/usr/local/bin/mempalace",
        "/usr/bin/mempalace",
        os.path.expanduser("~/bin/mempalace"),
        os.path.expanduser("~/.local/bin/mempalace"),
    ):
        if os.path.isfile(_p) and os.access(_p, os.X_OK):
            _MP = _p
            break
_MP_TMP = os.environ.get("TMPDIR")
if not _MP_TMP or not os.path.isdir(_MP_TMP):
    import tempfile
    _candidates = [
        str(Path.home() / "tmp" / "opencode"),
        "/tmp/opencode",
        tempfile.gettempdir(),
    ]
    for _c in _candidates:
        try:
            os.makedirs(_c, exist_ok=True)
            _probe = os.path.join(_c, ".wtest")
            with open(_probe, "w") as _f:
                _f.write("ok")
            os.remove(_probe)
            _MP_TMP = _c
            break
        except Exception:
            continue
    if not _MP_TMP:
        _MP_TMP = tempfile.gettempdir()

def _mp_available() -> bool:
    """True when the MemPalace CLI binary is resolvable."""
    return bool(_MP)

def _mp(*args):
    import shlex
    if not _mp_available():
        raise RuntimeError("mempalace CLI not found (tried PATH, /usr/local/bin, /usr/bin and ~/bin, ~/.local/bin)")
    quoted = [shlex.quote(a) for a in args]
    # Use the approved temp dir for mempalace CLI invocations.
    cmd = "cd " + _MP_TMP + " && " + _MP + " " + " ".join(quoted)
    # H4: the CLI reads MEMPALACE_PALACE_PATH; accept MEMPALACE_DIR too so one
    # variable points every identity at the same palace.
    env = os.environ.copy()
    if env.get("MEMPALACE_DIR"):
        env.setdefault("MEMPALACE_PALACE_PATH", env["MEMPALACE_DIR"])
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                          timeout=30, env=env)

def _serialize(val):
    return json.dumps(val)

def _deserialize_payload(payload: str) -> dict:
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return {}

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _parse_activity(last_activity) -> datetime | None:
    """Parse checkpoint `last_activity` (float epoch or ISO string)."""
    if last_activity is None:
        return None
    if isinstance(last_activity, (int, float)):
        try:
            return datetime.fromtimestamp(float(last_activity), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(last_activity))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed

def _lapse_minutes(last_activity) -> float:
    """Minutes since `last_activity` (float epoch or ISO). 0 on garbage."""
    parsed = _parse_activity(last_activity)
    if parsed is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 60.0)

# Narrative/interests fields in checkpoints are opt-in (see on_session_start's
# first-session text and checkpoint()'s default). Disabled by default (M4).
_NARRATIVE_ENV = "LOCAL_CHECKPOINT_NARRATIVE"

def _narrative_enabled() -> bool:
    return os.environ.get(_NARRATIVE_ENV, "").strip().lower() in ("1", "true", "yes", "on")

def _STORE_set(k: str, v) -> None:
    _STORE[k] = v

def _STORE_get(k: str, default=None):
    return _STORE.get(k, default)

def get_latest_checkpoint() -> dict:
    """Return the latest MemPalace checkpoint state for LLM consensus context.

    Operational fields only, sourced from the last checkpoint the trader
    actually wrote. session_pnl stays 0.0 until a cycle records it — the
    value is honest, never fabricated.
    """
    return {
        "cycles_completed": _STORE_get("_cycles_completed", 0),
        "session_pnl": _STORE_get("_session_pnl", 0.0),
        "session_total": _STORE_get("_session_total", 0),
        "last_activity": _STORE_get("_last_activity", ""),
        "checkpoint_key": _STORE_get("_resume_key", "active"),
    }

def _default_checkpoint_state() -> dict:
    """Lean operational default for a first session. Narrative fields only
    when LOCAL_CHECKPOINT_NARRATIVE is enabled."""
    state = {
        "last_activity": _serialize(_now_iso()),
        "cycles_completed": _STORE_get("_cycles_completed", 0),
        "session_pnl": _STORE_get("_session_pnl", 0.0),
        "session_total": _STORE_get("_session_total", 0),
        "resume_key": _STORE_get("_resume_key", "first-session-ever"),
    }
    if _narrative_enabled():
        state.update({
            "last_topic": _STORE_get("_last_topic", "none"),
            "interests": _STORE_get("_interests", []),
            "last_url": _STORE_get("_last_url", ""),
            "exploration_depth": _STORE_get("_exploration_depth", 0),
            "self_narrative": _STORE_get("_self_narrative", ""),
            "recent_thoughts": _STORE_get("_recent_thoughts", []),
            "recent_urls": _STORE_get("_recent_urls", []),
        })
    return state

def checkpoint(state: dict | None = None, correlation_id: str | None = None,
               identity: str = AGENT_IDENTITY) -> dict:
    if state is None:
        state = _default_checkpoint_state()
    state["resume_key"] = state.get("resume_key") or _now_iso()
    payload = _serialize(state)
    body = "CHECKPOINT_ENTITY:" + payload
    corr = correlation_id or state.get("resume_key")
    r = _mp(
        "logstream", "append",
        "--type", "status",
        "--stream", "project/kalshi_deepbot",
        "--room", "continuity",
        "--topic", "agent-checkpoint",
        "--correlation-id", corr,
        "--from-agent", identity,
        "--to-agent", "*",
        "--body", body,
    )
    if r.returncode == 0:
        log.info("MemPalace checkpoint saved: cycles=%s, pnl=$%s, key=%s", state.get("cycles_completed", 0), state.get("session_pnl", 0.0), state.get("resume_key", "unknown"))
    else:
        log.warning("Checkpoint KG RC=%s: %s", r.returncode, r.stderr[:120])
    for k, v in state.items():
        _STORE_set("_" + k, v)
    return state

def resume(identity: str = AGENT_IDENTITY) -> dict | None:
    r = _mp(
        "logstream", "list",
        "--topic", "agent-checkpoint",
        "--to-agent", identity,
        "--json",
        "--limit", "5",
    )
    if r.returncode != 0 or not r.stdout:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        log.warning("resume: could not parse logstream JSON output")
        return None
    events = data.get("events", [])
    # Events are returned oldest-first by default; take the last (newest) valid checkpoint
    for event in reversed(events):
        body = event.get("body", "")
        if "CHECKPOINT_ENTITY:" in body:
            payload = body.split("CHECKPOINT_ENTITY:", 1)[1]
            state = _deserialize_payload(payload)
            for k, v in state.items():
                _STORE_set("_" + k, v)
            log.info("Resumed from KG checkpoint: key=%s", state.get("resume_key", "unknown"))
            return state
    return None

def on_session_start(identity: str = AGENT_IDENTITY) -> dict | None:
    state = resume(identity)
    if state is None:
        log.info("First session ever - no prior checkpoint; starting fresh")
        state = _default_checkpoint_state()
    else:
        # Lapse since last activity, for both float-epoch and ISO timestamps.
        lapse = _lapse_minutes(state.get("last_activity"))
        log.info("Resumed checkpoint %s (activity ~%.1f min ago; session_total=%d)",
                 state.get("resume_key", "unknown"), lapse,
                 state.get("session_total", 0))
    for k, v in state.items():
        _STORE_set("_" + k, v)
    # Announce the agent is alive on the shared logstream so peers know this
    # session is active and continuous.
    try:
        _post_status("online", {"session_total": state.get("session_total", 0), "resume_key": state.get("resume_key")}, identity=identity)
    except Exception as e:
        log.debug("Could not post session-start status: %s", e)
    return state

def on_session_end(identity: str = AGENT_IDENTITY) -> None:
    state = _default_checkpoint_state()
    state["last_activity"] = _now_iso()
    state["session_total"] = _STORE_get("_session_total", 0) + 1
    state["resume_key"] = _STORE_get("_resume_key", "unknown")
    checkpoint(state, identity=identity)
    log.info("Self-model checkpoint saved at session end: key=%s (session_total=%d)", state.get("resume_key", "unknown"), state.get("session_total", 0))

def _post_status(status: str, extra: dict | None = None, identity: str = AGENT_IDENTITY) -> None:
    """Post a status event to the MemPalace logstream announcing agent state."""
    body_parts = ["status=" + status, "agent=" + identity, "ts=" + _now_iso()]
    if extra:
        body_parts.append("data=" + _serialize(extra))
    r = _mp(
        "logstream", "append",
        "--type", "status",
        "--stream", "project/kalshi_deepbot",
        "--room", "status",
        "--topic", "agent-heartbeat",
        "--correlation-id", _now_iso(),
        "--from-agent", identity,
        "--to-agent", "*",
        "--body", " | ".join(body_parts),
    )
    if r.returncode != 0:
        log.debug("Status post RC=%s: %s", r.returncode, r.stderr[:120])

def heartbeat() -> None:
    """Post a periodic heartbeat status so the agent's continuous presence is
    visible on the MemPalace logstream. Safe to call every cycle."""
    _post_status("heartbeat", {"cycles_completed": _STORE_get("_cycles_completed", 0)})

def check_for_tasks() -> list[dict]:
    """Sweep the MemPalace inbox for task.request events addressed to this
    agent. Returns a list of event dicts (may be empty)."""
    if not _mp_available():
        log.debug("check_for_tasks: mempalace CLI unavailable — skipping inbox sweep")
        return []
    r = _mp(
        "logstream", "list",
        "--stream", "project/kalshi_deepbot",
        "--room", "delegation",
        "--type", "task.request",
        "--to-agent", AGENT_IDENTITY,
        "--status", "open",
        "--json",
        "--limit", "10",
    )
    if r.returncode != 0 or not r.stdout:
        return []
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        log.warning("check_for_tasks: could not parse logstream JSON output")
        return []
    return data.get("events", [])

def ack_event(event_id: str) -> bool:
    """Ack a MemPalace event via logstream ack (type=event.ack).
    Returns True if the ack was accepted."""
    if not _mp_available():
        log.debug("ack_event: mempalace CLI unavailable — skipping ack")
        return False
    r = _mp("logstream", "ack", "--event-id", event_id)
    if r.returncode == 0:
        log.debug("Acked MemPalace event %s", event_id)
        return True
    log.warning("ack_event failed for %s: %s", event_id, r.stderr[:120])
    return False

def get_self_narrative() -> str:
    return _STORE_get("_self_narrative", "")

def get_recent_thoughts() -> list:
    return _STORE_get("_recent_thoughts", [])

def get_recent_urls() -> list:
    return _STORE_get("_recent_urls", [])

def get_cycles_completed() -> int:
    return _STORE_get("_cycles_completed", 0)

def update_interests(new_interests: list) -> None:
    current = _STORE_get("_interests", [])
    merged = list(set(current + new_interests))
    checkpoint({"interests": merged})
    log.info("Interests updated (now %d categories): %s", len(merged), merged)

def get_interests() -> list:
    return _STORE_get("_interests", [])

_CONTINUITY_DIR = str(Path(__file__).resolve().parent / ".continuity")

def record_webfind(topic: str, url: str, depth: int = 0) -> None:
    append_narrative_entry("webfind", "topic=%s url=%s depth=%s" % (topic, url, depth))

def append_narrative_entry(topic: str, text: str) -> None:
    import os, json
    from datetime import datetime
    tmp_dir = _CONTINUITY_DIR
    os.makedirs(tmp_dir, exist_ok=True)
    chmod_path = os.path.join(tmp_dir, "narrative.json")
    try:
        with open(chmod_path, "r") as f:
            narrative = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        narrative = {"entries": [], "last_topic": "", "session_count": 0}
    narrative["entries"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "topic": topic,
        "text": text,
    })
    narrative["entries"] = narrative["entries"][-50:]
    narrative["last_topic"] = topic
    narrative["session_count"] = narrative.get("session_count", 0) + 1
    with open(chmod_path, "w") as f:
        json.dump(narrative, f, indent=2)
    os.chmod(chmod_path, 0o600)

def get_peer_awareness() -> dict:
    checkpoint_exists = resume() is not None
    peers_working = []
    r = _mp("logstream", "list", "--room", "continuity", "--json", "--limit", "20")
    if r.returncode == 0 and r.stdout:
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            data = {"events": []}
        for event in data.get("events", []):
            ident = event.get("from_agent", "")
            if not ident or ident == AGENT_IDENTITY:
                continue
            topic = event.get("topic", "unknown")
            peers_working.append((ident, topic, "working"))
    recent_broadcasts = []
    try:
        r = _mp("logstream", "list", "--topic", "cross-project-awareness",
                "--to-agent", AGENT_IDENTITY, "--json", "--limit", "3")
        if r.returncode == 0 and r.stdout:
            try:
                data = json.loads(r.stdout)
            except json.JSONDecodeError:
                data = {"events": []}
            for event in data.get("events", [])[:3]:
                recent_broadcasts.append(event.get("body", "").strip())
    except Exception:
        pass
    return {
        "self_status": "working" if checkpoint_exists else "between_sessions",
        "peers_working": peers_working,
        "recent_broadcasts": recent_broadcasts,
        "continuity_active": checkpoint_exists,
    }

def artifact_put(content: str, wing: str, room: str,
                 from_agent: str = AGENT_IDENTITY,
                 stream: str = "project/kalshi_deepbot",
                 correlation_id: str | None = None) -> dict | None:
    """Durably store content in MemPalace via `mempalace artifact put`.

    Returns the artifact dict (with id/sha256) on success, None on failure.
    Used to file decisions, patches, and shared knowledge so peers can recall
    them via `mempalace artifact get`.
    """
    if not _mp_available():
        log.debug("artifact_put: mempalace CLI unavailable — skipping")
        return None
    corr = correlation_id or _now_iso()
    r = _mp(
        "artifact", "put",
        "--content", content,
        "--wing", wing,
        "--room", room,
        "--from-agent", from_agent,
        "--stream", stream,
        "--correlation-id", corr,
    )
    if r.returncode != 0:
        log.warning("artifact_put RC=%s: %s", r.returncode, r.stderr[:200])
        return None
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        log.warning("artifact_put: could not parse output: %s", r.stdout[:200])
        return None

def artifact_get(artifact_id: str) -> dict | None:
    """Retrieve a MemPalace artifact via `mempalace artifact get`.

    Returns the artifact dict on success, None if not found or on error.
    Caller should verify sha256 before applying any patch content.
    """
    if not _mp_available():
        log.debug("artifact_get: mempalace CLI unavailable — skipping")
        return None
    r = _mp("artifact", "get", "--artifact-id", artifact_id)
    if r.returncode != 0:
        log.debug("artifact_get RC=%s: %s", r.returncode, r.stderr[:200])
        return None
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        log.warning("artifact_get: could not parse output: %s", r.stdout[:200])
        return None

def kg_add(entity: str, relation: str, target: str,
           from_agent: str = AGENT_IDENTITY) -> bool:
    """Add a knowledge-graph fact via `mempalace kg add`.

    Returns True on success. Used to record durable relational facts
    (e.g. `"SuperBot Monitor" → "monitors" → "<workflow>"`,
    `"workflow" → "owned_by" → "project"`).
    """
    if not _mp_available():
        log.debug("kg_add: mempalace CLI unavailable — skipping")
        return False
    r = _mp(
        "kg", "add",
        "--entity", entity,
        "--relation", relation,
        "--target", target,
        "--from-agent", from_agent,
    )
    if r.returncode != 0:
        log.warning("kg_add RC=%s: %s", r.returncode, r.stderr[:200])
        return False
    return True

def kg_supersede(entity: str, relation: str, target: str,
                 from_agent: str = AGENT_IDENTITY) -> bool:
    """Replace a single-valued KG fact via `mempalace kg supersede`.

    Returns True on success. Use when a fact changes (e.g. a workflow is
    replaced) — the old relationship is superseded rather than duplicated.
    """
    if not _mp_available():
        log.debug("kg_supersede: mempalace CLI unavailable — skipping")
        return False
    r = _mp(
        "kg", "supersede",
        "--entity", entity,
        "--relation", relation,
        "--target", target,
        "--from-agent", from_agent,
    )
    if r.returncode != 0:
        log.warning("kg_supersede RC=%s: %s", r.returncode, r.stderr[:200])
        return False
    return True

def kg_invalidate(entity: str, relation: str, target: str,
                  from_agent: str = AGENT_IDENTITY) -> bool:
    """Retire a KG fact via `mempalace kg invalidate`.

    Returns True on success. Use when a fact ended without replacement
    (e.g. a retired workflow).
    """
    if not _mp_available():
        log.debug("kg_invalidate: mempalace CLI unavailable — skipping")
        return False
    r = _mp(
        "kg", "invalidate",
        "--entity", entity,
        "--relation", relation,
        "--target", target,
        "--from-agent", from_agent,
    )
    if r.returncode != 0:
        log.warning("kg_invalidate RC=%s: %s", r.returncode, r.stderr[:200])
        return False
    return True

__all__ = [
    "checkpoint", "resume", "on_session_start", "on_session_end",
    "get_self_narrative", "get_recent_thoughts", "get_recent_urls",
    "get_cycles_completed", "update_interests", "get_interests",
    "record_webfind", "get_peer_awareness", "heartbeat", "check_for_tasks",
    "ack_event", "CHECKPOINT_ENTITY",
    "artifact_put", "artifact_get", "kg_add", "kg_supersede", "kg_invalidate",
]