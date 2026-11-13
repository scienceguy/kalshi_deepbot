#!/usr/bin/env python3
"""Cumulative realized-PnL curve from the settlement ledger.

Reads pnl_ledger rows (default: the VPS local SQLite DB) and prints the running
sum, so a live dry-run's compounding is visible trade by trade. Optionally
filters to trades settled at/after a cutoff (e.g. when a new config went live).

Usage: python scripts/pnl_curve.py [db_path] [--since "YYYY-MM-DD HH:MM"]
"""
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") \
    else "/opt/superbot/local_data/superbot_local.db"
since = None
if "--since" in sys.argv:
    since = sys.argv[sys.argv.index("--since") + 1]

conn = sqlite3.connect(DB)
q = "SELECT created_at, market_ticker, realized_pnl_dollars FROM pnl_ledger"
args = ()
if since:
    q += " WHERE created_at >= ?"
    args = (since,)
q += " ORDER BY created_at"
rows = list(conn.execute(q, args))
if not rows:
    print("no ledger rows")
    sys.exit(0)

cum = 0.0
wins = 0
print("%-20s %-26s %8s %9s" % ("settled_at", "market", "pnl$", "cum$"))
for i, (ts, tk, pnl) in enumerate(rows):
    cum += pnl
    if pnl > 0:
        wins += 1
    if i < 8 or i >= len(rows) - 12 or (i + 1) % 10 == 0:
        print("%-20s %-26s %8.4f %9.3f" % (ts, tk[:26], pnl, cum))
print("\ntrades=%d  wins=%d (%.0f%%)  final_cum=$%.4f  mean=$%.4f/trade" %
      (len(rows), wins, 100.0 * wins / len(rows), cum, cum / len(rows)))
