# Comprehensive Fix List - Kalshi SuperBot

## MUST FIX (Critical bugs / data loss)

### 1. OKX klines pagination cursor logic error (binance_feed.py:261-297)
- Problem: After data.reverse(), cursor_end = int(data[0][0]) - 1 advances cursor BEFORE oldest kline, skipping it
- Fix: Use data[-1][0] (newest in batch) to advance cursor, or use `after` parameter for forward pagination

### 2. NO price format validation crashes bot in live mode (trader.py:1657-1688)
- Problem: Hard failure on config mismatch; downstream code already handles both formats
- Fix: Downgrade to warning in all modes

### 3. Settlement P&L idempotency key uses order_id that may change (trader.py:1225, local_db.py:354)
- Problem: Key format doesn't include event info; cross-event collisions possible
- Fix: Change key to include event_ticker: settle:{event_ticker}:{order_id}

### 4. delete_pnl_ledger_entry deletes without ownership verification (local_db.py:433-450)
- Problem: Could delete another process's settled P&L record
- Fix: Add source column check: DELETE WHERE idempotency_key = ? AND source = 'local'

## SHOULD FIX (Risk / scale / correctness)

### 5. sigma_curve assumes klines sorted but OKX fallback reverses (binance_feed.py, crossvenue_history.py)
- Problem: OKX data not perfectly sorted after reverse+extend; enumerate assumes chronological order
- Fix: Sort all klines by timestamp before processing: all_klines.sort(key=lambda k: k[0])

### 6. spot_closes vs sigma_curve timestamp alignment (crossvenue_history.py)
- Problem: spot_closes aligns to close time (+60000ms), sigma_curve uses open_time; 1-minute misalignment
- Fix: Make sigma_curve also align to close time like spot_closes

### 7. Bid history TTL cleanup runs every 100 calls (signals.py:237-246)
- Problem: With 9 markets × 30s cycle, cleanup runs ~every 5.5 min; _bid_hist grows to 200k tuples
- Fix: Reduce cleanup interval to 50 calls or add size-based trigger

### 8. market_finder.py hedge filter uses info.yes_ask instead of ya (market_finder.py:314)
- Problem: info built from orderbook; if orderbook fetch failed, info.yes_ask might be 0 while ya had valid value
- Fix: Pass ya from pre-filter to _build and use it for hedge check

### 9. settled_events doesn't validate expiration_value correctly (crossvenue_history.py:87-96)
- Problem: Validation 100 < val < 1_000_000 assumes BTC price; wrong for ETH/SOL; also doesn't handle payout format
- Fix: Use _settle_yes_cents logic from market_finder.py

## NICE TO HAVE (Lean / cleanup)

### 10. Duplicate sigma fetching logic (binance_feed.py)
- Problem: Three functions with similar pagination logic
- Fix: Extract common pagination into helper

### 11. stream_trades unused websockets import (binance_feed.py:31)
- Problem: Import at module top but stream_trades never called
- Fix: Remove import or move inside function

### 12. config.py auto-validation on import (config.py:458-462)
- Problem: validate_bands() runs at module import; crashes before graceful handling
- Fix: Remove auto-validation; keep only explicit calls in trader.py
