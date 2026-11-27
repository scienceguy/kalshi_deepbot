-- Historical rows are not authoritative settled P&L for live reconciliation.
UPDATE pnl_ledger
SET source = 'legacy'
WHERE source IS NULL OR source NOT IN ('settled', 'paper', 'manual', 'legacy');
