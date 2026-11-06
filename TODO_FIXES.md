# Comprehensive Fix List - Kalshi SuperBot

## MUST FIX (Critical bugs / data loss)

### 1. OKX pagination cursor logic error (binance_feed.py:261-297)
- Problem: cursor = int(data[0][0]) - 1 points before oldest kline, skipping it on next iteration
- Fix: Use cursor = int(data[-1][0]) - 1 (newest in batch) for OKX reverse pagination

### 2. Settlement P&L idempotency key uses event_ticker fallback
- Problem: Falls back to t.split("-")[0] (series) instead of event_ticker
- Fix: Use market.get("event_ticker") directly; verify API returns it; don't fall back to series

### 3. NO price validation sample bias (trader.py:1651)
- Problem: Uses markets[0] only; may be unrepresentative
- Fix: Check all markets or remove validation entirely (downstream handles it)

## SHOULD FIX (Risk / correctness)

### 4. Redundant sort in sigma_curve (crossvenue_history.py:156)
- Problem: rows.sort() redundant; _fetch_klines_paginated guarantees order
- Fix: Remove the sort

### 5. Bid history TTL cleanup: add size-based trigger (signals.py)
- Problem: Only time-based (every 50 calls); no size-based trigger
- Fix: Add `if len(_bid_hist) > 50000: cleanup()`

### 6. Delete_pnl_ledger_entry source filter redundant (local_db.py:439)
- Problem: In single-process, all entries have source='local'; filter redundant
- Fix: Remove the filter

## NICE TO HAVE (Lean / cleanup)

### 7. NO price validation sample bias (trader.py)
- Fix: Check multiple markets or remove validation

### 8. Verify event_ticker is actually returned by API
- Need to check what fields Kalshi API returns for settled markets

