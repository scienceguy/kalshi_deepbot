# Polymarket US 15-Minute Markets — Verification Evidence

**Date verified:** 2026-11-20  
**Source:** Web search + public market data (predictionmarketspicks.com, polymarket.com, polymarket.us)

## Summary

Polymarket US crypto Up/Down 15-minute markets are **LIVE** (not PREPROD) as of:
- **BTC 15m:** 2026-09-22
- **ETH/SOL/XRP 15m:** 2026-10-15
- **5-minute windows:** 2026-10-27

The stale comment in `trader.py` (2026-11-13 PREPROD) has been corrected.

## Market Details

| Asset | Window | Settlement Index | Tie Rule | Price to Beat |
|-------|--------|------------------|----------|---------------|
| BTC | 5m, 15m, 1h, 4h | CF Benchmarks BRTI (60s avg) | Up if end >= start | 60s BRTI avg at window open |
| ETH | 5m, 15m, 1h, 4h | CF Benchmarks ETHUSD_RTI (60s avg) | Up if end >= start | 60s ETHUSD_RTI avg at window open |
| SOL | 5m, 15m, 1h, 4h | CF Benchmarks SOLUSD_RTI (60s avg) | Up if end >= start | 60s SOLUSD_RTI avg at window open |
| XRP | 5m, 15m, 1h, 4h | CF Benchmarks XRPUSD_RTI (60s avg) | Up if end >= start | 60s XRPUSD_RTI avg at window open |

**Key finding:** Polymarket US uses **CF Benchmarks indices** (same as Kalshi), NOT Chainlink (that's the international polymarket.com site).

## Fee Structure

- **Fee model:** `Theta × contracts × price × (1 - price)` on taker orders
- **Tier 1 (<$10k trailing 30d vol):** Theta = 0.095
- **Tier 2 (<$50k):** Theta = 0.085
- **Tier 3 (<$250k):** Theta = 0.075
- **Tier 4 (>= $250k):** Theta = 0.0695
- **New accounts (days 1-7):** Theta = 0.0695
- **Makers:** Free (participateDontInitiate=True)

## Cross-Venue Equivalence (T15-31)

| Factor | Kalshi KX*15M | Polymarket US 15m |
|--------|---------------|-------------------|
| Settlement Index | CF Benchmarks BRTI/ETHUSD_RTI/SOLUSD_RTI/XRPUSD_RTI | CF Benchmarks BRTI/ETHUSD_RTI/SOLUSD_RTI/XRPUSD_RTI |
| Averaging Window | 60 seconds | 60 seconds |
| Tie Rule | Up if end > start (strict) | Up if end >= start (inclusive) |
| Assets | BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, ZEC, NEAR | BTC, ETH, SOL, XRP (also DOGE, BNB, HYPE on intl) |
| Windows | 15m only | 5m, 15m, 1h, 4h |
| Window Alignment | 15-min boundaries | 5/15/60/240-min boundaries |

**Implication for cross-venue matcher:** Pair on same underlying + same window start/end time (15m windows). Basis risk comes from tie-rule difference (>= vs >) and potential index calculation differences.

## Verification Commands

```bash
# Check live markets on Polymarket US
curl -s "https://polymarket.com/15M" | grep -E "BTC|ETH|SOL|XRP" | head -20

# Check market rules (resolution source)
curl -s "https://polymarket.com/event/btc-updown-15m-1795225500" | grep -A5 "Resolution Source"

# Side-by-side Kalshi comparison
# https://predictionmarketspicks.com/polymarket-us/15-minute-markets
```

## Config Impact

- `POLYMARKET_ENABLED` remains `false` by default (opt-in via env var)
- When enabled, the concurrent maker order path in `trader.py:_place_poly_order()` is active
- Fee model uses `PM_FEE_MODEL="us_tier"` with `PM_TAKER_COEFFICIENT=0.095` (Tier 1 conservative)
- Settlement logic in `polymarket_us_client.py` already uses BRTI/RTI indices correctly

## References

1. predictionmarketspicks.com - "Kalshi 15-Minute Bitcoin Strategy" (2026-11-06)
2. predictionmarketspicks.com - "Polymarket US 15-minute crypto markets, live" (2026-11-20)
3. polymarket.com market pages (BTC, ETH, SOL, XRP 15m) - resolution source shows CF Benchmarks
4. polymarket.us category pages - live odds
5. defirate.com/odds/crypto/ - live Kalshi vs Polymarket US comparison
