#!/usr/bin/env python3
"""Refresh the durable credential mirror at ~/.secrets/bw/ from Bitwarden.

Pulls every Bitwarden "secure note" (plus the local Supabase service key file)
and writes one protected JSON per item plus an index. This is the only step that
touches Bitwarden; after it runs, scripts/n8n_creds_sync.py can restore n8n
credentials offline, any number of times, with no vault access.

Auth: uses BW_SESSION if already unlocked, else unlocks with BW_PASSWORD.
      The master password is never stored by this script.

USAGE
    BW_PASSWORD='...' python scripts/bw_pull_secure_notes.py
    python scripts/bw_pull_secure_notes.py          # uses existing BW_SESSION
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

STORE = Path(os.environ.get("BW_SECRET_STORE", "/data/data/com.termux/files/home/.secrets/bw"))
SUPABASE_KEY_FILE = Path(os.environ.get("SUPABASE_KEY_FILE", "/data/data/com.termux/files/home/.secrets/supabase_service_key"))
# Bitwarden item name -> n8n credential name, when they differ.
NAME_ALIASES = {"Supabase Service Key": "Supabase API Key"}


def bw(*args, session=None):
    env = dict(os.environ)
    if session:
        env["BW_SESSION"] = session
    out = subprocess.run(["bw", *args], capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise RuntimeError(f"bw {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def get_session() -> str:
    sess = os.environ.get("BW_SESSION", "").strip()
    if sess:
        return sess
    pwd = os.environ.get("BW_PASSWORD", "")
    if not pwd:
        sys.exit("ERROR: set BW_SESSION (unlocked) or BW_PASSWORD (to unlock)")
    return bw("unlock", "--passwordenv", "BW_PASSWORD", "--raw").strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "item"


def main() -> int:
    try:
        subprocess.run(["bw", "sync"], capture_output=True, text=True,
                       env={**os.environ, "BW_SESSION": os.environ.get("BW_SESSION", "")})
    except FileNotFoundError:
        sys.exit("ERROR: bw CLI not found")
    session = get_session()
    items = json.loads(bw("list", "items", "--session", session) or "[]")
    notes = [i for i in items if i.get("type") == 2]

    records = [(n["name"].strip(), n["id"], (n.get("notes") or "").strip()) for n in notes]
    if SUPABASE_KEY_FILE.exists():
        records.append(("Supabase Service Key", "local-file", SUPABASE_KEY_FILE.read_text().strip()))

    STORE.mkdir(parents=True, exist_ok=True)
    os.chmod(STORE, 0o700)
    # clear old numbered files so renames don't leave orphans
    for old in STORE.glob("*.json"):
        old.unlink()

    counts, index, used = {}, [], set()
    for name, sid, value in records:
        if not value:
            continue
        counts[name] = counts.get(name, 0) + 1
        n8n_name = name if counts[name] == 1 else f"{name} ({counts[name]})"
        n8n_name = NAME_ALIASES.get(name, n8n_name) if counts[name] == 1 else n8n_name
        fn = slug(name)
        while fn in used:
            fn += "_x"
        used.add(fn)
        (STORE / f"{fn}.json").write_text(json.dumps(
            {"name": name, "n8n_name": n8n_name, "source": sid, "value": value}, indent=2))
        os.chmod(STORE / f"{fn}.json", 0o600)
        index.append({"name": name, "n8n_name": n8n_name, "file": f"{fn}.json"})

    (STORE / "index.json").write_text(json.dumps(index, indent=2))
    os.chmod(STORE / "index.json", 0o600)
    print(f"mirror refreshed: {len(index)} items -> {STORE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
