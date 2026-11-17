#!/usr/bin/env python3
"""
local_db.py — Local SQLite database for Kalshi SuperBot.

Provides a local-first database layer that mirrors Supabase tables:
- signals
- orders
- bot_state
- backtest_runs

All writes go to local SQLite immediately (fast, reliable, offline-capable).
A background sync thread pushes changes to Supabase periodically.

Usage:
    from local_db import LocalDB
    db = LocalDB()
    
    # Write (immediate, local)
    db.insert_signal(signal_data)
    db.set_bot_state("session_pnl", 123.45)
    
    # Read (local)
    state = db.get_bot_state("session_pnl")
    
    # Sync to Supabase (manual or auto via background thread)
    db.sync_to_supabase()
"""

import hashlib
import os
import json
import random
import socket
import sqlite3
import threading
import time
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from pathlib import Path

import config

# Try to import supabase client for sync
try:
    from supabase_client import get_supabase, is_enabled as supabase_is_enabled
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False
    supabase_is_enabled = lambda: False

log = logging.getLogger(__name__)

# Local database path — falls back to SUPERBOT_DATA_DIR (V1), then repo-local
# local_data/ to preserve current behavior when neither is set.
DB_DIR = Path(os.environ.get("SUPERBOT_LOCAL_DB_DIR")
              or config.SUPERBOT_DATA_DIR
              or Path(__file__).resolve().parent / "local_data")
DB_DIR.mkdir(exist_ok=True)


def get_db_path() -> Path:
    """Return mode-isolated database path (live vs dry) unless explicitly overridden."""
    if os.environ.get("SUPERBOT_LOCAL_DB_PATH"):
        return Path(os.environ["SUPERBOT_LOCAL_DB_PATH"])
    mode_suffix = "_dry" if config.DRY_RUN else "_live"
    return DB_DIR / f"superbot_local{mode_suffix}.db"


DB_PATH = get_db_path()

# Sync interval (seconds) — configurable via env
SYNC_INTERVAL = float(config.LOCAL_DB_SYNC_INTERVAL)

# Sync circuit breaker — stops hammering a dead Supabase. D2 hardening.
SYNC_BREAKER_MAX_FAILURES = int(config.LOCAL_DB_SYNC_BREAKER_MAX_FAILURES)
SYNC_BREAKER_COOLDOWN_SECONDS = float(config.LOCAL_DB_SYNC_BREAKER_COOLDOWN_SECONDS)
SYNC_MAX_ROW_ATTEMPTS = int(config.LOCAL_DB_SYNC_MAX_ROW_ATTEMPTS)


