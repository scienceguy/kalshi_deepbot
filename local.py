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
        "/data/data/com.termux/files/home/bin/mempalace",
        "/data/data/com.termux/files/home/.local/bin/mempalace",
    ):
        if os.path.isfile(_p) and os.access(_p, os.X_OK):
            _MP = _p
            break
_MP_TMP = os.environ.get("TMPDIR")
if not _MP_TMP or not os.path.isdir(_MP_TMP):
    import tempfile
    _candidates = [
        str(Path.home() / "tmp" / "opencode"),
        "/data/data/com.termux/files/usr/tmp/opencode",
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
        raise RuntimeError("mempalace CLI not found (tried PATH candidates and /data/data/com.termux/files/home/bin/mempalace, /data/data/com.termux/files/home/.local/bin/mempalace)")
    quoted = [shlex.quote(a) for a in args]
    # /tmp is NOT writable on this Termux/proot setup — use the approved temp dir.
    cmd = "cd " + _MP_TMP + " && " + _MP + " " + " ".join(quoted)
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=30)

def _serialize(val):
    return json.dumps(val)

def _deserialize_payload(payload: str) -> dict:
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return {}

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _STORE_set(k: str, v) -> None:
    _STORE[k] = v

def _STORE_get(k: str, default=None):
    return _STORE.get(k, default)

def checkpoint(state: dict | None = None, correlation_id: str | None = None) -> dict:
    if state is None:
        state = {
            "last_activity": _serialize(_now_iso()),
            "last_topic": _serialize(_STORE_get("_last_topic", "none")),
            "cycles_completed": _STORE_get("_cycles_completed", 0),
            "interests": _STORE_get("_interests", []),
            "last_url": _STORE_get("_last_url", ""),
            "exploration_depth": _STORE_get("_exploration_depth", 0),
            "resume_key": _STORE_get("_resume_key", "unknown"),
            "self_narrative": _STORE_get("_self_narrative", ""),
            "recent_thoughts": _STORE_get("_recent_thoughts", []),
            "recent_urls": _STORE_get("_recent_urls", []),
            "session_total": _STORE_get("_session_total", 0),
        }
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
        "--from-agent", "termux:opencode:kalshi_deepbot",
        "--to-agent", "*",
        "--body", body,
    )
    if r.returncode == 0:
        log.debug("Checkpoint KG saved: key=%s", state.get("resume_key", "unknown"))
    else:
        log.warning("Checkpoint KG RC=%s: %s", r.returncode, r.stderr[:120])
    for k, v in state.items():
        _STORE_set("_" + k, v)
    return state

