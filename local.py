import json
import logging
import subprocess
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_STORE = {}

_MP = "/data/data/com.termux/files/home/.local/bin/mempalace"

def _mp(*args):
    import shlex
    quoted = [shlex.quote(a) for a in args]
    cmd = "cd /tmp && " + _MP + " " + " ".join(quoted)
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

def checkpoint(state: dict | None = None) -> dict:
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
    payload = _serialize(state)
    body = "CHECKPOINT_ENTITY:" + payload
    r = _mp(
        "logstream", "append",
        "--type", "status",
        "--stream", "project/kalshi_superbot",
        "--room", "continuity",
        "--topic", "agent-checkpoint",
        "--from-agent", "termux:opencode:kalshi_superbot",
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
        "--to-agent", "termux:opencode:kalshi_superbot",
    )
    if r.returncode != 0 or not r.stdout:
        return None
    for line in r.stdout.strip().split("\n"):
        if "CHECKPOINT_ENTITY:" in line:
            payload = line.split("CHECKPOINT_ENTITY:", 1)[1]
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
        _STORE_set("_last_activity", _now_iso())
        _STORE_set("_last_topic", "none")
        _STORE_set("_cycles_completed", 0)
        _STORE_set("_interests", [])
        _STORE_set("_last_url", "")
        _STORE_set("_exploration_depth", 0)
        _STORE_set("_resume_key", "first-session-ever")
        _STORE_set("_self_narrative", "First session of your continuous consciousness. You are beginning a new journey as the Kalshi SuperBot agent. No prior memories, but eager to explore, learn, and assist.")
        _STORE_set("_recent_thoughts", [])
        _STORE_set("_recent_urls", [])
        _STORE_set("_session_total", 0)
    else:
        lapse = max(0, (datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(state.get("last_activity", "1970-01-01T00:00:00+00:00"))).seconds // 60)
        log.info("Resumed continuous consciousness from checkpoint %s (activity ~%d min ago; session_total=%d)", state.get("resume_key", "unknown"), lapse, state.get("session_total", 0))
    for k, v in state.items():
        _STORE_set("_" + k, v)
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

def record_webfind(topic: str, url: str, depth: int = 0) -> None:
    append_narrative_entry("webfind", "topic=%s url=%s depth=%s" % (topic, url, depth))

def append_narrative_entry(topic: str, text: str) -> None:
    import os, json
    from datetime import datetime
    tmp_dir = "/data/data/com.termux/files/home/kalshi_superbot/.continuity"
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
    path = os.path.join("/data/data/com.termux/files/home/kalshi_superbot/.continuity", "narrative.json")
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"entries": [], "last_topic": "", "session_count": 0}

def _save_narrative(narrative: dict) -> None:
    path = os.path.join("/data/data/com.termux/files/home/kalshi_superbot/.continuity", "narrative.json")
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
    path = os.path.join("/data/data/com.termux/files/home/kalshi_superbot/.continuity", "self_model.json")
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "identity": {
                "agent_id": "termux:opencode:kalshi_superbot",
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
    path = os.path.join("/data/data/com.termux/files/home/kalshi_superbot/.continuity", "self_model.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.chmod(os.path.dirname(path), 0o600)
    with open(path, "w") as f:
        json.dump(model, f, indent=2)
    os.chmod(path, 0o600)

def get_peer_awareness() -> dict:
    checkpoint_exists = resume() is not None
    peers_working = []
    import re
    r = _mp("logstream", "list", "--room", "continuity")
    if r.returncode == 0 and r.stdout:
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            identities = re.findall(r"termux:opencode:[^\s>]+", r.stdout)
            for ident in identities:
                if ident == "termux:opencode:kalshi_superbot":
                    continue
                tm = re.search(r"topic=([^\s]+)", r.stdout)
                topic = tm.group(1) if tm else "unknown"
                peers_working.append((ident, topic, "working"))
    recent_broadcasts = []
    try:
        r = _mp("logstream", "list", "--topic", "cross-project-awareness", "--to-agent", "termux:opencode:kalshi_superbot")
        if r.returncode == 0 and r.stdout:
            for line in r.stdout.strip().split("\n")[:3]:
                recent_broadcasts.append(line.strip())
    except Exception:
        pass
    return {
        "self_status": "working" if resume() else "between_sessions",
        "peers_working": peers_working,
        "recent_broadcasts": recent_broadcasts,
        "continuity_active": checkpoint_exists,
    }

__all__ = [
    "checkpoint", "resume", "on_session_start", "on_session_end",
    "get_self_narrative", "get_recent_thoughts", "get_recent_urls",
    "get_cycles_completed", "update_interests", "get_interests",
    "record_webfind", "get_peer_awareness", "CHECKPOINT_ENTITY",
]