class LocalDB:
    """Local SQLite database with background Supabase sync."""

    # Tables the sync thread mirrors to Supabase. PolyLocalDB overrides this
    # with its own table set (D2).
    SYNC_TABLES = ("bot_state", "signals", "orders", "pnl_ledger", "backtest_runs")

    def __init__(self, db_path: str | None = None, auto_sync: bool = True):
        self.db_path = str(db_path or get_db_path())
        self._local = threading.local()
        self._lock = threading.RLock()
        self._sync_thread = None
        self._stop_sync = threading.Event()
        self._auto_sync = auto_sync and supabase_is_enabled()
        # D2: sync circuit breaker state + metrics
        self._sync_breaker_open_until = 0.0
        self._sync_breaker_failures = 0
        self._sync_metrics = {
            "sync_success_count": 0,
            "sync_failure_count": 0,
            "last_sync_ts": 0.0,
            "breaker_opens": 0,
            "breaker_state": "closed",
        }
        
        try:
            self._init_db()
        except sqlite3.DatabaseError as e:
            log.error("Local SQLite DB image corrupted (%s) — backing up and recreating clean database", e)
            corrupt_path = f"{self.db_path}.corrupt.{int(time.time())}"
            try:
                # Close connection before renaming files so file locks are released
                if hasattr(self._local, "conn") and self._local.conn is not None:
                    try:
                        self._local.conn.close()
                    except Exception:
                        pass
                    self._local.conn = None
                if os.path.exists(self.db_path):
                    os.rename(self.db_path, corrupt_path)
                for ext in ("-wal", "-shm"):
                    if os.path.exists(self.db_path + ext):
                        os.rename(self.db_path + ext, corrupt_path + ext)
            except Exception as re:
                log.warning("Could not rename corrupt DB: %s", re)
            self._local = threading.local()
            self._init_db()

        if self._auto_sync:
            self._start_sync_thread()

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create a dedicated connection per thread for true isolation."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            # Enable WAL mode and 30s busy timeout for rock-solid concurrency
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        with self._lock:
            # signals table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    type TEXT,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price_cents INTEGER NOT NULL,
                    edge REAL,
                    model_prob REAL,
                    contracts REAL NOT NULL,
                    reason TEXT,
                    executed INTEGER DEFAULT 0,
                    blocked_reason TEXT,
                    outcome_won INTEGER,
                    outcome_pnl REAL,
                    settled_at TEXT,
                    synced INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            
            # orders table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT DEFAULT 'buy',
                    contracts REAL NOT NULL,
                    price_cents INTEGER NOT NULL,
                    order_id TEXT,
                    status TEXT,
                    fees_dollars REAL,
                    cycles INTEGER,
                    fill_price_cents INTEGER,
                    outcome_won INTEGER,
                    pnl_dollars REAL,
                    settled_at TEXT,
                    synced INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            try:
                conn.execute("ALTER TABLE orders ADD COLUMN action TEXT DEFAULT 'buy'")
            except Exception:
                pass
            
            # bot_state table (key-value with upsert support)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    synced INTEGER DEFAULT 0,
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            
            # backtest_runs table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS backtest_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    ev_per_trade REAL,
                    total_pnl REAL,
                    n_trades INTEGER,
                    win_rate REAL,
                    max_drawdown REAL,
                    final_balance REAL,
                    synced INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)

            # pnl_ledger: idempotent settlement P&L ledger
            # Every settlement P&L is applied exactly once via idempotency_key.
            # Use INSERT ... ON CONFLICT(idempotency_key) DO NOTHING to dedupe.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pnl_ledger (
                    idempotency_key TEXT PRIMARY KEY,
                    market_ticker TEXT NOT NULL,
                    realized_pnl_dollars REAL NOT NULL,
                    fee_dollars REAL NOT NULL DEFAULT 0,
                    applied_at TEXT NOT NULL DEFAULT (datetime('now')),
                    source TEXT NOT NULL DEFAULT 'local',
                    synced INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pnl_ledger_market ON pnl_ledger(market_ticker, applied_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pnl_ledger_synced ON pnl_ledger(synced)")

            # event_outbox: durable outbox for n8n deliveries (N2).
            # critical events are never dropped; telemetry events are drop-oldest
            # when the backlog exceeds a cap. Status: pending | sent | dead.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS event_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    type TEXT NOT NULL,
                    delivery_class TEXT NOT NULL DEFAULT 'critical',
                    payload_json TEXT NOT NULL,
                    venue TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now')),
                    attempts INTEGER DEFAULT 0,
                    next_attempt_at TEXT DEFAULT (datetime('now')),
                    last_error TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    delivered_at TEXT NOT NULL DEFAULT '',
                    webhook_path TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event_outbox_status ON event_outbox(status, next_attempt_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event_outbox_event_id ON event_outbox(event_id)")

            # N4: signed control commands from n8n/operators land here first;
            # the trader drains them at the top of each cycle. UNIQUE(nonce)
            # is the replay guard; signature material is stored so the trader
            # can re-verify before acting.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS control_inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    agent_id TEXT NOT NULL DEFAULT '',
                    nonce TEXT NOT NULL UNIQUE,
                    ts INTEGER NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    signature TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    result TEXT NOT NULL DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_control_inbox_status ON control_inbox(status, created_at)")

            # D3: durable per-row sync bookkeeping + quarantine for poison rows.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_quarantine (
                    table_name TEXT NOT NULL,
                    pk TEXT NOT NULL,
                    row_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    quarantined_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (table_name, pk)
                )
            """)

            # D1: venue/mode/origin_host/schema_version on all activity tables.
            # Added via ALTER TABLE so existing databases are migrated in place.
            for _table, _cols in (
                ("signals", ("venue TEXT DEFAULT '', mode TEXT DEFAULT '', origin_host TEXT DEFAULT '', schema_version TEXT DEFAULT '1'")),
                ("orders", ("venue TEXT DEFAULT '', mode TEXT DEFAULT '', origin_host TEXT DEFAULT '', schema_version TEXT DEFAULT '1'")),
                ("bot_state", ("venue TEXT DEFAULT '', mode TEXT DEFAULT '', origin_host TEXT DEFAULT '', schema_version TEXT DEFAULT '1'")),
                ("pnl_ledger", ("venue TEXT DEFAULT '', mode TEXT DEFAULT '', origin_host TEXT DEFAULT '', schema_version TEXT DEFAULT '1'")),
                ("backtest_runs", ("venue TEXT DEFAULT '', mode TEXT DEFAULT '', origin_host TEXT DEFAULT '', schema_version TEXT DEFAULT '1'")),
                # D3: persistent per-row retry bookkeeping (attempts survive
                # restarts; backoff is enforced in the SELECT, not in memory).
                ("signals", "sync_attempts INTEGER DEFAULT 0, last_error TEXT, next_attempt_at TEXT"),
                ("orders", "sync_attempts INTEGER DEFAULT 0, last_error TEXT, next_attempt_at TEXT"),
                ("bot_state", "sync_attempts INTEGER DEFAULT 0, last_error TEXT, next_attempt_at TEXT"),
                ("pnl_ledger", "sync_attempts INTEGER DEFAULT 0, last_error TEXT, next_attempt_at TEXT"),
                ("backtest_runs", "sync_attempts INTEGER DEFAULT 0, last_error TEXT, next_attempt_at TEXT"),
                # N2/N3: delivery timestamp for outbox metrics, and the webhook
                # path to deliver to (event type alone cannot route).
                ("event_outbox", "delivered_at TEXT NOT NULL DEFAULT ''"),
                ("event_outbox", "webhook_path TEXT NOT NULL DEFAULT ''"),
            ):
                for _col in _cols.split(", "):
                    _col_name = _col.split(" ")[0]
                    try:
                        conn.execute(f"ALTER TABLE {_table} ADD COLUMN {_col}")
                    except Exception:
                        pass  # column already exists

            # Create indexes for common queries
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_synced ON signals(synced)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_ticker ON orders(ticker)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_synced ON orders(synced)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_state_synced ON bot_state(synced)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_backtest_synced ON backtest_runs(synced)")
            
            conn.commit()
            log.info("Local database initialized at %s", self.db_path)

    # ── signals ──────────────────────────────────────────────────────────────────

    def insert_signal(self, signal_data: Dict[str, Any]) -> bool:
        """Insert a new trade signal locally."""
        data = dict(signal_data)
        if not data.get("type"):
            data["type"] = "taker"
        required = ("ts", "ticker", "side", "price_cents", "contracts")
        missing = [f for f in required if f not in data]
        if missing:
            log.warning("insert_signal: missing required fields %s", ", ".join(missing))
            return False
        
        conn = self._get_conn()
        with self._lock:
            try:
                columns = list(data.keys())
                placeholders = ", ".join(["?" for _ in columns])
                values = [data.get(c) for c in columns]
                # Add synced=0
                columns.append("synced")
                placeholders += ", ?"
                values.append(0)
                
                sql = f"INSERT INTO signals ({', '.join(columns)}) VALUES ({placeholders})"
                conn.execute(sql, values)
                conn.commit()
                return True
            except Exception as e:
                log.warning("insert_signal error: %s", e)
                return False

    def insert_signals_batch(self, signals: List[Dict[str, Any]]) -> bool:
        """Insert multiple signals in a single transaction."""
        if not signals:
            return True
        
        required = ("ts", "ticker", "side", "price_cents", "contracts")
        clean = []
        for s in signals:
            item = dict(s)
            if not item.get("type"):
                item["type"] = "taker"
            missing = [f for f in required if f not in item]
            if missing:
                log.warning("insert_signals_batch: skipping signal missing %s", ", ".join(missing))
                continue
            clean.append(item)
        
        if not clean:
            return False
        
        conn = self._get_conn()
        with self._lock:
            try:
                columns = list(clean[0].keys())
                placeholders = ", ".join(["?" for _ in columns]) + ", 0"
                columns.append("synced")
                
                sql = f"INSERT INTO signals ({', '.join(columns)}) VALUES ({placeholders})"
                values = []
                for s in clean:
                    row = [s.get(c) for c in clean[0].keys()]
                    row.append(0)  # synced
                    values.append(row)
                
                conn.executemany(sql, values)
                conn.commit()
                return True
            except Exception as e:
                log.warning("insert_signals_batch error: %s", e)
                return False

    # ── orders ──────────────────────────────────────────────────────────────────

    def insert_order(self, order_data: Dict[str, Any]) -> bool:
        """Insert a new order record locally."""
        conn = self._get_conn()
        with self._lock:
            try:
                valid_cols = {
                    "ts", "ticker", "side", "action", "contracts", "price_cents",
                    "order_id", "status", "fees_dollars", "cycles", "fill_price_cents",
                    "outcome_won", "pnl_dollars", "settled_at"
                }
                filtered = {k: v for k, v in order_data.items() if k in valid_cols}
                columns = list(filtered.keys())
                placeholders = ", ".join(["?" for _ in columns])
                values = [filtered.get(c) for c in columns]
                columns.append("synced")
                placeholders += ", ?"
                values.append(0)
                
                sql = f"INSERT INTO orders ({', '.join(columns)}) VALUES ({placeholders})"
                conn.execute(sql, values)
                conn.commit()
                return True
            except Exception as e:
                log.warning("insert_order error: %s", e)
                return False

    def update_order_settlement(self, order_id: str, *, fill_price_cents: Optional[int],
                                outcome_won: Optional[bool], pnl_dollars: Optional[float],
                                settled_at: str, ticker: Optional[str] = None) -> bool:
        """Mark a local order row as settled with outcome fields.

        Matches on order_id first; falls back to the latest still-open row for
        `ticker` (dry-run rows where order_id may differ). Never touches the
        `synced` flag: outcome columns stay local-only (the remote orders table
        has no such columns), and the periodic sync pushes `status='settled'`
        on its own schedule.
        """
        conn = self._get_conn()
        with self._lock:
            try:
                won = None if outcome_won is None else int(bool(outcome_won))
                if order_id:
                    cur = conn.execute(
                        """UPDATE orders
                              SET status = 'settled',
                                  fill_price_cents = ?,
                                  outcome_won = ?,
                                  pnl_dollars = ?,
                                  settled_at = ?
                            WHERE order_id = ?""",
                        (fill_price_cents, won, pnl_dollars, settled_at, order_id),
                    )
                    if cur.rowcount > 0:
                        conn.commit()
                        return True
                if ticker:
                    cur = conn.execute(
                        """UPDATE orders
                              SET status = 'settled',
                                  fill_price_cents = ?,
                                  outcome_won = ?,
                                  pnl_dollars = ?,
                                  settled_at = ?
                            WHERE id = (SELECT id FROM orders
                                         WHERE ticker = ? AND settled_at IS NULL
                                         ORDER BY id DESC LIMIT 1)""",
                        (fill_price_cents, won, pnl_dollars, settled_at, ticker),
                    )
                    if cur.rowcount > 0:
                        conn.commit()
                        return True
                log.warning("update_order_settlement: no open orders row for order_id=%s ticker=%s",
                            order_id, ticker)
                return False
            except Exception as e:
                log.warning("update_order_settlement(%s, %s) error: %s", order_id, ticker, e)
                return False

    # ── bot_state ───────────────────────────────────────────────────────────────

    def set_bot_state(self, key: str, value: Any) -> bool:
        """Set a bot state key-value pair (upsert) locally."""
        conn = self._get_conn()
        with self._lock:
            try:
                value_json = json.dumps(value)
                ts = datetime.now(timezone.utc).isoformat()
                conn.execute("""
                    INSERT INTO bot_state (key, value_json, ts, synced)
                    VALUES (?, ?, ?, 0)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        ts = excluded.ts,
                        synced = 0,
                        updated_at = datetime('now')
                """, (key, value_json, ts))
                conn.commit()
                return True
            except Exception as e:
                log.warning("set_bot_state error: %s", e)
                return False

    def set_bot_state_batch(self, states: Dict[str, Any]) -> bool:
        """Set multiple bot state key-value pairs in a single transaction."""
        conn = self._get_conn()
        with self._lock:
            try:
                ts = datetime.now(timezone.utc).isoformat()
                for key, value in states.items():
                    value_json = json.dumps(value)
                    conn.execute("""
                        INSERT INTO bot_state (key, value_json, ts, synced)
                        VALUES (?, ?, ?, 0)
                        ON CONFLICT(key) DO UPDATE SET
                            value_json = excluded.value_json,
                            ts = excluded.ts,
                            synced = 0,
                            updated_at = datetime('now')
                    """, (key, value_json, ts))
                conn.commit()
                return True
            except Exception as e:
                log.warning("set_bot_state_batch error: %s", e)
                return False

    def get_bot_state(self, key: str) -> Optional[Any]:
        """Get a bot state value by key from local database."""
        conn = self._get_conn()
        with self._lock:
            try:
                row = conn.execute(
                    "SELECT value_json FROM bot_state WHERE key = ?", (key,)
                ).fetchone()
                if row:
                    return json.loads(row["value_json"])
                return None
            except Exception as e:
                log.warning("get_bot_state(%s) error: %s", key, e)
                return None

    def get_all_bot_state(self) -> Dict[str, Any]:
        """Get all bot state as a dictionary."""
        conn = self._get_conn()
        with self._lock:
            try:
                rows = conn.execute("SELECT key, value_json FROM bot_state").fetchall()
                return {row["key"]: json.loads(row["value_json"]) for row in rows}
            except Exception as e:
                log.warning("get_all_bot_state error: %s", e)
                return {}

    # ── pnl_ledger (idempotent settlement P&L) ───────────────────────────

    def apply_settlement_pnl(self, idempotency_key: str, market_ticker: str,
                             realized_pnl_dollars: float, fee_dollars: float = 0.0,
                             source: str = "local", venue: str = "",
                             mode: str = "", origin_host: str = "") -> bool:
        """Apply settlement P&L exactly once via idempotency key.

        Returns True if the P&L was newly applied, False if it was already
        recorded (duplicate key), None if the write failed. Callers must treat
        None as "not recorded" and book the P&L locally — returning False on
        error made a locked/full database silently swallow realized P&L.
        Uses INSERT ... ON CONFLICT DO NOTHING so concurrent processes can't
        double-count.

        D1: venue/mode/origin_host are stored on the row so Polymarket and
        Kalshi activity can be distinguished in the shared ledger.
        """
        conn = self._get_conn()
        with self._lock:
            try:
                ts = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    INSERT INTO pnl_ledger
                        (idempotency_key, market_ticker, realized_pnl_dollars,
                         fee_dollars, applied_at, source, venue, mode,
                         origin_host, synced)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(idempotency_key) DO NOTHING
                    """,
                    (idempotency_key, market_ticker, realized_pnl_dollars,
                     fee_dollars, ts, source, venue, mode, origin_host),
                )
                conn.commit()
                # Check if row was actually inserted (changes > 0 means new)
                cursor = conn.execute("SELECT changes()")
                new_rows = cursor.fetchone()[0]
                return new_rows > 0
            except Exception as e:
                log.warning("apply_settlement_pnl(%s, %s) error: %s",
                            idempotency_key, market_ticker, e)
                return None

    def apply_poly_settlement(self, idempotency_key: str, market_slug: str,
                              realized_pnl_dollars: float, state_dict: Dict[str, Any],
                              venue: str = "polymarket_us", mode: str = "dry",
                              fee_dollars: float = 0.0) -> bool:
        """Atomically apply Polymarket settlement P&L and save state in one transaction.

        Writes the idempotent pnl_ledger row AND a bot_state snapshot in a
        single SQLite transaction. State is only saved when the ledger row is
        newly inserted (not a duplicate), so a crash between the DB write and
        the JSON snapshot can never double-credit.

        Returns True if newly applied, False if duplicate, None on error.
        """
        conn = self._get_conn()
        with self._lock:
            try:
                ts = datetime.now(timezone.utc).isoformat()
                origin = socket.gethostname()
                cursor = conn.execute(
                    """
                    INSERT INTO pnl_ledger
                        (idempotency_key, market_ticker, realized_pnl_dollars,
                         fee_dollars, applied_at, source, venue, mode,
                         origin_host, synced)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(idempotency_key) DO NOTHING
                    """,
                    (idempotency_key, market_slug, realized_pnl_dollars,
                     fee_dollars, ts, "local", venue, mode, origin),
                )
                new_rows = cursor.rowcount > 0
                if new_rows:
                    # Only persist state when this is a new settlement.
                    value_json = json.dumps(state_dict)
                    conn.execute(
                        """
                        INSERT INTO bot_state (key, value_json, ts, synced)
                        VALUES (?, ?, ?, 0)
                        ON CONFLICT(key) DO UPDATE SET
                            value_json = excluded.value_json,
                            ts = excluded.ts,
                            synced = 0,
                            updated_at = datetime('now')
                        """,
                        (f"poly_us_state:{mode}", value_json, ts),
                    )
                conn.commit()
                return new_rows
            except Exception as e:
                log.warning("apply_poly_settlement(%s) error: %s",
                            idempotency_key, e)
                return None

    def get_total_realized_pnl(self) -> float:
        """Sum of all applied realized P&L (net of fees)."""
        conn = self._get_conn()
        with self._lock:
            try:
                row = conn.execute(
                    "SELECT COALESCE(SUM(realized_pnl_dollars), 0.0) "
                    "FROM pnl_ledger"
                ).fetchone()
                return float(row[0]) if row else 0.0
            except Exception as e:
                log.warning("get_total_realized_pnl error: %s", e)
                return 0.0

    def get_unsynced_pnl_rows(self) -> list:
        """Get unsynced pnl_ledger rows for cloud sync."""
        conn = self._get_conn()
        with self._lock:
            try:
                rows = conn.execute(
                    "SELECT * FROM pnl_ledger WHERE synced = 0 ORDER BY created_at LIMIT 100"
                ).fetchall()
                return [dict(r) for r in rows]
            except Exception as e:
                log.warning("get_unsynced_pnl_rows error: %s", e)
                return []

    # Alias for API consistency
    get_unsynced_pnl_ledger = get_unsynced_pnl_rows

    def get_unsynced_bot_state(self) -> list:
        """Get unsynced bot_state rows for cloud sync."""
        conn = self._get_conn()
        with self._lock:
            try:
                rows = conn.execute(
                    "SELECT * FROM bot_state WHERE synced = 0 ORDER BY updated_at LIMIT 100"
                ).fetchall()
                return [dict(r) for r in rows]
            except Exception as e:
                log.warning("get_unsynced_bot_state error: %s", e)
                return []

    def get_unsynced_signals(self) -> list:
        """Get unsynced signals rows for cloud sync."""
        conn = self._get_conn()
        with self._lock:
            try:
                rows = conn.execute(
                    "SELECT * FROM signals WHERE synced = 0 ORDER BY created_at LIMIT 100"
                ).fetchall()
                return [dict(r) for r in rows]
            except Exception as e:
                log.warning("get_unsynced_signals error: %s", e)
                return []

    def get_unsynced_orders(self) -> list:
        """Get unsynced orders rows for cloud sync."""
        conn = self._get_conn()
        with self._lock:
            try:
                rows = conn.execute(
                    "SELECT * FROM orders WHERE synced = 0 ORDER BY created_at LIMIT 100"
                ).fetchall()
                return [dict(r) for r in rows]
            except Exception as e:
                log.warning("get_unsynced_orders error: %s", e)
                return []

    def get_sync_status(self) -> Dict[str, int]:
        """Return counts of unsynced rows across all tables."""
        conn = self._get_conn()
        status = {}
        with self._lock:
            for table in self.SYNC_TABLES:
                try:
                    count = conn.execute(f"SELECT count(*) FROM {table} WHERE synced = 0").fetchone()[0]
                    status[table] = int(count)
                except Exception:
                    status[table] = 0
        return status

    def get_latest_order_id(self, ticker: str) -> Optional[str]:
        """Find the latest order_id for a ticker from the local database."""
        conn = self._get_conn()
        with self._lock:
            try:
                row = conn.execute(
                    "SELECT order_id FROM orders WHERE ticker = ? ORDER BY id DESC LIMIT 1",
                    (ticker,),
                ).fetchone()
                if row and row[0]:
                    return row[0]
            except Exception:
                pass
        return None

    def mark_pnl_synced(self, idempotency_key: str) -> bool:
        """Mark a pnl_ledger row as synced to Supabase."""
        conn = self._get_conn()
        with self._lock:
            try:
                conn.execute(
                    "UPDATE pnl_ledger SET synced = 1 WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
                conn.commit()
                return True
            except Exception as e:
                log.warning("mark_pnl_synced(%s) error: %s", idempotency_key, e)
                return False

    def delete_pnl_ledger_entry(self, idempotency_key: str) -> bool:
        """Delete a pnl_ledger entry by idempotency key.
        
        Used when Supabase sync fails after local insert — we need to remove
        the local record so it can be retried on the next cycle.
        """
        conn = self._get_conn()
        with self._lock:
            try:
                conn.execute(
                    "DELETE FROM pnl_ledger WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
                conn.commit()
                return True
            except Exception as e:
                log.warning("delete_pnl_ledger_entry(%s) error: %s", idempotency_key, e)
                return False

    # ── event_outbox (N1) ──────────────────────────────────────────────

    def insert_outbox_event(self, event_type: str, payload_json: str,
                            delivery_class: str = "critical",
                            venue: str = "", mode: str = "",
                            event_id: Optional[str] = None,
                            webhook_path: str = "") -> Optional[int]:
        """Insert an event into the durable outbox. Returns the row id or None."""
        conn = self._get_conn()
        with self._lock:
            try:
                cur = conn.execute("""
                    INSERT INTO event_outbox
                        (event_id, type, delivery_class, payload_json, venue, mode,
                         webhook_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (event_id, event_type, delivery_class, payload_json, venue,
                      mode, webhook_path))
                conn.commit()
                return cur.lastrowid
            except Exception as e:
                log.warning("insert_outbox_event(%s) error: %s", event_type, e)
                return None

    def get_pending_outbox_events(self, limit: int = 100,
                                  lane: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return pending outbox events whose next_attempt_at <= now, ordered by
        delivery_class (critical first, then normal, then telemetry) then
        created_at. Critical events are always selected before others.
        `lane` restricts the query to one N2 lane.
        """
        conn = self._get_conn()
        with self._lock:
            try:
                # julianday parses both SQLite's 'YYYY-MM-DD HH:MM:SS' and the
                # Python isoformat written by mark_outbox_dead, so rows written
                # by either path compare correctly against 'now'.
                now = datetime.now(timezone.utc).isoformat()
                lane_filter = " AND delivery_class = ?" if lane else ""
                params: list = [now]
                if lane:
                    params.append(lane)
                params.append(limit)
                rows = conn.execute(f"""
                    SELECT id, event_id, type, delivery_class, payload_json,
                           venue, mode, created_at, attempts, next_attempt_at,
                           last_error, status, webhook_path
                    FROM event_outbox
                    WHERE status = 'pending'
                      AND julianday(next_attempt_at) <= julianday(?)
                      {lane_filter}
                    ORDER BY
                        CASE delivery_class WHEN 'critical' THEN 0
                                            WHEN 'normal' THEN 1 ELSE 2 END,
                        created_at ASC
                    LIMIT ?
                """, params).fetchall()
                return [dict(zip(
                    ["id", "event_id", "type", "delivery_class", "payload_json",
                     "venue", "mode", "created_at", "attempts", "next_attempt_at",
                     "last_error", "status", "webhook_path"], row)) for row in rows]
            except Exception as e:
                log.warning("get_pending_outbox_events error: %s", e)
                return []

    def mark_outbox_sent(self, event_id: int) -> bool:
        """Mark an outbox event as sent; record the delivery timestamp (N2)."""
        conn = self._get_conn()
        with self._lock:
            try:
                conn.execute(
                    "UPDATE event_outbox SET status = 'sent', "
                    "delivered_at = datetime('now') WHERE id = ?",
                    (event_id,),
                )
                conn.commit()
                return True
            except Exception as e:
                log.warning("mark_outbox_sent(%s) error: %s", event_id, e)
                return False

    def mark_outbox_dead(self, event_id: int, last_error: str,
                         max_attempts: int = 10) -> bool:
        """Mark an outbox event as dead after max_attempts, or bump attempts
        and schedule retry with exponential backoff."""
        conn = self._get_conn()
        with self._lock:
            try:
                row = conn.execute(
                    "SELECT attempts FROM event_outbox WHERE id = ?",
                    (event_id,),
                ).fetchone()
                if not row:
                    return False
                attempts = row[0] + 1
                if attempts >= max_attempts:
                    conn.execute(
                        "UPDATE event_outbox SET status = 'dead', attempts = ?, "
                        "last_error = ? WHERE id = ?",
                        (attempts, last_error, event_id),
                    )
                else:
                    # Exponential backoff with jitter (N2): 2^attempts capped at
                    # 1 hour, scaled by a 0.5-1.0 random factor to avoid a
                    # thundering-herd retry against a recovering n8n.
                    backoff = min(2 ** attempts, 3600)
                    jittered = backoff * (0.5 + random.random() * 0.5)
                    from datetime import timedelta
                    next_attempt = (
                        datetime.now(timezone.utc) + timedelta(seconds=jittered)
                    ).isoformat()
                    conn.execute(
                        "UPDATE event_outbox SET attempts = ?, next_attempt_at = ?, "
                        "last_error = ? WHERE id = ?",
                        (attempts, next_attempt, last_error, event_id),
                    )
                conn.commit()
                return True
            except Exception as e:
                log.warning("mark_outbox_dead(%s) error: %s", event_id, e)
                return False

    def get_outbox_stats(self) -> Dict[str, int]:
        """Return counts of outbox events by status and delivery class."""
        conn = self._get_conn()
        with self._lock:
            try:
                rows = conn.execute("""
                    SELECT status, delivery_class, COUNT(*) as cnt
                    FROM event_outbox
                    GROUP BY status, delivery_class
                """).fetchall()
                stats: Dict[str, int] = {}
                for status, dclass, cnt in rows:
                    stats[f"{status}:{dclass}"] = cnt
                return stats
            except Exception as e:
                log.warning("get_outbox_stats error: %s", e)
                return {}

    def get_outbox_depth_stats(self) -> Dict[str, Any]:
        """N2 metrics: pending depth, oldest undelivered age, per-lane counts.

        `oldest_undelivered_age_s` is 0.0 when nothing is pending. Ages are
        computed with julianday, which parses both SQLite and Python
        datetime formats written to created_at/next_attempt_at.
        """
        conn = self._get_conn()
        with self._lock:
            try:
                row = conn.execute("""
                    SELECT COUNT(*),
                           COALESCE(MAX((julianday('now') - julianday(created_at)) * 86400.0), 0.0),
                           COALESCE(SUM(CASE WHEN delivery_class = 'critical' THEN 1 ELSE 0 END), 0),
                           COALESCE(SUM(CASE WHEN delivery_class = 'normal' THEN 1 ELSE 0 END), 0),
                           COALESCE(SUM(CASE WHEN delivery_class = 'telemetry' THEN 1 ELSE 0 END), 0)
                    FROM event_outbox WHERE status = 'pending'
                """).fetchone()
                dead = conn.execute(
                    "SELECT COUNT(*) FROM event_outbox WHERE status = 'dead'"
                ).fetchone()[0]
                return {
                    "outbox_depth": int(row[0] or 0),
                    "oldest_undelivered_age_s": float(row[1] or 0.0),
                    "pending:critical": int(row[2] or 0),
                    "pending:normal": int(row[3] or 0),
                    "pending:telemetry": int(row[4] or 0),
                    "dead": int(dead or 0),
                }
            except Exception as e:
                log.warning("get_outbox_depth_stats error: %s", e)
                return {"outbox_depth": 0, "oldest_undelivered_age_s": 0.0,
                        "pending:critical": 0, "pending:normal": 0,
                        "pending:telemetry": 0, "dead": 0}

    # ── backtest_runs ───────────────────────────────────────────────────────────

    def insert_backtest_run(self, params: Dict[str, Any], metrics: Dict[str, Any]) -> bool:
        """Record a backtest run result locally."""
        conn = self._get_conn()
        with self._lock:
            try:
                data = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "params_json": json.dumps(params),
                    "ev_per_trade": metrics.get("ev", 0.0),
                    "total_pnl": metrics.get("total_pnl", 0.0),
                    "n_trades": metrics.get("n_trades", 0),
                    "win_rate": metrics.get("win_rate", 0.0),
                    "max_drawdown": metrics.get("max_drawdown", 0.0),
                    "final_balance": metrics.get("final_balance", 0.0),
                    "synced": 0,
                }
                columns = list(data.keys())
                placeholders = ", ".join(["?" for _ in columns])
                values = list(data.values())
                sql = f"INSERT INTO backtest_runs ({', '.join(columns)}) VALUES ({placeholders})"
                conn.execute(sql, values)
                conn.commit()
                return True
            except Exception as e:
                log.warning("insert_backtest_run error: %s", e)
                return False

    # ── Supabase sync ───────────────────────────────────────────────────────────

    # -- circuit breaker ----------------------------------------------------------

    def _sync_breaker_closed(self) -> bool:
        """Return True if the sync circuit breaker is closed (sync allowed)."""
        now = time.time()
        if self._sync_breaker_open_until > 0 and now < self._sync_breaker_open_until:
            return False
        if self._sync_breaker_open_until > 0:
            # Half-open probe: allow one attempt
            self._sync_breaker_open_until = 0.0
            self._sync_breaker_failures = 0
            self._sync_metrics["breaker_state"] = "half-open"
        return True

    def _sync_breaker_success(self) -> None:
        """Record a successful sync; reset breaker."""
        self._sync_breaker_failures = 0
        self._sync_breaker_open_until = 0.0
        self._sync_metrics["breaker_state"] = "closed"

    def _sync_breaker_failure(self) -> None:
        """Record a failed sync; open breaker if max failures reached."""
        self._sync_breaker_failures += 1
        self._sync_metrics["sync_failure_count"] += 1
        if self._sync_breaker_failures >= SYNC_BREAKER_MAX_FAILURES:
            self._sync_breaker_open_until = time.time() + SYNC_BREAKER_COOLDOWN_SECONDS
            self._sync_metrics["breaker_opens"] += 1
            self._sync_metrics["breaker_state"] = "open"
            log.warning(
                "Supabase sync circuit breaker OPEN after %d failures — cooldown %ds",
                self._sync_breaker_failures, int(SYNC_BREAKER_COOLDOWN_SECONDS),
            )

    def _record_sync_failure(self, table: str, pk_col: str, pk_val, attempts: int,
                             wait: int, row_dict: Dict[str, Any], error: str) -> None:
        """D3: persist one sync failure. Past the attempt cap the row moves to
        sync_quarantine (durable poison row) and is deleted from the source
        table so it stops blocking the batch."""
        conn = self._get_conn()
        with self._lock:
            try:
                if attempts >= SYNC_MAX_ROW_ATTEMPTS:
                    conn.execute(
                        "INSERT OR REPLACE INTO sync_quarantine "
                        "(table_name, pk, row_json, attempts, last_error) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (table, str(pk_val), json.dumps(row_dict, default=str),
                         attempts, error[:500]))
                    conn.execute(
                        f"DELETE FROM {table} WHERE {pk_col} = ?", (pk_val,))
                    log.warning("quarantined poison %s row %s after %d attempts",
                                table, pk_val, attempts)
                else:
                    conn.execute(
                        f"UPDATE {table} SET sync_attempts = ?, last_error = ?, "
                        f"next_attempt_at = datetime('now', ?) WHERE {pk_col} = ?",
                        (attempts, error[:500], f"+{int(wait)} seconds", pk_val))
                conn.commit()
            except Exception as qe:
                log.warning("_record_sync_failure(%s, %s) failed: %s",
                            table, pk_val, qe)

    def _sync_table(self, table: str, supabase_client) -> int:
        """Sync unsynced rows from local table to Supabase. Returns count synced.

        D2 hardening:
        - Circuit breaker: skips sync entirely if breaker is open.
        - Per-row retry with exponential backoff: tracks attempts per
          (table, pk) and skips rows that need backoff this cycle.
        - Dead letters: rows exceeding SYNC_MAX_ROW_ATTEMPTS are skipped
          forever (in-memory) so they don't block sync of other rows.
        """
        if not self._sync_breaker_closed():
            self._sync_metrics["breaker_state"] = "open"
            return 0

        conn = self._get_conn()
        # Use appropriate timestamp column for ordering
        order_col = "updated_at" if table == "bot_state" else "created_at"
        with self._lock:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE synced = 0 "
                f"AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now')) "
                f"ORDER BY {order_col} LIMIT 100"
            ).fetchall()

        if not rows:
            return 0

        now = time.time()
        synced_count = 0
        table_failures = 0
        for row in rows:
            row_dict = dict(row)
            row_dict.pop("synced", None)
            row_dict.pop("created_at", None)
            row_dict.pop("updated_at", None)

            # Get primary key for update
            if table == "bot_state":
                pk_col = "key"
                pk_val = row_dict[pk_col]
            elif table == "pnl_ledger":
                pk_col = "idempotency_key"
                pk_val = row_dict[pk_col]
            else:
                pk_col = "id"
                pk_val = row_dict["id"]
                if not table.startswith("poly_"):
                    row_dict.pop("id")

            # D3: persisted retry/backoff/quarantine. Attempts survive
            # restarts; a row that keeps failing is moved to
            # sync_quarantine instead of dead-lettering in memory (which a
            # restart would lose).
            attempts = int(row_dict.get("sync_attempts", 0) or 0) + 1
            wait = min(2 ** attempts, 3600)

            try:
                # Map local column names to Supabase column names
                if table == "signals":
                    if not row_dict.get("type"):
                        row_dict["type"] = "taker"
                    result = supabase_client.insert_signal(row_dict)
                elif table == "orders":
                    # Strip columns that may not exist in remote Supabase schema yet
                    # (fill_price_cents, outcome_won, pnl_dollars, settled_at may
                    #  need manual ALTER TABLE on remote). Local DB keeps them all.
                    order_data = {k: v for k, v in row_dict.items()
                                  if k in ("ts", "ticker", "side", "contracts",
                                           "price_cents", "order_id", "status",
                                           "fees_dollars", "cycles")}
                    # remote orders.action is NOT NULL with no default
                    order_data["action"] = "buy"
                    result = supabase_client.insert_order(order_data)
                elif table == "bot_state":
                    key = row_dict.pop("key")
                    raw_val = row_dict.pop("value_json")
                    try:
                        value = json.loads(raw_val) if isinstance(raw_val, str) else raw_val
                    except Exception as je:
                        log.warning("Corrupt bot_state value_json for %s (%s) — syncing raw string", key, je)
                        value = str(raw_val)
                    result = supabase_client.set_bot_state(key, value)
                elif table == "pnl_ledger":
                    result = supabase_client.insert_pnl_ledger(row_dict)
                elif table == "backtest_runs":
                    raw_params = row_dict.pop("params_json")
                    try:
                        params = json.loads(raw_params) if isinstance(raw_params, str) else raw_params
                    except Exception as je:
                        log.warning("Corrupt params_json in backtest_runs: %s", je)
                        params = {"raw": str(raw_params)}
                    metrics = {k: v for k, v in row_dict.items() if k != "ts"}
                    result = supabase_client.insert_backtest_run(params, metrics)
                elif table.startswith("poly_"):
                    # D2: poly tables mirror to same-named remote tables.
                    result = supabase_client.upsert_table(table, row_dict)
                else:
                    continue

                if result:
                    with self._lock:
                        conn.execute(
                            f"UPDATE {table} SET synced = 1, sync_attempts = 0, "
                            f"last_error = NULL, next_attempt_at = NULL "
                            f"WHERE {pk_col} = ?", (pk_val,)
                        )
                        conn.commit()
                    synced_count += 1
                    self._sync_metrics["sync_success_count"] += 1
                else:
                    # Sync returned False — record the attempt (may quarantine)
                    self._record_sync_failure(table, pk_col, pk_val,
                                              attempts, wait, row_dict,
                                              "upsert returned False")
                    table_failures += 1
                    log.warning("Failed to sync %s row %s to Supabase", table, pk_val)
            except Exception as e:
                self._record_sync_failure(table, pk_col, pk_val,
                                          attempts, wait, row_dict, str(e))
                table_failures += 1
                log.warning("Error syncing %s row %s: %s", table, pk_val, e)

        # Circuit breaker: if any rows failed in this batch, count it as a
        # partial failure. If ALL rows failed, trip the breaker.
        if table_failures > 0 and synced_count == 0 and len(rows) > 0:
            self._sync_breaker_failure()
        elif synced_count > 0:
            self._sync_breaker_success()

        self._sync_metrics["last_sync_ts"] = time.time()
        return synced_count

    def sync_to_supabase(self, supabase_client=None) -> Dict[str, int]:
        """Push all unsynced local changes to Supabase. Returns counts per table.

        If ``supabase_client`` is not provided, it is fetched via
        ``get_supabase()``. The parameter exists for testing with a fake
        client.

        Conflict policy: local rows for trades are authoritative (remote is
        a mirror, keyed by order_id / signal_id / pnl idempotency_key /
        poly row id, merged via on_conflict upserts). Remote config tables
        are read-only to the traders."""
        if not supabase_is_enabled():
            return {t: 0 for t in self.SYNC_TABLES}

        # Circuit breaker check: if open, skip entire sync cycle
        if not self._sync_breaker_closed():
            self._sync_metrics["breaker_state"] = "open"
            return {t: 0 for t in self.SYNC_TABLES}

        if supabase_client is None:
            try:
                supabase_client = get_supabase()
            except Exception as e:
                log.warning("Could not get Supabase client for sync: %s", e)
                self._sync_breaker_failure()
                return {t: 0 for t in self.SYNC_TABLES}

        results = {}
        # Sync tables with compatible schemas
        # bot_state uses per-row upsert and is critical for PnL — sync it FIRST
        # so session_pnl isn't stalled behind large tables.
        for table in self.SYNC_TABLES:
            count = self._sync_table(table, supabase_client)
            results[table] = count
            if count > 0:
                log.info("Synced %d rows from %s to Supabase", count, table)

        return results

    def get_sync_health(self) -> Dict[str, Any]:
        """Return sync health metrics and circuit breaker state."""
        now = time.time()
        breaker_open = self._sync_breaker_open_until > 0 and now < self._sync_breaker_open_until
        return {
            **dict(self._sync_metrics),
            "breaker_open": breaker_open,
            "breaker_open_until": self._sync_breaker_open_until,
            "breaker_failures": self._sync_breaker_failures,
            "quarantined_rows": self.quarantine_count(),
            "supabase_enabled": supabase_is_enabled(),
            "auto_sync": self._auto_sync,
        }

    def get_sync_lag_stats(self) -> Dict[str, Any]:
        """N3: mirror lag for the sync.lag event — unsynced row count and the
        age (seconds) of the oldest unsynced row across the mirrored tables.
        """
        tables = self.SYNC_TABLES
        total = 0
        oldest = 0.0
        conn = self._get_conn()
        with self._lock:
            for table in tables:
                try:
                    row = conn.execute(
                        f"SELECT COUNT(*), "
                        f"COALESCE(MAX((julianday('now') - julianday(created_at)) * 86400.0), 0.0) "
                        f"FROM {table} WHERE synced = 0"
                    ).fetchone()
                    total += int(row[0] or 0)
                    oldest = max(oldest, float(row[1] or 0.0))
                except Exception as e:
                    log.debug("get_sync_lag_stats(%s): %s", table, e)
        return {
            "unsynced_count": total,
            "oldest_unsynced_age_s": oldest,
            "last_success_ts": self._sync_metrics.get("last_sync_ts", 0.0),
            "quarantined": self.quarantine_count(),
        }

    def quarantine_count(self) -> int:
        """D3: rows moved to sync_quarantine (poison rows that failed N times)."""
        conn = self._get_conn()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM sync_quarantine").fetchone()[0])
        except Exception:
            return 0

    def enqueue_control_command(self, command: str, agent_id: str, nonce: str,
                                ts: int, payload_json: str, signature: str
                                ) -> Optional[int]:
        """N4: store a signed control command. Returns row id, or None when the
        nonce is a replay (UNIQUE constraint) or the insert fails."""
        conn = self._get_conn()
        with self._lock:
            try:
                cur = conn.execute(
                    "INSERT INTO control_inbox "
                    "(command, agent_id, nonce, ts, payload_json, signature) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (command, agent_id, nonce, ts, payload_json, signature))
                conn.commit()
                return cur.lastrowid
            except Exception as e:
                log.warning("enqueue_control_command(%s) error: %s", command, e)
                return None

    def get_pending_control_commands(self, limit: int = 20) -> List[Dict[str, Any]]:
        """N4: pending control commands, oldest first."""
        conn = self._get_conn()
        with self._lock:
            try:
                rows = conn.execute(
                    "SELECT id, command, agent_id, nonce, ts, payload_json, signature "
                    "FROM control_inbox WHERE status = 'pending' "
                    "ORDER BY created_at ASC, id ASC LIMIT ?", (limit,)).fetchall()
                return [dict(zip(("id", "command", "agent_id", "nonce", "ts",
                                  "payload_json", "signature"), r)) for r in rows]
            except Exception as e:
                log.warning("get_pending_control_commands error: %s", e)
                return []

    def set_control_command_result(self, row_id: int, status: str, result: str) -> bool:
        """N4: mark a control command applied/rejected with its result."""
        conn = self._get_conn()
        with self._lock:
            try:
                conn.execute(
                    "UPDATE control_inbox SET status = ?, result = ? WHERE id = ?",
                    (status, result, row_id))
                conn.commit()
                return True
            except Exception as e:
                log.warning("set_control_command_result(%s) error: %s", row_id, e)
                return False

    def _sync_loop(self):
        """Background sync loop."""
        while not self._stop_sync.is_set():
            self._stop_sync.wait(SYNC_INTERVAL)
            if not self._stop_sync.is_set():
                try:
                    self.sync_to_supabase()
                except Exception as e:
                    log.warning("Background sync error: %s", e)

    def _start_sync_thread(self):
        """Start background sync thread."""
        if self._sync_thread is None or not self._sync_thread.is_alive():
            self._stop_sync.clear()
            self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
            self._sync_thread.start()
            log.info("Background Supabase sync started (interval: %ds)", SYNC_INTERVAL)

    def stop_sync(self):
        """Stop background sync thread."""
        self._stop_sync.set()
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5)
            log.info("Background Supabase sync stopped")

    def close(self):
        """Close database connection and stop sync."""
        self.stop_sync()
        if hasattr(self, "_local") and hasattr(self._local, "conn") and self._local.conn:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None

    def __del__(self):
        self.close()


