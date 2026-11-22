# Polymarket US 15-Minute Markets -- Verification Evidence

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

- **Fee model:** `Theta x contracts x price x (1 - price)` on taker orders
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

---

## PRODUCTION API VERIFICATION (2026-11-22, agent evidence)

Verified against the live production API through `polymarket_us_client`
(`get_market_by_slug` reads are public; `get_events`/`search_events` list).
Resolves for the same instant on all four underlyings:

| Asset | Market slug (live)       | indexSymbol | horizon | window (UTC)          | tick  | minQty | feeCoefficient | status            |
|-------|--------------------------|-------------|---------|-----------------------|-------|--------|----------------|-------------------|
| BTC   | cpc-btc-updown-15m-...   | BRTI        | 15m     | 05:45:00Z->06:00:00Z  | 0.01  | 0.01   | 0.095          | MARKET_STATUS_OPEN|
| ETH   | cpceth-eth-updown-15m-...| ETHUSD_RTI  | 15m     | 05:45:00Z->06:00:00Z  | 0.01  | 0.01   | 0.095          | MARKET_STATUS_OPEN|
| SOL   | cpcsol-sol-updown-15m-...| SOLUSD_RTI  | 15m     | 05:45:00Z->06:00:00Z  | 0.01  | 0.01   | 0.095          | MARKET_STATUS_OPEN|
| XRP   | cpcxrp-xrp-updown-15m-...| XRPUSD_RTI  | 15m     | 05:45:00Z->06:00:00Z  | 0.01  | 0.01   | 0.095          | MARKET_STATUS_OPEN|

Slug prefixes are per-asset and NOT uniform: BTC is `cpc-btc-`, the others are
`cpceth-`, `cpcsol-`, `cpcxrp-`. Event slugs (search results) lack the prefix;
only the market slugs above resolve on `/v1/market/slug/{slug}`.

Resolution source (verbatim market description, BTC): "This market will settle
to Up if the Bitcoin price at 1:00AM ET on Nov 22, 2026 is greater than or equal
to the price at 12:45AM ET on Nov 22, 2026. Otherwise, this market will settle
to Down. Outcome sourced from CF Benchmarks' Bitcoin Real-Time Index (BRTI)."
ETH adds the averaging rule verbatim: "in the last minute before each of those
two times, 60 ETHUSD_RTI prices are collected. The official value for that time
is the simple average of these prices, rounded to the nearest 2 decimal places."

Consequences recorded here (T15-01, T15-31 inputs):
- Tie rule CONFIRMED: Up when end >= start (not >).
- Reference source CONFIRMED: CF Benchmarks RTI indexes -- the SAME index
  family Kalshi settles on (Kalshi KX*15M uses the BRTI 60-second average; my
  session audit 2026-11-22 measured strike(N) = prior window final-minute BRTI
  TWAP to <0.1bps). The 2026-11-13 PREPROD comment and the "possibly
  Chainlink" report (F-01) are both WRONG for the US venue; Chainlink is the
  international site. Basis risk on a BTC pair therefore narrows to window
  alignment and the start reference, not the index source -- but it is not zero
  until the equivalence table is filled (T15-31).
- Fee tier CONFIRMED from the venue's own market field: feeCoefficient = 0.095
  = Tier 1, matching config PM_FEE_TIER default (F-15).
  scripts/fee_probe.py (authenticated orders.preview) returns OK but its
  response body carries no fee field (keys: ['order']), so the preview path
  cannot confirm the tier; the market's feeCoefficient field is the evidence.
- Windows roll on :00/:15/:30/:45 UTC and purge within minutes of closing, so
  a matcher must resolve the live slug per instant; hardcoded slugs go stale.
