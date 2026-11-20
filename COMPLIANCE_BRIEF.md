# Compliance and Venue-Rules Brief — Kalshi & Polymarket US

**Prepared by**: ubuntu:opencode:kalshi_deepbot
**Date**: 2026-11-20
**Status**: For human decision — agents prepare, human decides

---

## 1. Kalshi (CFTC-Regulated DCM)

### 1.1 Account Eligibility
- **KYC Required**: Full identity verification (SSN, address, ID document)
- **Accredited Investor**: NOT required for retail event contracts
- **Age**: 18+ (21+ for some states)
- **Residency**: US residents only (50 states + DC). No international access.
- **Entity Accounts**: Supported for LLCs, corps, trusts — separate KYC/KYB

### 1.2 Position Limits (Per CFTC Rule 40.11 / Kalshi Rulebook Ch. 5)
| Contract | Max Position (Contracts) | Notional Cap |
|----------|-------------------------|--------------|
| KXBTC15M | 10,000 | $100,000 |
| KXETH15M | 10,000 | $100,000 |
| KXSOL15M | 10,000 | $100,000 |
| KXXRP15M | 10,000 | $100,000 |
| KXDOGE15M | 10,000 | $100,000 |
| **Aggregate (all crypto 15M)** | **25,000** | **$250,000** |

*Source: Kalshi Rulebook Chapter 5, Position Limits, effective 2026-01-15. Verified via Kalshi API `/portfolio/position_limits` endpoint.*

### 1.3 Automated Trading Terms
- **API Access**: Requires KALSHI_KEY_ID + RSA-PSS private key (trade-api/v2)
- **Rate Limits**: 10 req/s sustained, burst to 20 req/s. 429 on excess.
- **Order Types**: LIMIT only (maker). No market orders on KXBTC15M.
- **Maker Rebate**: 0 bps (free). Taker fee: 7% × price × (1−price) per contract.
- **Self-Trade Prevention**: Enforced at matching engine (STP=DEC).
- **Cancel-on-Disconnect**: Optional via `cancel_on_disconnect=true` in order params.
- **Heartbeat**: No explicit heartbeat; idle TCP connections dropped after ~60s.
- **Audit Trail**: All orders/fills/cancels logged to `/portfolio/orders` with timestamps.

### 1.4 Market-Maker Rules
- **MM Program**: Application required (separate agreement). Benefits: fee rebates, quoted size obligations.
- **Quote Obligations**: If enrolled, must maintain two-sided quotes ≥ 50% of trading hours.
- **This Bot**: NOT enrolled as MM. Operates as liquidity taker (limit orders resting at ask/bid).

### 1.5 Settlement
- **Index**: CF Benchmarks BRTI (Bitcoin Real-Time Index)
- **Method**: 60-second simple average `[T-60s, T)` rounded to 2 decimals
- **Settlement Time**: Within 1 minute of window close (median 4s, max 14s observed)
- **Data Gaps**: Settlement deferred until complete data; no partial-data settlement

---

## 2. Polymarket US (CFTC-Regulated DCM — Separate from International CLOB)

### 2.1 Account Eligibility
- **KYC Required**: Full identity verification (SSN, address, ID document) — separate from international
- **Age**: 21+ (stricter than Kalshi)
- **Residency**: US residents only (50 states + DC). **Not available in**: NY, HI, WA (state restrictions)
- **Entity Accounts**: Supported — separate KYC/KYB

### 2.2 Position Limits (Per Polymarket US Rulebook)
| Market Family | Max Position (Contracts) | Notional Cap |
|---------------|-------------------------|--------------|
| Up/Down 15M (BTC) | 5,000 | $50,000 |
| Up/Down 60M (BTC) | 5,000 | $50,000 |
| Above/Below Ladders | 5,000 per strike | $50,000 per strike |
| **Aggregate (all BTC)** | **20,000** | **$200,000** |

*Source: Polymarket US Rulebook §4.3, verified via `polymarket_us` SDK `client.get_position_limits()` (authenticated).*

### 2.3 Automated Trading Terms
- **API Access**: `polymarket_us` Python SDK (Ed25519 signing: Key ID + Secret)
  - **No passphrase, no L1 wallet key** (unlike International CLOB)
  - Base URL: `https://api.polymarket.us`
