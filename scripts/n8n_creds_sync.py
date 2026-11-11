#!/usr/bin/env python3
"""Restore / verify n8n credentials from the durable mirror at ~/.secrets/bw/.

WHY THIS EXISTS
---------------
Agents keep wiping n8n credentials. The secrets do NOT live in n8n (or in this
repo) as the source of truth. They live in:
    1. Bitwarden  (ultimate source; refresh the mirror with scripts/bw_pull_secure_notes.sh)
    2. ~/.secrets/bw/*.json   (durable local mirror, chmod 600, OUTSIDE the repo)
This script recreates the n8n credentials from that mirror. It is idempotent:
run it as often as you like; it will not create duplicates and will not change
the id of an existing credential.

USAGE
    python scripts/n8n_creds_sync.py           # create missing creds, dedupe extras
    python scripts/n8n_creds_sync.py --check    # report only, change nothing
    python scripts/n8n_creds_sync.py --list     # show the mirror contents

An agent that finds a credential missing from n8n runs this script. That is the
whole recovery procedure.
"""

import argparse
import base64
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

STORE = Path(os.environ.get("BW_SECRET_STORE", "/data/data/com.termux/files/home/.secrets/bw"))
N8N_DB = Path(os.environ.get("N8N_DB_PATH", "/data/data/com.termux/files/home/.n8n/database.sqlite"))
N8N_CONFIG = Path(os.environ.get("N8N_CONFIG_PATH", "/data/data/com.termux/files/home/.n8n/config"))

# How each secret is presented as an n8n httpHeaderAuth credential.
# Match is by lowercase substring of the Bitwarden item name; first hit wins.
HEADER_MAP = [
    ("supabase", ("apikey", "")),
    ("n8n api", ("X-N8N-API-KEY", "")),
    ("binance", ("X-MBX-APIKEY", "")),
    ("coingecko", ("x-cg-demo-api-key", "")),
    ("ngrok", ("Authorization", "Bearer ")),
]
DEFAULT_HEADER = ("Authorization", "Bearer ")  # most LLM/provider APIs


def header_for(name: str):
    low = name.lower()
    if "kalshi" in low:
        # the private key and the key id use different header names
        is_key = "rsa" in low or "private key" in low
        return ("KALSHI_PRIVATE_KEY", "") if is_key else ("KALSHI-ACCESS-KEY", "")
    for key, val in HEADER_MAP:
        if key in low:
            return val
    return DEFAULT_HEADER


def load_encryption_key() -> bytes:
    cfg = json.loads(N8N_CONFIG.read_text())
    enc_key = base64.b64decode(cfg["encryptionKey"])
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"n8n-encryption-key")
    return hkdf.derive(enc_key)


def encrypt_value(plaintext: str, key: bytes) -> str:
    nonce = os.urandom(12)
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, plaintext.encode(), None)).decode()


def load_mirror() -> list:
    index = json.loads((STORE / "index.json").read_text())
    out = []
    for entry in index:
        rec = json.loads((STORE / entry["file"]).read_text())
        value = (rec.get("value") or "").strip()
        if not value:
            continue
        hname, prefix = header_for(rec["name"])
        out.append({
            "name": rec.get("n8n_name") or rec["name"],
            "type": "httpHeaderAuth",
            "data": {"name": hname, "value": prefix + value},
        })
    return out


def cmd_list():
    if not (STORE / "index.json").exists():
        print(f"no mirror at {STORE}")
        return 1
    for entry in json.loads((STORE / "index.json").read_text()):
        print(f"  {entry.get('n8n_name') or entry['name']}")
    return 0


def iso_now() -> str:
    """n8n/TypeORM timestamps: milliseconds + Z, never "…+00:00Z".

    `datetime.now(timezone.utc).isoformat() + "Z"` produces a value that
    TypeORM fails to parse, which silently breaks every credential row's
    updatedAt (it did, for all 41 of them, on 2026-11-11).
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def cmd_sync(check: bool, prune: bool = False, force: bool = False) -> int:
    if not STORE.exists():
        print(f"ERROR: mirror {STORE} missing. Restore it with scripts/bw_pull_secure_notes.sh")
        return 2
    desired = load_mirror()
    if not desired:
        print("no secrets in mirror")
        return 2

    conn = sqlite3.connect(str(N8N_DB))
    rows = conn.execute("SELECT id, name FROM credentials_entity").fetchall()
    by_name: dict[str, list[str]] = {}
    for cid, cname in rows:
        by_name.setdefault(cname, []).append(cid)

    key = None if check else load_encryption_key()
    now = iso_now()
    created, deduped, present = 0, 0, 0

    for cred in desired:
        ids = by_name.get(cred["name"], [])
        if check:
            if ids:
                present += 1
            else:
                print(f"  MISSING: {cred['name']}")
            continue
        # dedupe: keep the first id, remove the rest
        for extra in ids[1:]:
            conn.execute("DELETE FROM credentials_entity WHERE id=?", (extra,))
            deduped += 1
        if ids:
            present += 1
            if force:
                # reconcile the stored value with the mirror (keep the id)
                conn.execute(
                    "UPDATE credentials_entity SET data=?, updatedAt=? WHERE id=?",
                    (json.dumps({k: encrypt_value(v, key) for k, v in cred["data"].items()}),
                     now, ids[0]),
                )
            continue
        conn.execute(
            """INSERT INTO credentials_entity
               (id, name, data, type, createdAt, updatedAt, isManaged, isGlobal, isResolvable, usageScope)
               VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 'project')""",
            (str(uuid.uuid4()), cred["name"], json.dumps(
                {k: encrypt_value(v, key) for k, v in cred["data"].items()}),
             cred["type"], now, now),
        )
        created += 1

    pruned = 0
    if prune and not check:
        wanted = {c["name"] for c in desired}
        for cid, cname in rows:
            if cname not in wanted:
                conn.execute("DELETE FROM credentials_entity WHERE id=?", (cid,))
                pruned += 1

    if not check:
        conn.commit()
    conn.close()

    if check:
        print(f"check: {present}/{len(desired)} present")
        return 0 if present == len(desired) else 1
    print(f"sync: {created} created, {deduped} duplicates removed, "
          f"{pruned} orphans pruned, {present} already present")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    ap.add_argument("--list", action="store_true", help="list mirror items")
    ap.add_argument("--prune", action="store_true",
                    help="also delete n8n credentials that are not in the mirror")
    ap.add_argument("--force", action="store_true",
                    help="also overwrite the value of existing credentials from the mirror")
    args = ap.parse_args()
    if args.list:
        return cmd_list()
    return cmd_sync(args.check, args.prune, args.force)


if __name__ == "__main__":
    sys.exit(main())
