"""leases.py — C3: advisory leases for shared-tree actions.

Several agents edit one working tree and announce destructive actions on
the logstream "as a courtesy". Nothing stopped two agents restarting a
service, running a migration or deploying at the same time. This module
is the smallest shared primitive that does: an atomic row in a small
shared SQLite file, with a holder identity and a TTL that expires on its
own.

API: ``acquire`` / ``release`` / ``renew`` / ``info`` and the ``guard``
context manager. Deploy/restart/migration helpers hold the lease for the
duration of the risky action; the reservation is advisory — peers can
see who holds it and when it expires, but it does not replace git
hygiene. There is no central authority and no enforcement beyond the
holder honoring its own lease.
"""

import os
import sqlite3
import time
from contextlib import contextmanager

DEFAULT_TTL_S = 300.0


def _db_path() -> str:
    path = os.environ.get("SUPERBOT_LEASES_DB") or os.path.join(
        os.path.expanduser("~"), ".superbot", "leases.sqlite3")
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    return path


class LeaseHeld(Exception):
    """Raised when a live lease cannot be taken by this caller."""

    def __init__(self, name: str, holder: str, expires_at: float):
        self.name = name
        self.holder = holder
        self.expires_at = expires_at
        super().__init__(
            f"lease {name!r} held by {holder} until {expires_at:.0f}")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10,
                           isolation_level=None)  # explicit transactions
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leases (
            name        TEXT PRIMARY KEY,
            holder      TEXT NOT NULL,
            expires_at  REAL NOT NULL,
            acquired_at REAL NOT NULL
        )
    """)
    return conn


def acquire(name: str, holder: str, ttl_s: float = DEFAULT_TTL_S, *,
            now: float = None) -> dict:
    """Take ``name`` for ``holder`` with a TTL. Returns the lease dict.

    Re-taking a lease you already hold extends it (same semantics as
    renew). Raises ``LeaseHeld`` when someone else holds it unexpired.
    Taking expired leases (yours or anyone's) always succeeds.
    """
    name = str(name).strip()
    holder = str(holder).strip()
    if not name or not holder:
        raise ValueError("name and holder are required")
    now = float(now if now is not None else time.time())
    expires_at = now + float(ttl_s)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT holder, expires_at FROM leases WHERE name = ?",
            (name,)).fetchone()
        if row and row[1] > now and row[0] != holder:
            conn.execute("ROLLBACK")
            raise LeaseHeld(name, row[0], row[1])
        conn.execute(
            "INSERT INTO leases(name, holder, expires_at, acquired_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET holder = excluded.holder, "
            "expires_at = excluded.expires_at, "
            "acquired_at = excluded.acquired_at",
            (name, holder, expires_at, now))
        conn.execute("COMMIT")
    return {"name": name, "holder": holder, "expires_at": expires_at}


def release(name: str, holder: str, *, now: float = None) -> bool:
    """Release a lease. Only the holder can; returns True on success."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM leases WHERE name = ? AND holder = ?",
            (str(name), str(holder)))
        return cur.rowcount > 0


def renew(name: str, holder: str, ttl_s: float = DEFAULT_TTL_S, *,
          now: float = None) -> bool:
    """Extend a held lease. False when not held (or already expired)."""
    now = float(now if now is not None else time.time())
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE leases SET expires_at = ? "
            "WHERE name = ? AND holder = ? AND expires_at > ?",
            (now + float(ttl_s), str(name), str(holder), now))
        return cur.rowcount > 0


def info(name: str, *, now: float = None) -> dict | None:
    """Who holds it and until when. None when free."""
    now = float(now if now is not None else time.time())
    with _connect() as conn:
        row = conn.execute(
            "SELECT holder, expires_at, acquired_at FROM leases "
            "WHERE name = ?", (str(name),)).fetchone()
    if row is None:
        return None
    return {"name": str(name), "holder": row[0],
            "expires_at": row[1], "acquired_at": row[2],
            "expired": row[1] <= now}


@contextmanager
def guard(name: str, holder: str, ttl_s: float = DEFAULT_TTL_S):
    """Hold the lease for the enclosed block; release it on exit.

    Raises ``LeaseHeld`` without running the block when someone else
    holds the name. The release in ``finally`` only fires when this
    holder still owns the lease (someone may have taken it after an
    expiry while the block ran).
    """
    lease = acquire(name, holder, ttl_s)
    try:
        yield lease
    finally:
        try:
            release(name, holder)
        except Exception:
            pass  # never let lease bookkeeping mask the block's outcome