_local_dbs: dict[str, LocalDB] = {}


def get_local_db(db_path: str | None = None) -> LocalDB:
    """Get or create the mode-isolated LocalDB instance."""
    path_str = str(db_path or get_db_path())
    if path_str not in _local_dbs:
        _local_dbs[path_str] = LocalDB(db_path=path_str)
    return _local_dbs[path_str]


def get_poly_db_path(mode: str = "dry") -> Path:
    """D2: mode-isolated Polymarket database path (local_data/)."""
    mode_suffix = "live" if mode == "live" else "dry"
    return DB_DIR / f"superbot_local_poly_{mode_suffix}.db"


_poly_dbs: Dict[str, "PolyLocalDB"] = {}


def get_poly_db(mode: str = "dry") -> "PolyLocalDB":
    """Get or create the mode-isolated Polymarket LocalDB instance (D2)."""
    mode = "live" if mode == "live" else "dry"
    if mode not in _poly_dbs:
        _poly_dbs[mode] = PolyLocalDB(mode=mode)
    return _poly_dbs[mode]


class PolyLocalDB(LocalDB):
    """D2: Polymarket US local-first system of record.

    The JSON snapshot (poly_us_state.json) and JSONL ledgers remain the
    crash-recovery export; this DB is the queryable record and the Supabase
    sync source. Same WAL/backup properties as the kalshi LocalDB.
    """

    SYNC_TABLES = ("poly_orders", "poly_positions", "poly_fills",
                   "poly_settlements", "poly_ledger", "poly_epochs",
                   "poly_calibration")

    def __init__(self, db_path: str | None = None, mode: str = "dry",
                 auto_sync: bool = True):
        self.mode = "live" if mode == "live" else "dry"
        super().__init__(db_path=db_path or str(get_poly_db_path(self.mode)),
                         auto_sync=auto_sync)

    def _init_db(self):
        """Poly tables only; the kalshi schema is not created here."""
        conn = self._get_conn()
        with self._lock:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS poly_orders (
                    id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL DEFAULT 'buy',
                    qty REAL NOT NULL,
                    price REAL NOT NULL,
                    cost REAL,
                    fee REAL,
                    status TEXT NOT NULL DEFAULT 'resting',
                    mode TEXT NOT NULL DEFAULT '',
                    synced INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS poly_positions (
                    id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    cost REAL,
                    price REAL,
                    mode TEXT NOT NULL DEFAULT '',
                    synced INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS poly_fills (
                    id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    qty REAL NOT NULL,
                    price REAL NOT NULL,
                    fee REAL,
                    order_id TEXT,
                    mode TEXT NOT NULL DEFAULT '',
                    synced INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS poly_settlements (
                    id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    side TEXT,
                    qty REAL,
                    payout REAL,
                    pnl REAL,
                    outcome INTEGER,
                    mode TEXT NOT NULL DEFAULT '',
                    synced INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS poly_ledger (
                    id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    event TEXT NOT NULL,
                    slug TEXT NOT NULL DEFAULT '',
                    payload_json TEXT,
                    mode TEXT NOT NULL DEFAULT '',
                    synced INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS poly_epochs (
                    id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    start_balance REAL,
                    cash REAL,
                    mode TEXT NOT NULL DEFAULT '',
                    synced INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS poly_calibration (
                    id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT '',
                    synced INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            # N2 parity: the poly DB carries the same durable outbox table.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS event_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    type TEXT NOT NULL,
                    delivery_class TEXT NOT NULL DEFAULT 'critical',
                    payload_json TEXT NOT NULL,
                    venue TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now')),
                    attempts INTEGER DEFAULT 0,
                    next_attempt_at TEXT DEFAULT (datetime('now')),
                    last_error TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    delivered_at TEXT NOT NULL DEFAULT '',
                    webhook_path TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event_outbox_status ON event_outbox(status, next_attempt_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event_outbox_event_id ON event_outbox(event_id)")
            # D3: durable per-row sync bookkeeping + quarantine for poison rows.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_quarantine (
                    table_name TEXT NOT NULL,
                    pk TEXT NOT NULL,
                    row_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    quarantined_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (table_name, pk)
                )
            """)
            for _table in self.SYNC_TABLES:
                for _col in ("sync_attempts INTEGER DEFAULT 0",
                             "last_error TEXT", "next_attempt_at TEXT"):
                    _col_name = _col.split(" ")[0]
                    try:
                        conn.execute(f"ALTER TABLE {_table} ADD COLUMN {_col}")
                    except Exception:
                        pass  # column already exists
            conn.execute("CREATE INDEX IF NOT EXISTS idx_poly_orders_slug ON poly_orders(slug, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_poly_fills_slug ON poly_fills(slug, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_poly_settlements_slug ON poly_settlements(slug, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_poly_ledger_event ON poly_ledger(event, ts)")
            conn.commit()

    # ── helpers ───────────────────────────────────────────────────────────

    def table_count(self, table: str) -> int:
        conn = self._get_conn()
        with self._lock:
            try:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except Exception:
                return 0

    def unsynced_count(self, table: str) -> int:
        conn = self._get_conn()
        with self._lock:
            try:
                return int(conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE synced = 0").fetchone()[0])
            except Exception:
                return 0

    @staticmethod
    def _fid(parts: str) -> str:
        return hashlib.sha256(parts.encode("utf-8", errors="replace")).hexdigest()[:32]

    @staticmethod
    def _row_id(raw_line: str) -> str:
        """Deterministic id for a JSONL source line (importer idempotency)."""
        return hashlib.sha256(raw_line.encode("utf-8", errors="replace")).hexdigest()[:32]

    # ── atomic cycle write (acceptance: one transaction per cycle) ─────────

    def apply_poly_cycle(self, rows: Dict[str, Any]) -> Dict[str, int]:
        """D2: write a full trading cycle atomically.

        ``rows`` maps a poly table name to one row dict (or a list of them).
        Everything commits in a single SQLite transaction: a crash mid-cycle
        rolls back together — a position can never be left without its order
        row. Re-applying the same rows is idempotent (upsert on id).
        """
        counts: Dict[str, int] = {}
        conn = self._get_conn()
        with self._lock:
            try:
                for table, payload in (rows or {}).items():
                    if payload is None:
                        continue
                    items = payload if isinstance(payload, list) else [payload]
                    n = 0
                    for row in items:
                        if not row:
                            continue
                        data = dict(row)
                        data.setdefault("mode", self.mode)
                        cols = [c for c in data if c not in ("synced", "created_at")]
                        vals = [data[c] for c in cols]
                        ph = ", ".join(["?"] * len(cols))
                        sets = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
                        sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph}) "
                               f"ON CONFLICT(id) DO UPDATE SET {sets}, synced = 0")
                        conn.execute(sql, vals)
                        n += 1
                    counts[table] = n
                conn.commit()
                return counts
            except Exception as e:
                conn.rollback()
                log.warning("apply_poly_cycle rollback: %s", e)
                raise

    # ── position snapshot mirror ───────────────────────────────────────────

    def replace_positions(self, positions: List[Dict[str, Any]],
                          mode: str | None = None) -> bool:
        """Mirror the current JSON position snapshot into poly_positions.

        Idempotent upsert keyed by ``slug|side``; rows for positions that are
        no longer present are removed. Called at every _save_state point.
        """
        mode = mode or self.mode
        conn = self._get_conn()
        have: set = set()
        with self._lock:
            try:
                for p in positions or []:
                    pid = f"{p.get('slug', '')}|{p.get('side', '')}"
                    have.add(pid)
                    conn.execute(
                        "INSERT INTO poly_positions (id, slug, side, qty, cost, price, mode, synced) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 0) "
                        "ON CONFLICT(id) DO UPDATE SET qty = excluded.qty, cost = excluded.cost, "
                        "price = excluded.price, mode = excluded.mode, synced = 0",
                        (pid, p.get("slug", ""), p.get("side", ""),
                         float(p.get("qty", 0) or 0),
                         float(p.get("cost", 0) or 0),
                         float(p.get("price", 0) or 0), mode))
                if have:
                    placeholders = ", ".join("?" * len(have))
                    conn.execute(
                        f"DELETE FROM poly_positions WHERE mode = ? AND id NOT IN ({placeholders})",
                        (mode, *sorted(have)))
                else:
                    conn.execute("DELETE FROM poly_positions WHERE mode = ?", (mode,))
                conn.commit()
                return True
            except Exception as e:
                conn.rollback()
                log.warning("replace_positions error: %s", e)
                return False

    # ── recording helpers (used by the trader wiring) ─────────────────────

    def record_settlement(self, settle_key: str, slug: str, position: Dict[str, Any],
                          pnl: float, payout: float, outcome: int,
                          mode: str | None = None) -> bool:
        """D2: one idempotent settlement row (id = settle_key)."""
        mode = mode or self.mode
        try:
            self.apply_poly_cycle({"poly_settlements": {
                "id": settle_key,
                "ts": datetime.now(timezone.utc).isoformat(),
                "slug": slug,
                "side": position.get("side", ""),
                "qty": float(position.get("qty", 0) or 0),
                "payout": float(payout),
                "pnl": float(pnl),
                "outcome": int(outcome),
                "mode": mode,
            }})
            return True
        except Exception:
            return False

    def record_order(self, order: Dict[str, Any], mode: str | None = None) -> bool:
        """D2: one order row (id = caller's order id, e.g. venue id or coid)."""
        mode = mode or self.mode
        try:
            self.apply_poly_cycle({"poly_orders": {
                "id": str(order["id"]),
                "ts": order.get("ts") or datetime.now(timezone.utc).isoformat(),
                "slug": order.get("slug", ""),
                "side": order.get("side", ""),
                "action": order.get("action", "buy"),
                "qty": float(order.get("qty", 0) or 0),
                "price": float(order.get("price", 0) or 0),
                "cost": float(order.get("cost", 0) or 0) if order.get("cost") is not None else None,
                "fee": float(order.get("fee", 0) or 0) if order.get("fee") is not None else None,
                "status": order.get("status", "resting"),
                "mode": mode,
            }})
            return True
        except Exception:
            return False

    def record_fill(self, fill: Dict[str, Any], mode: str | None = None) -> bool:
        """D2: mark the order filled and append a fill row (acceptance: fills)."""
        mode = mode or self.mode
        oid = str(fill.get("order_id", ""))
        ts = fill.get("ts") or datetime.now(timezone.utc).isoformat()
        rows: Dict[str, Any] = {"poly_fills": {
            "id": self._fid(f"{oid}|{fill.get('slug','')}|{fill.get('side','')}|"
                            f"{fill.get('qty', 0)}|{fill.get('price', 0)}"),
            "ts": ts,
            "slug": fill.get("slug", ""),
            "side": fill.get("side", ""),
            "action": fill.get("action", "buy"),
            "qty": float(fill.get("qty", 0) or 0),
            "price": float(fill.get("price", 0) or 0),
            "fee": float(fill.get("fee", 0) or 0),
            "order_id": oid,
            "mode": mode,
        }}
        if oid:
            rows["poly_orders"] = {"id": oid, "ts": ts, "slug": fill.get("slug", ""),
                                   "side": fill.get("side", ""),
                                   "action": fill.get("action", "buy"),
                                   "qty": float(fill.get("qty", 0) or 0),
                                   "price": float(fill.get("price", 0) or 0),
                                   "status": "filled", "mode": mode}
        try:
            self.apply_poly_cycle(rows)
            return True
        except Exception:
            return False

    def mirror_ledger(self, record: Dict[str, Any], mode: str | None = None) -> bool:
        """Mirror one paper-ledger record: poly_ledger row plus order/fill rows
        for trade events. Caller (poly_us_trader._append_ledger) wraps this in
        try/except — trading never depends on it."""
        mode = mode or self.mode
        ev = record.get("event", "")
        ts = record.get("timestamp", "")
        raw = json.dumps(record, sort_keys=True, default=str)
        lid = self._fid(raw)
        rows: Dict[str, Any] = {"poly_ledger": [{
            "id": lid, "ts": ts, "event": ev, "slug": record.get("slug", ""),
            "payload_json": raw, "mode": mode,
        }]}
        if ev == "open":
            oid = f"{lid}:o"
            rows["poly_orders"] = [{
                "id": oid, "ts": ts, "slug": record.get("slug", ""),
                "side": record.get("side", ""), "action": "buy",
                "qty": float(record.get("qty", 0) or 0),
                "price": float(record.get("price", 0) or 0),
                "cost": float(record.get("cost", 0) or 0),
                "fee": float(record.get("fee", 0) or 0),
                "status": "filled", "mode": mode,
            }]
            rows["poly_fills"] = [{
                "id": f"{lid}:f", "ts": ts, "slug": record.get("slug", ""),
                "side": record.get("side", ""), "action": "buy",
                "qty": float(record.get("qty", 0) or 0),
                "price": float(record.get("price", 0) or 0),
                "fee": float(record.get("fee", 0) or 0),
                "order_id": oid, "mode": mode,
            }]
        elif ev in ("rotate_exit", "exit", "close"):
            oid = f"{lid}:o"
            rows["poly_orders"] = [{
                "id": oid, "ts": ts, "slug": record.get("slug", ""),
                "side": record.get("side", ""), "action": "sell",
                "qty": float(record.get("qty", 0) or 0),
                "price": float(record.get("exit_price", record.get("price", 0)) or 0),
                "cost": float(record.get("payout", 0) or 0),
                "fee": float(record.get("fee", 0) or 0),
                "status": "filled", "mode": mode,
            }]
            rows["poly_fills"] = [{
                "id": f"{lid}:f", "ts": ts, "slug": record.get("slug", ""),
                "side": record.get("side", ""), "action": "sell",
                "qty": float(record.get("qty", 0) or 0),
                "price": float(record.get("exit_price", record.get("price", 0)) or 0),
                "fee": float(record.get("fee", 0) or 0),
                "order_id": oid, "mode": mode,
            }]
        try:
            self.apply_poly_cycle(rows)
            return True
        except Exception:
            return False

    # ── legacy importer ────────────────────────────────────────────────────

    def import_legacy(self, state_file, ledger_file, calibration_file=None,
                      mode: str | None = None) -> Dict[str, int]:
        """D2: idempotently import the pre-D2 JSON state + JSONL ledgers.

        Deterministic ids (raw-line hashes, settle keys, epoch timestamps)
        make a second run a no-op. Returns row totals per table after import.
        """
        mode = mode or self.mode
        conn = self._get_conn()
        with self._lock:
            try:
                state = {}
                sp = Path(state_file) if state_file else None
                if sp and sp.exists():
                    try:
                        state = json.loads(sp.read_text())
                    except Exception as e:
                        log.warning("import_legacy: unreadable state file %s: %s", sp, e)
                epoch = state.get("epoch_started_at")
                if epoch:
                    conn.execute(
                        "INSERT OR IGNORE INTO poly_epochs "
                        "(id, started_at, start_balance, cash, mode) VALUES (?, ?, ?, ?, ?)",
                        (str(epoch), str(epoch),
                         float(state.get("start_balance", 0) or 0),
                         float(state.get("cash", 0) or 0), mode))
                for p in state.get("positions", []) or []:
                    pid = f"{p.get('slug', '')}|{p.get('side', '')}"
                    conn.execute(
                        "INSERT INTO poly_positions (id, slug, side, qty, cost, price, mode, synced) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 0) "
                        "ON CONFLICT(id) DO UPDATE SET qty = excluded.qty, cost = excluded.cost, "
                        "price = excluded.price, synced = 0",
                        (pid, p.get("slug", ""), p.get("side", ""),
                         float(p.get("qty", 0) or 0),
                         float(p.get("cost", 0) or 0),
                         float(p.get("price", 0) or 0), mode))

                lp = Path(ledger_file) if ledger_file else None
                if lp and lp.exists():
                    for raw in lp.read_text().splitlines():
                        raw = raw.strip()
                        if not raw:
                            continue
                        lid = self._row_id(raw)
                        try:
                            rec = json.loads(raw)
                        except Exception:
                            continue
                        ev = rec.get("event", "")
                        ts = rec.get("timestamp", "")
                        conn.execute(
                            "INSERT OR IGNORE INTO poly_ledger (id, ts, event, slug, payload_json, mode) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (lid, ts, ev, rec.get("slug", ""), raw, mode))
                        if ev == "open":
                            self._import_legacy_trade(conn, rec, raw, "buy", mode)
                        elif ev in ("rotate_exit", "exit", "close"):
                            self._import_legacy_trade(conn, rec, raw, "sell", mode)

                cp = Path(calibration_file) if calibration_file else None
                if cp and cp.exists():
                    for raw in cp.read_text().splitlines():
                        raw = raw.strip()
                        if not raw:
                            continue
                        lid = self._row_id(raw)
                        ts = ""
                        try:
                            rec = json.loads(raw)
                            ts = rec.get("ts") or rec.get("timestamp") or ""
                        except Exception:
                            pass
                        conn.execute(
                            "INSERT OR IGNORE INTO poly_calibration (id, ts, payload_json, mode) "
                            "VALUES (?, ?, ?, ?)",
                            (lid, ts, raw, mode))
                conn.commit()
                return {t: self.table_count(t) for t in self.SYNC_TABLES}
            except Exception as e:
                conn.rollback()
                raise

    def _import_legacy_trade(self, conn, rec: Dict[str, Any], raw: str,
                             action: str, mode: str) -> None:
        """Import one open/exit ledger record as an order + fill pair."""
        lid = self._row_id(raw)
        ts = rec.get("timestamp", "")
        slug = rec.get("slug", "")
        side = rec.get("side", "")
        qty = float(rec.get("qty", 0) or 0)
        price = float(rec.get("exit_price", rec.get("price", 0)) or 0)
        fee = float(rec.get("fee", 0) or 0)
        cost = float(rec.get("cost", rec.get("payout", 0)) or 0)
        oid = f"{lid}:o"
        conn.execute(
            "INSERT OR IGNORE INTO poly_orders "
            "(id, ts, slug, side, action, qty, price, cost, fee, status, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'filled', ?)",
            (oid, ts, slug, side, action, qty, price, cost, fee, mode))
        conn.execute(
            "INSERT OR IGNORE INTO poly_fills "
            "(id, ts, slug, side, action, qty, price, fee, order_id, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"{lid}:f", ts, slug, side, action, qty, price, fee, oid, mode))
class LocalDBAPI:
    """SupabaseClient-compatible API using local database."""
    
    def __init__(self):
        self.db = get_local_db()
    
    def insert_signal(self, signal_data: Dict[str, Any]) -> bool:
        return self.db.insert_signal(signal_data)
    
    def insert_signals_batch(self, signals: List[Dict[str, Any]]) -> bool:
        return self.db.insert_signals_batch(signals)
    
    def insert_order(self, order_data: Dict[str, Any]) -> bool:
        return self.db.insert_order(order_data)
    
    def set_bot_state(self, key: str, value: Any) -> bool:
        return self.db.set_bot_state(key, value)
    
    def set_bot_state_batch(self, states: Dict[str, Any]) -> bool:
        return self.db.set_bot_state_batch(states)
    
    def get_bot_state(self, key: str) -> Optional[Any]:
        return self.db.get_bot_state(key)
    
    def insert_backtest_run(self, params: Dict[str, Any], metrics: Dict[str, Any]) -> bool:
        return self.db.insert_backtest_run(params, metrics)
    
    def health_check(self) -> bool:
        # Local DB is always healthy if we can connect
        try:
            conn = self.db._get_conn()
            conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False
    
    @property
    def enabled(self) -> bool:
        """Always True for local DB — no external credentials needed."""
        return True
    
    def sync_now(self) -> Dict[str, int]:
        """Manually trigger sync to Supabase."""
        return self.db.sync_to_supabase()


# Global LocalDB API instance (lazy)
_local_db_api: Optional[LocalDBAPI] = None


def get_local_db_api() -> LocalDBAPI:
    """Get or create the global LocalDBAPI instance (SupabaseClient-compatible)."""
    global _local_db_api
    if _local_db_api is None:
        _local_db_api = LocalDBAPI()
    return _local_db_api