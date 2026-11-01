#!/usr/bin/env python3
"""Insert credentials into n8n database using n8n's encryption."""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
import base64

def load_encryption_key():
    config_path = os.environ.get("N8N_CONFIG_PATH", "/data/data/com.termux/files/home/.n8n/config")
    with open(config_path) as f:
        config = json.load(f)
    encryption_key = config["encryptionKey"]
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
    # Encrypt each value in the data dict
    encrypted_data = {}
    for k, v in data_dict.items():
        encrypted_data[k] = encrypt_value(str(v), encryption_key_bytes)
    
    data_json = json.dumps(encrypted_data)
    
    cred_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat() + "Z"
    
    conn.execute(
        """INSERT INTO credentials_entity 
           (id, name, data, type, createdAt, updatedAt, isManaged, isGlobal, isResolvable, usageScope) 
           VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 'project')""",
        (cred_id, name, data_json, cred_type, now, now)
    )
    conn.commit()
    print(f"✓ Inserted credential: {name} (type: {cred_type}, id: {cred_id[:8]}...)")

# Credentials to add from Bitwarden
# Replace placeholder values with your actual credentials before running.
credentials = [
    # Kalshi API Key - HTTP Header Auth
    {
        "name": "Kalshi API Key",
        "type": "httpHeaderAuth",
        "data": {
            "name": "KALSHI-ACCESS-KEY",
            "value": "your_kalshi_key_id_here"
        }
    },
    # Kalshi RSA Private Key - Secret
    {
        "name": "Kalshi RSA Key",
        "type": "n8n-nodes-base.httpHeaderAuth",
        "data": {
            "name": "KALSHI_PRIVATE_KEY",
            "value": "your_kalshi_private_key_here"
        }
    },
    # Gemini API Key - HTTP Header Auth
    {
        "name": "Gemini API Key",
        "type": "httpHeaderAuth",
        "data": {
            "name": "Authorization",
            "value": "Bearer your_gemini_api_key_here"
        }
    },
    # OpenAI API Key - HTTP Header Auth
    {
        "name": "OpenAI API Key",
        "type": "httpHeaderAuth",
        "data": {
            "name": "Authorization",
            "value": "Bearer your_openai_api_key_here"
        }
    },
    # Groq API Key - HTTP Header Auth
    {
        "name": "Groq API Key",
        "type": "httpHeaderAuth",
        "data": {
            "name": "Authorization",
            "value": "Bearer your_groq_api_key_here"
        }
    },
    # Supabase API Key - HTTP Header Auth
    {
        "name": "Supabase API Key",
        "type": "httpHeaderAuth",
        "data": {
            "name": "apikey",
            "value": "your_supabase_api_key_here"
        }
    },
]

encryption_key_bytes = load_encryption_key()
conn = sqlite3.connect("/data/data/com.termux/files/home/.n8n/database.sqlite")
try:
    for cred in credentials:
        try:
            insert_credential(conn, cred["name"], cred["type"], cred["data"])
        except Exception as e:
            print(f"✗ Failed to insert {cred['name']}: {e}")
finally:
    conn.close()
print("\nDone!")
