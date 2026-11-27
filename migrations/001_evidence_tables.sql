-- T19-07: first-class fill, settlement, position, and gate evidence.
-- Raw production payloads are stored beside parsed columns for auditability.
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    order_id TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    contracts REAL NOT NULL,
    price_cents INTEGER NOT NULL,
    fee_dollars REAL NOT NULL DEFAULT 0,
    liquidity TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    settled_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    payout_dollars REAL NOT NULL DEFAULT 0,
    fee_dollars REAL NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS positions (
    ticker TEXT PRIMARY KEY,
    side TEXT NOT NULL,
    contracts REAL NOT NULL,
    entry_price_cents INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    raw_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS gate_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    decision TEXT NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);
