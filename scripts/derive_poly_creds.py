#!/usr/bin/env python3
"""Derive Polymarket CLOB L2 API credentials from an L1 wallet key.

Run this interactively, once. It must never be run by an unattended agent:
deriving creds is an authenticated action against your Polymarket account.

The L1 private key is read from POLYMARKET_PRIVATE_KEY in the shell
environment, or from the .env file's value of the same name. It is never
accepted as a command-line argument (argv is visible in `ps`), and it is
never printed.

Output goes to .env as POLY_API_KEY / POLY_API_SECRET / POLY_API_PASSPHRASE.
Existing lines are preserved; only those three keys are replaced.

Usage:
    ./scripts/derive_poly_creds.py --confirm              # write to .env
    ./scripts/derive_poly_creds.py --confirm --from-env   # key only from shell env
    ./scripts/derive_poly_creds.py --confirm --force      # replace existing creds

Proxy wallets (funder): set POLYMARKET_FUNDER_ADDRESS and
POLYMARKET_SIGNATURE_TYPE (1 = email/magic proxy, 2 = browser wallet proxy).
Unset = bare EOA, type 0. A wrong combination derives creds that belong to a
DIFFERENT wallet, so the resolved host/funder/type are printed for you to
check before the request goes out.
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
HOST = os.environ.get("POLYMARKET_HOST", "https://clob.polymarket.com").strip()
CHAIN_ID = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))

KEY_NAMES = ("POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE")


def read_env_file(path):
    """Parse .env with the same rules as config.py (quotes, export prefix)."""
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def write_env_file(path, updates):
    """Atomically replace the given keys in .env, preserving every other line."""
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    out = []
    for line in lines:
        stripped = line.strip()
        probe = stripped[len("export "):] if stripped.startswith("export ") else stripped
        key = probe.partition("=")[0].strip() if "=" in probe else ""
        if key in updates:
            if key in seen:
                continue  # drop duplicate definition, keep the first slot
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(out) + "\n")
    tmp.chmod(0o600)
    os.replace(tmp, path)


def make_client(key):
    from py_clob_client_v2 import ClobClient

    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0") or 0)
    if not funder:
        sig_type = 0
    return ClobClient(
        HOST,
        CHAIN_ID,
        key=key,
        signature_type=sig_type,
        funder=funder or None,
    ), funder, sig_type


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true",
                    help="required: acknowledge this writes credentials to .env")
    ap.add_argument("--from-env", dest="from_env", action="store_true",
                    help="read the key only from the shell environment, ignore .env")
    ap.add_argument("--force", action="store_true", help="re-derive even if .env has creds")
    args = ap.parse_args()

    if not args.confirm:
        sys.exit(
            "Refusing to run unattended.\n"
            "This derives live Polymarket API credentials for your account.\n"
            "Re-run with --confirm to derive and write them to .env."
        )

    file_env = {} if args.from_env else read_env_file(ENV_FILE)

    existing = {k: (file_env.get(k) or os.environ.get(k) or "").strip() for k in KEY_NAMES}
    have = [k for k, v in existing.items() if v]
    if have and not args.force:
        print("Already present in .env or env: " + ", ".join(have))
        print("Run with --force to replace.")
        return 0

    key = os.environ.get("POLYMARKET_PRIVATE_KEY") or file_env.get("POLYMARKET_PRIVATE_KEY") or ""
    if not key:
        sys.exit("POLYMARKET_PRIVATE_KEY is not set (shell env or .env); nothing to do.")

    # Validate the shape before the library ever sees it. A bad key otherwise
    # dies inside eth_account as a raw traceback, which reads like a tool bug
    # rather than the one thing the operator can fix. Never echo the value.
    hex_body = key[2:] if key.startswith("0x") else key
    if len(hex_body) != 64 or any(c not in "0123456789abcdefABCDEF" for c in hex_body):
        sys.exit(
            "POLYMARKET_PRIVATE_KEY is malformed: expected 64 hex characters, "
            "optionally prefixed with 0x (got %d characters%s). Nothing written."
            % (len(hex_body), ", non-hex present" if len(hex_body) == 64 else "")
        )

    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS") or file_env.get("POLYMARKET_FUNDER_ADDRESS", "")
    sig = os.environ.get("POLYMARKET_SIGNATURE_TYPE") or file_env.get("POLYMARKET_SIGNATURE_TYPE", "")
    os.environ.setdefault("POLYMARKET_FUNDER_ADDRESS", funder.strip())
    os.environ.setdefault("POLYMARKET_SIGNATURE_TYPE", sig.strip())

    try:
        client, funder, sig_type = make_client(key)
    except ImportError:
        sys.exit("Missing dependency. Install: python -m pip install -q py-clob-client-v2")

    print(f"host={HOST} chain_id={CHAIN_ID} funder={funder or '(bare EOA)'} "
          f"signature_type={sig_type} key_len={len(key)}")

    creds = client.create_or_derive_api_key()
    ok = getattr(creds, "api_key", None) and getattr(creds, "api_secret", None) \
        and getattr(creds, "api_passphrase", None)
    if not ok:
        sys.exit("CLOB returned incomplete creds; nothing written.")

    write_env_file(ENV_FILE, {
        "POLY_API_KEY": creds.api_key,
        "POLY_API_SECRET": creds.api_secret,
        "POLY_API_PASSPHRASE": creds.api_passphrase,
    })

    print("Wrote", ENV_FILE)
    print("  POLY_API_KEY        =", creds.api_key[:8] + "...", f"(len {len(creds.api_key)})")
    print("  POLY_API_SECRET     = (hidden, len", len(creds.api_secret), ")")
    print("  POLY_API_PASSPHRASE = (hidden, len", len(creds.api_passphrase), ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
