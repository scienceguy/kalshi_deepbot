#!/usr/bin/env python3
"""Insert credentials into the n8n database using n8n's own encryption.

Secrets are read at runtime from the same sources the bot uses
(environment variables, the local .env, and ~/.secrets/* key files).
NOTHING is hardcoded here — never commit a key to this file.
"""

import base64
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import config


def load_encryption_key():
    config_path = os.environ.get("N8N_CONFIG_PATH", "/data/data/com.termux/files/home/.n8n/config")
    with open(config_path) as f:
        n8n_config = json.load(f)
    encryption_key = n8n_config["encryptionKey"]
    enc_key = base64.b64decode(encryption_key)
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"n8n-encryption-key",
    )
    return hkdf.derive(enc_key)


def encrypt_value(plaintext: str, encryption_key_bytes: bytes) -> str:
    """Encrypt a value using AES-256-GCM like n8n does."""
    aesgcm = AESGCM(encryption_key_bytes)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    # n8n stores as: nonce + ciphertext, base64 encoded
    result = nonce + ciphertext
    return base64.b64encode(result).decode()


def insert_credential(conn, name, cred_type, data_dict, encryption_key_bytes: bytes):
    """Insert a credential into the database."""
    encrypted_data = {k: encrypt_value(str(v), encryption_key_bytes) for k, v in data_dict.items()}
    data_json = json.dumps(encrypted_data)

    cred_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat() + "Z"

    conn.execute(
        """INSERT INTO credentials_entity
           (id, name, data, type, createdAt, updatedAt, isManaged, isGlobal, isResolvable, usageScope)
           VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 'project')""",
        (cred_id, name, data_json, cred_type, now, now),
    )
    conn.commit()
    print(f"✓ Inserted credential: {name} (type: {cred_type}, id: {cred_id[:8]}...)")


def _load_kalshi_pem() -> str:
    """Load the Kalshi RSA private key from the configured key file (never hardcoded)."""
    key_file = config.KALSHI_KEY_FILE
    if key_file and Path(key_file).expanduser().exists():
        return Path(key_file).expanduser().read_text().strip()
    content = config.KALSHI_PRIVATE_KEY_CONTENT
    if content:
        return content.replace("\\n", "\n").strip()
    return ""


def _load_supabase_key() -> str:
    """Load the Supabase service key from the configured key file or env."""
    key_file = os.environ.get("SUPABASE_KEY_FILE", "")
    if key_file and Path(key_file).expanduser().exists():
        return Path(key_file).expanduser().read_text().strip()
    return os.environ.get("SUPABASE_SERVICE_KEY", "") or os.environ.get("SUPABASE_API_KEY", "")


def build_credentials() -> list:
    """Build the credential list from runtime config; skip any that are missing."""
    kalshi_key_id = config.KALSHI_KEY_ID
    kalshi_pem = _load_kalshi_pem()
    supabase_key = _load_supabase_key()

    creds = []
    if kalshi_key_id:
        creds.append({
            "name": "Kalshi API Key",
            "type": "httpHeaderAuth",
            "data": {"name": "KALSHI-ACCESS-KEY", "value": kalshi_key_id},
        })
    else:
        print("⚠  KALSHI_KEY_ID missing — skipping Kalshi API Key credential")

    if kalshi_pem:
        creds.append({
            "name": "Kalshi RSA Key",
            "type": "httpHeaderAuth",
            "data": {"name": "KALSHI_PRIVATE_KEY", "value": kalshi_pem},
        })
    else:
        print("⚠  Kalshi private key missing — skipping Kalshi RSA Key credential")

    if supabase_key:
        creds.append({
            "name": "Supabase API Key",
            "type": "httpHeaderAuth",
            "data": {"name": "apikey", "value": supabase_key},
        })
    else:
        print("⚠  Supabase service key missing — skipping Supabase API Key credential")

    return creds


def main():
    credentials = build_credentials()
    if not credentials:
        print("✗ No credentials available — check .env and ~/.secrets/*")
        return

    encryption_key_bytes = load_encryption_key()
    conn = sqlite3.connect("/data/data/com.termux/files/home/.n8n/database.sqlite")
    try:
        for cred in credentials:
            try:
                insert_credential(conn, cred["name"], cred["type"], cred["data"], encryption_key_bytes)
            except Exception as e:
                print(f"✗ Failed to insert {cred['name']}: {e}")
    finally:
        conn.close()
    print("\nDone!")


if __name__ == "__main__":
    main()