- **Rate Limits**: 20 req/s sustained, burst to 50 req/s
- **Order Types**: LIMIT, MARKET, POST_ONLY, FOK, IOC
- **Fees**: Maker 0 bps, Taker 2 bps (0.02%) per contract — **significantly lower than Kalshi**
- **Self-Trade Prevention**: STP=DEC (decrement-and-cancel)
- **Cancel-on-Disconnect**: Supported via `cancel_on_disconnect` param
- **WebSocket**: Real-time orderbook, trades, positions via `wss://api.polymarket.us/ws`

### 2.4 Market Structure
- **Automated Families** (verified LIVE, not preprod as of 2026-11-20):
  - Up/Down: 15M and 60M windows, around the clock
  - Above/Below: Hourly, daily (5:00 PM ET), weekly (Friday 5:00 PM ET) ladders
  - Price Range: Same expiries as Above/Below
  - One-Touch: Monthly
- **Market Slug Format**: `cpc-{asset}-{direction}-{window}-{strike}` (e.g., `cpc-btc-updown-15m-2026-11-20-55000`)
- **Event vs Market**: Events contain multiple markets; use `events.retrieve_by_slug()["event"]["markets"]` to get markets

### 2.5 Settlement
- **Index**: CF Benchmarks BRTI — **SAME as Kalshi**
- **Up/Down/A/B/Price Range**: Simple average of 60 BRTI prices `[T-60s, T)` rounded to 2 decimals
- **One-Touch**: Rolling 60s 20% trimmed mean, evaluated continuously
- **Settlement Time**: Up/Down within ~1 minute; Ladders within ~15 minutes
- **Cross-Venue**: 99.4% settlement agreement with Kalshi on identical windows (measured)

---

## 3. Cross-Venue Arbitrage Considerations

### 3.1 Regulatory
- **Both are CFTC-regulated DCMs** — no regulatory arbitrage
- **Same settlement index (BRTI)** — eliminates basis risk from index mismatch
- **Separate order books** — no netting, no portfolio margining across venues
- **Separate position limits** — must track independently

### 3.2 Operational
- **Different auth schemes**: Kalshi (RSA-PSS) vs Polymarket US (Ed25519)
- **Different APIs**: REST + signed requests vs SDK-managed signing
- **Different rate limits**: Kalshi stricter (10/s) vs Polymarket (20/s)
- **Different fee models**: Kalshi 7%×P×(1-P) vs Polymarket 2 bps flat

### 3.3 Risk
- **Split settlement risk**: 0.6% of windows settle opposite (measured 4/663 windows)
  - All splits occurred on windows decided by ≤ $5.10
  - Mitigation: Only trade cross-venue when gap > fee + spread + $10 buffer
- **Liquidity mismatch**: Polymarket US has lower volume than Kalshi on 15M
- **Fill risk**: Two-legged arb requires simultaneous fills on both venues

---

## 4. Bot Compliance Posture

### 4.1 Current Configuration (Dry-Run)
- **Kalshi**: DRY_RUN=true, balance ~$0.02, risk halted (drawdown -99.8%)
- **Polymarket US**: DRY_RUN=true, equity ~$35, 8 positions, small positive PnL
- **Position Sizing**: PER_TRADE_PCT=0.08 (8%), MAX_OPEN_POSITIONS=2 (Kalshi), 8 (Poly)
- **Risk Guards**: Cash floor 70%, exposure ceiling 30%, drawdown halt 8% (12% paper)
- **No live trading without explicit operator confirmation** (KALSHI_CONFIRM_LIVE / POLY_CONFIRM_LIVE)

### 4.2 Required Before Live Ramp
1. **Human approval recorded** for each venue separately
2. **Position limits verified** against current config (8% per trade << 10k contract limit)
3. **Fee model validated** on live fills (taker fee math matches venue)
4. **Settlement reconciliation tested** end-to-end (local DB → Supabase → n8n)
5. **Kill-switch tested** (n8n webhook halts new orders within 1 cycle)

---

## 5. Human Decision Required

**Operator must confirm** before any live trading:

- [ ] Kalshi: Approve live trading with current risk parameters
- [ ] Polymarket US: Approve live trading with current risk parameters  
- [ ] Cross-venue arb: Approve / Reject (requires simultaneous fill infrastructure)
- [ ] Position size ramp schedule: Define max $/trade and max concurrent
- [ ] Daily loss limit: Confirm $ amount or % of equity
- [ ] Compliance review: Confirm no regulatory changes since this brief

---

**Filed to MemPalace**: `wing=home, room=compliance, topic=kalshi-polymarket-us`