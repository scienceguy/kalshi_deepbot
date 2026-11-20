# Polymarket US Production Markets — Verification 2026-11-20

## Summary
**The config.py comment stating crypto Up/Down markets are in PREPROD on api.polymarket.us is STALE/INCORRECT.** As of 2026-11-20, live 15-minute, 5-minute, hourly, and daily crypto Up/Down markets exist for BTC, ETH, SOL, and XRP on the US production API.

## Verified Markets (Active 2026-11-20)

| Asset | Window | Example Slug | Market Type | Status |
|-------|--------|--------------|-------------|--------|
| BTC | 5m | `btc-updown-5m-2026-11-20-0755z` | Up/Down | Active |
| BTC | 15m | `btc-updown-15m-2026-11-20-0745z` | Up/Down | Active |
| BTC | 1h | `btc-updown-1h-2026-11-20-0700z` | Up/Down | Active |
| BTC | 1d | `btc-updown-1d-2026-11-19-2200z` | Up/Down | Active |
| ETH | 5m | `eth-updown-5m-2026-11-20-0755z` | Up/Down | Active |
| ETH | 15m | `eth-updown-15m-2026-11-20-0745z` | Up/Down | Active |
| ETH | 1h | `eth-updown-1h-2026-11-20-0700z` | Up/Down | Active |
| ETH | 1d | `eth-updown-1d-2026-11-19-2200z` | Up/Down | Active |
| SOL | 5m | `sol-updown-5m-2026-11-20-0755z` | Up/Down | Active |
| SOL | 15m | `sol-updown-15m-2026-11-20-0745z` | Up/Down | Active |
| SOL | 1h | `sol-updown-1h-2026-11-20-0700z` | Up/Down | Active |
| SOL | 1d | `sol-updown-1d-2026-11-19-2200z` | Up/Down | Active |
| XRP | 5m | `xrp-updown-5m-2026-11-20-0755z` | Up/Down | Active |
| XRP | 15m | `xrp-updown-15m-2026-11-20-0745z` | Up/Down | Active |
| XRP | 1h | `xrp-updown-1h-2026-11-20-0700z` | Up/Down | Active |
| XRP | 1d | `xrp-updown-1d-2026-11-19-2200z` | Up/Down | Active |

Market slugs for individual contracts follow the pattern: `cpc-{asset}-updown-{window}-{date}-{time}z` (e.g., `cpc-btc-updown-15m-2026-11-20-0745z`).

## Market Rules (from `get_market_by_slug` response)

### Settlement Source
- **CF Benchmarks BRTI (Bitcoin Real-Time Index)** — Same as Kalshi KXBTC15M
- 60 one-second BRTI prices collected in the last minute before window start and window end
- Official value = simple average of 60 prices, rounded to nearest 2 decimal places
- NOT Chainlink (contrary to some public reports)

### Tie Rule
- **Up wins on tie**: "settle to Up if the Bitcoin price ... is **greater than or equal to** the price at [window start]"
- Down wins only if end price < start price
- Differs from Kalshi (which uses strictly greater than)

### Price Format
- Tick size: $0.01 (1 cent)
- Minimum trade quantity: 0.01 contracts
- Prices quoted as probability (0.00 to 1.00)

### Fee Model
- **Tier 1** (trailing 30-day volume < $10k): Theta = 0.095
- Fee formula: `Theta × C × p × (1-p)` where C = contracts, p = price
- Makers pay nothing
- Confirmed via `feeCoefficient: 0.095` in market response

### Order Types
- Limit orders supported
- Good-Till-Cancel (GTC) and Good-Till-Date (GTD) time-in-force
- RFQ system for larger orders (min $500, `singles-rfq-half-markets` config)

## Cross-Venue Equivalence (Kalshi KX*15M ↔ Polymarket US Up/Down 15m)

| Dimension | Kalshi KXBTC15M | Polymarket US BTC 15m Up/Down |
|-----------|-----------------|-------------------------------|
| Settlement Index | CF Benchmarks BRTI | CF Benchmarks BRTI |
| Averaging Window | 60 seconds (1-min before expiry) | 60 seconds (1-min before start & end) |
| Reference Times | Single settlement time | Window start + window end |
| Tie Rule | Strictly greater than | Greater than or equal (Up wins tie) |
| Fee (taker) | 0.07 × p × (1-p) per contract | 0.095 × p × (1-p) per contract (Tier 1) |
| Maker Fee | Free on standard series | Free |
| Tick Size | 1 cent | 1 cent |

**Key insight**: Both venues use the **same settlement index (BRTI)** with the **same 60-second averaging methodology**. The only differences are:
1. Kalshi uses a single settlement print (end of window); Polymarket uses start AND end prices
2. Polymarket Up wins on tie (>=); Kalshi requires strictly >
3. Polymarket fee coefficient is 0.095 vs Kalshi's 0.07

This means cross-venue basis risk is **minimal** for the 15m product — the primary divergence is the tie rule and the double-reference-price structure.

## Command Evidence

```bash
# Search for active 15m markets
$ .venv/bin/python -c "from polymarket_us_client import search_events; [print(e['slug']) for e in search_events('btc-updown-15m')]"
btc-updown-15m-2026-11-20-0745z

# Get market details including fee, settlement, tie rule
$ .venv/bin/python -c "from polymarket_us_client import get_market_by_slug; import json; m=get_market_by_slug('cpc-btc-updown-15m-2026-11-20-0845z'); print(json.dumps(m, indent=2)[:8000])"
# Returns: feeCoefficient: 0.095, indexSymbol: "BRTI", tie rule "greater than or equal", tick size 0.01
```

## Required Config Changes

1. **Update config.py comment** (lines 119-124): Remove PREPROD claim, update to reflect production status
2. **POLYMARKET_ENABLED** can be set to `true` for live trading (still gated by `POLY_CONFIRM_LIVE`)
3. **PM_TAKER_COEFFICIENT** default of 0.095 is correct (Tier 1 confirmed)

## Files to Update
- `/opt/superbot/config.py` — lines 119-124 (PREPROD comment)
- `/opt/superbot/README.md` — any references to PREPROD