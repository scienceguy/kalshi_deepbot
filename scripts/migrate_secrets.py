#!/usr/bin/env python3
"""Migrate inline secrets from .env to ~/.secrets/ files.

Reads .env, finds inline secret values for known keys, writes them to
~/.secrets/ with chmod 600, and updates .env to reference the file paths.
Prints NO secret values — only key names.
"""
import os
import stat
import tempfile
from pathlib import Path

env_path = Path(".env")
secrets_dir = Path(os.path.expanduser("~/.secrets"))

if not env_path.exists():
    print(f"ERROR: {env_path} not found — nothing to migrate.")
    raise SystemExit(1)

# Inline keys -> secrets file name -> env var for file path
MIGRATIONS = [
    ("SUPABASE_SERVICE_KEY", "supabase_service_key", "SUPABASE_KEY_FILE"),
    ("NVIDIA_API_KEY", "nvidia_api_key", "NVIDIA_API_KEY_FILE"),
    ("KALSHIEYE_API_KEY", "kalsheye_api_key", "KALSHIEYE_API_KEY_FILE"),
]


def strip_inline_comment(value: str) -> str:
    """Remove an unquoted trailing # comment from a .env value."""
    in_single = in_double = False
    for i, ch in enumerate(value):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return value[:i].strip()
    return value.strip()


def clean_value(raw: str) -> str:
    """Strip surrounding quotes and inline comments from a .env value."""
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        v = v[1:-1]
    return strip_inline_comment(v)


# Ensure ~/.secrets/ is 700 regardless of umask or pre-existing perms.
secrets_dir.mkdir(mode=0o700, exist_ok=True)
secrets_dir.chmod(0o700)

lines = env_path.read_text().splitlines()
new_lines: list[str] = []
migrated_keys: set[str] = set()
migrated_file_vars: set[str] = set()

for line in lines:
    done = False
    for inline_key, secret_name, file_env_var in MIGRATIONS:
        if line.startswith(f"{inline_key}=") and not line.startswith(f"{inline_key}_FILE"):
            value = clean_value(line.split("=", 1)[1])
            if value:
                secret_path = secrets_dir / secret_name
                secret_path.write_text(value)
                secret_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
                new_lines.append(f"{file_env_var}={secret_path}")
                migrated_keys.add(inline_key)
                migrated_file_vars.add(file_env_var)
                done = True
                break
    if not done:
        new_lines.append(line)

# Drop duplicate _FILE lines: keep only the first occurrence of each file env var.
deduped: list[str] = []
seen_file_vars: set[str] = set()
for line in new_lines:
    is_file_var = False
    for _, _, fv in MIGRATIONS:
        if line.startswith(f"{fv}="):
            is_file_var = True
            break
    if is_file_var:
        fv = line.split("=", 1)[0]
        if fv in seen_file_vars:
            continue  # skip duplicate
        seen_file_vars.add(fv)
    deduped.append(line)

# Atomic write: temp file in same dir, then os.replace.
tmp_fd, tmp_path = tempfile.mkstemp(dir=env_path.parent, prefix=".env.", suffix=".tmp")
try:
    with os.fdopen(tmp_fd, "w") as fh:
        fh.write("\n".join(deduped) + "\n")
    os.replace(tmp_path, env_path)
except Exception:
    os.unlink(tmp_path)
    raise

print(f"Migrated {len(migrated_keys)} secrets: {sorted(migrated_keys)}")
