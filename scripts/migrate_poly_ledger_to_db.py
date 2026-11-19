#!/usr/bin/env python3
"""D1: Idempotent JSONL ledger import into local SQLite pnl_ledger.

Reads the legacy Polymarket paper ledger (poly_us_paper_ledger.jsonl) and
imports each "settlement" event row into the local SQLite pnl_ledger via
apply_settlement_pnl, which is idempotent by idempotency key (INSERT ... ON
CONFLICT DO NOTHING). Safe to re-run: duplicates are silently skipped.

Usage:
    python scripts/migrate_poly_ledger_to_db.py [ledger_file]

Defaults ledger_file to SUPERBOT_DATA_DIR/poly_us/poly_us_paper_ledger.jsonl.
Writes nothing if the ledger file doesn't exist.
"""
import json
import sys
from pathlib import Path

import config
from local_db import get_local_db


def main(ledger_file: Path | None = None) -> int:
    if ledger_file is None:
        ledger_file = config.SUPERBOT_DATA_DIR / "poly_us" / "poly_us_paper_ledger.jsonl"

    if not ledger_file.exists():
        print(f"Ledger file not found, nothing to migrate: {ledger_file}")
        return 0

    db = get_local_db()
    imported = 0
    skipped = 0
    errors = 0

    for line_no, line in enumerate(ledger_file.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            print(f"  skip line {line_no}: not valid JSON")
            errors += 1
            continue

        if rec.get("event") != "settlement":
            continue

        slug = rec.get("slug") or rec.get("market_ticker") or ""
        if not slug:
            print(f"  skip line {line_no}: settlement with no slug")
            errors += 1
            continue

        pnl = float(rec.get("pnl", 0.0) or 0.0)
        fee = float(rec.get("fee_dollars", 0.0) or 0.0)
        qty = float(rec.get("qty", 0.0) or 0.0)
        entry = float(rec.get("entry_price", 0.0) or 0.0)

        # Idempotency key mirrors poly_us_trader.check_settlements' key format.
        key = f"poly_settle:{slug}:{rec.get('order_id') or slug}:{qty}:{entry}"

        applied = db.apply_settlement_pnl(
            key, slug, pnl,
            fee_dollars=fee,
            source="jsonl_migration",
            venue="polymarket_us",
            mode="dry",
            origin_host="",
        )
        if applied is True:
            imported += 1
        elif applied is False:
            skipped += 1
        else:
            print(f"  warn line {line_no}: DB write failed for {slug}")
            errors += 1

    print(f"Migrated {imported} settlement rows, {skipped} duplicates skipped, {errors} errors")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    ledger = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    raise SystemExit(main(ledger))