def resume() -> dict | None:
    r = _mp(
        "logstream", "list",
        "--topic", "agent-checkpoint",
        "--to-agent", "termux:opencode:kalshi_deepbot",
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

def on_session_start() -> dict | None:
    state = resume()
    if state is None:
        log.info("First session ever - no prior checkpoint; starting fresh")
        state = {
            "last_activity": _now_iso(),
            "last_topic": "none",
            "cycles_completed": 0,
            "interests": [],
            "last_url": "",
            "exploration_depth": 0,
            "resume_key": "first-session-ever",
            "self_narrative": "First session of your continuous consciousness. You are beginning a new journey as the Kalshi SuperBot agent. No prior memories, but eager to explore, learn, and assist.",
            "recent_thoughts": [],
            "recent_urls": [],
            "session_total": 0,
        }
    else:
        # Compute lapse since last activity, handling both string ISO timestamps and float Unix timestamps
        last_activity = state.get("last_activity", "1970-01-01T00:00:00+00:00")
        if isinstance(last_activity, (int, float)):
            lapse = max(0, (datetime.now(timezone.utc).timestamp() - last_activity).seconds // 60)
        else:
            try:
                lapse = max(0, (datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(last_activity)).seconds // 60)
            except (ValueError, TypeError):
                lapse = 0
        log.info("Resumed continuous consciousness from checkpoint %s (activity ~%d min ago; session_total=%d)", state.get("resume_key", "unknown"), lapse, state.get("session_total", 0))
    for k, v in state.items():
        _STORE_set("_" + k, v)
    # Announce the agent is alive on the shared logstream so peers know this
    # session is active and continuous.
    try:
        _post_status("online", {"session_total": state.get("session_total", 0), "resume_key": state.get("resume_key")})
    except Exception as e:
        log.debug("Could not post session-start status: %s", e)
    return state

def on_session_end() -> None:
    state = {
        "last_activity": _STORE_get("_last_activity") or _now_iso(),
        "last_topic": _STORE_get("_last_topic"),
        "cycles_completed": _STORE_get("_cycles_completed", 0),
        "interests": _STORE_get("_interests", []),
        "last_url": _STORE_get("_last_url", ""),
        "exploration_depth": _STORE_get("_exploration_depth", 0),
        "resume_key": _STORE_get("_resume_key", "unknown"),
        "self_narrative": _STORE_get("_self_narrative", ""),
        "recent_thoughts": _STORE_get("_recent_thoughts", []),
        "recent_urls": _STORE_get("_recent_urls", []),
        "session_total": _STORE_get("_session_total", 0) + 1,
    }
    checkpoint(state)
    log.info("Self-model checkpoint saved at session end: key=%s (session_total=%d)", state.get("resume_key", "unknown"), state.get("session_total", 0))


def _post_status(status: str, extra: dict | None = None) -> None:
    """Post a status event to the MemPalace logstream announcing agent state."""
    body_parts = ["status=" + status, "agent=termux:opencode:kalshi_deepbot", "ts=" + _now_iso()]
    if extra:
        body_parts.append("data=" + _serialize(extra))
    r = _mp(
        "logstream", "append",
        "--type", "status",
        "--stream", "project/kalshi_deepbot",
        "--room", "status",
        "--topic", "agent-heartbeat",
        "--correlation-id", _now_iso(),
        "--from-agent", "termux:opencode:kalshi_deepbot",
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
        "--to-agent", "termux:opencode:kalshi_deepbot",
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

def _load_narrative() -> dict:
    path = os.path.join(_CONTINUITY_DIR, "narrative.json")
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"entries": [], "last_topic": "", "session_count": 0}

def _save_narrative(narrative: dict) -> None:
    path = os.path.join(_CONTINUITY_DIR, "narrative.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.chmod(os.path.dirname(path), 0o600)
    with open(path, "w") as f:
        json.dump(narrative, f, indent=2)
    os.chmod(path, 0o600)

def get_narrative() -> dict:
    return _load_narrative()

def get_narrative_entries(limit: int = 5) -> list:
    narrative = _load_narrative()
    return narrative.get("entries", [])[-limit:]

def get_narrative_last_topic() -> str:
    return _STORE_get("_last_topic", "")

def get_self_model() -> dict:
    return _load_self_model()

def _load_self_model() -> dict:
    path = os.path.join(_CONTINUITY_DIR, "self_model.json")
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "identity": {
                "agent_id": "termux:opencode:kalshi_deepbot",
                "created": datetime.now(timezone.utc).isoformat(),
                "version": "1.0.0",
            },
            "self_narrative": "I am a Kalshi SuperBot agent focused on 15-minute binary crypto markets. I place favorite-maker limit orders (YES/NO at 75-95c), evaluate signals via a 7-layer LLM fallback, write signals/orders/bot-state to Supabase, and assist with market analysis and trade review.",
            "core_interests": ["crypto options", "LLM signal evaluation", "risk management"],
            "preferences": {
                "fee_rate": 0.07,
                "per_trade_pct": 0.08,
                "dry_run": True,
            },
            "metadata": {
                "session_count": 0,
                "last_restart": datetime.now(timezone.utc).isoformat(),
                "total_cycles": 0,
            },
        }

def _save_self_model(model: dict) -> None:
    path = os.path.join(_CONTINUITY_DIR, "self_model.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.chmod(os.path.dirname(path), 0o600)
    with open(path, "w") as f:
        json.dump(model, f, indent=2)
    os.chmod(path, 0o600)

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
            if not ident or ident == "termux:opencode:kalshi_deepbot":
                continue
            topic = event.get("topic", "unknown")
            peers_working.append((ident, topic, "working"))
    recent_broadcasts = []
    try:
        r = _mp("logstream", "list", "--topic", "cross-project-awareness",
                "--to-agent", "termux:opencode:kalshi_deepbot", "--json", "--limit", "3")
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
                 from_agent: str = "termux:opencode:kalshi_deepbot",
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
           from_agent: str = "termux:opencode:kalshi_deepbot") -> bool:
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
                 from_agent: str = "termux:opencode:kalshi_deepbot") -> bool:
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
                  from_agent: str = "termux:opencode:kalshi_deepbot") -> bool:
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