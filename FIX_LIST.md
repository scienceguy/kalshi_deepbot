# Comprehensive Fix List - Kalshi SuperBot

## MUST FIX (Critical bugs, data loss, crashes)

### 1. [FIXED] Settlement P&L double-count on replay crash - commit 13a9278
### 2. [FIXED] Position popped before PnL booked - commit 4731b5f
### 3. [FIXED] _order_count truncates fractional counts - already returns float
### 4. [FIXED] mid_yes returns synthetic 50c - returns None now
### 5. [FIXED] TradeSignal.ticker missing - uses sig.market.ticker

### 6. [PENDING] Hedge post-filter checks no_ask instead of yes_ask (market_finder.py:287)
### 7. [PENDING] sigma_per_minute single point of failure - no fallback (binance_feed.py)
### 8. [PENDING] Lookahead bias in ML training (ml_model.py, crossvenue_history.py)
### 9. [PENDING] ML fair value feature uses same spot path as label (ml_model.py:212)
### 10. [PENDING] _maker_price aggressive NO fallback returns 50c (signals.py:107)
### 11. [PENDING] settled_events expiration_value is raw price, no schema validation (crossvenue_history.py:78-84)
### 12. [PENDING] candles() wrong API path (crossvenue_history.py:99)
### 13. [PENDING] _f() reads close_dollars but API returns close (crossvenue_history.py:107, 155)

## SHOULD FIX (Risky code, config issues, performance)

### 14. [PENDING] PANIC_FADE_ENABLED never checked (signals.py:378)
### 15. [PENDING] Panic fade confidence inverted for NO (signals.py:451)
### 16. [PENDING] Panic fade hardcoded 5-25c NO band (signals.py:437)
### 17. [PENDING] Hedge strength hardcoded 0.0 (signals.py:520, 524)
### 18. [PENDING] Hedge missing stability check (signals.py:467-525)
### 19. [PENDING] MID_TARGET_CENTS=3 incompatible with 15-55c band (signals.py:340)
### 20. [PENDING] CONFIDENCE_STRENGTH_SCALE=6.0 too small (signals.py:342)
### 21. [PENDING] SKEW/TREND forced into favourite-maker band (signals.py:317-319)
### 22. [PENDING] dollar_risk missing taker fee (signals.py:335)
### 23. [PENDING] Missing ALLOW_YES_BUYS gate for SKEW/TREND (signals.py:338-348)
### 24. [PENDING] sigma_at O(n²) API calls (crossvenue_history.py:123-134, 176)
### 25. [PENDING] Missing 15m series in SERIES_ASSET (crossvenue_history.py:41-42)
### 26. [PENDING] expiration_value parsing outside try/except (crossvenue_history.py:84)
### 27. [PENDING] close_time can be None (crossvenue_history.py:83, 139)
### 28. [PENDING] No Dict import in type hints (binance_feed.py:75)
### 29. [PENDING] Symbol parsing lossy (binance_feed.py:62)
### 30. [PENDING] 40s worst-case timeout (binance_feed.py:51, 64, 93)
### 31. [PENDING] No session reuse (binance_feed.py:55, 85, 121, 135)
### 32. [PENDING] max_minutes default 10.0 blocks 15M markets (ladder_strategy.py:300)
### 33. [PENDING] Config validation not auto-called (config.py:233-339)

## NICE TO HAVE

### 34. [PENDING] Dead code: latency_arb.py
### 35. [PENDING] add_n8n_credentials.py / inject_n8n_workflows.py superseded
### 36. [PENDING] Duplicate floor_contracts call (signals.py:85, 329)
### 37. [PENDING] poly feature mirrors kalshi
### 38. [PENDING] _KRAKEN_PAIR missing most assets
### 39. [PENDING] get_klines_range hardcoded limit=1000
### 40. [PENDING] stream_trades imports inside function, no reconnect

