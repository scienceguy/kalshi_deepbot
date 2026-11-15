# Kalshi v2 REST API Research Report

**Compiled:** Sun Nov 01 2026  
**Purpose:** Authoritative reference for Kalshi v2 REST API authentication, trading, fee model, rate limits, historical data, and known gotchas — for building a Kalshi trading bot (`kalshi_superbot`).  
**Primary sources:** Official Kalshi documentation (docs.kalshi.com), official Kalshi fee schedule PDF, plus third-party deep-dives (Allium, Predictefy, BotForKalshi, QuantVPS, PredictAndProfit, AlphaScope, Kairos). All URLs are listed inline with each finding.

---

## Table of Contents

1. [API Signature Format — What Is Signed](#1-api-signature-format)
2. [API Key Scopes and Permissions](#2-api-key-scopes-and-permissions)
3. [Demo vs Production Environments](#3-demo-vs-production-environments)
4. [Fee Model](#4-fee-model)
5. [REST API Rate Limits](#5-rest-api-rate-limits)
6. [Order Placement — Limit Orders, Time-in-Force, Self-Trade Prevention](#6-order-placement)
7. [Historical Data Endpoints](#7-historical-data-endpoints)
8. [Known Authentication Issues and Gotchas](#8-known-authentication-issues-and-gotchas)
9. [Quick Reference: Minimal Signed Request (Python)](#9-quick-reference-minimal-signed-request-python)

---

## 1. API Signature Format — What Is Signed

### Source
- **Kalshi Docs — API Keys:** https://docs.kalshi.com/getting_started/api_keys
- **Kalshi Docs — Quick Start: Authenticated Requests:** https://docs.kalshi.com/getting_started/quick_start_authenticated_requests
- **Kalshi Docs — API Environments and Endpoints (Request Signing):** https://docs.kalshi.com/getting_started/api_environments
- **Kalshi Docs — Create Order (V2) securitySchemes:** https://docs.kalshi.com/api-reference/orders/create-order-v2

### Exact signature format

Every authenticated request requires three headers:

| Header | Value |
|--------|-------|
| `KALSHI-ACCESS-KEY` | The API Key ID (UUID), e.g. `a952bcbe-ec3b-4b5b-b8f9-11dae589608c` |
| `KALSHI-ACCESS-TIMESTAMP` | Request timestamp in **milliseconds** since Unix epoch, e.g. `1703123456789` |
| `KALSHI-ACCESS-SIGNATURE` | Base64-encoded signature of the pre-sign text |

### Pre-sign text (what precisely is signed)

The message to sign is the **concatenation with no separator**:

```
timestamp + HTTP_METHOD + path
```

Example: `1703123456789GET/trade-api/v2/portfolio/balance`

**Critical rules:**

1. **The path must include the `/trade-api/v2` prefix.** Sign the full URL path from the API root. For `https://external-api.demo.kalshi.co/trade-api/v2/portfolio/balance`, sign `/trade-api/v2/portfolio/balance` — not just `/portfolio/balance`. The host itself is **not** included in the signature.

2. **Strip query parameters from the path before signing.** If the request URL is `/trade-api/v2/portfolio/orders?limit=5`, sign only `/trade-api/v2/portfolio/orders` (strip the `?` and everything after it). Query parameters are sent normally in the request but are not part of the signed message.

3. **Timestamp must be in milliseconds**, not seconds. Kalshi rejects timestamps more than a few seconds old; clock drift causes 401s.

4. **HTTP method must be uppercase** (`GET`, `POST`, etc.).

5. **No spaces or separators** between timestamp, method, and path in the signed string.

### Signature algorithm by key type

| Key type | Algorithm | Padding / hash | Notes |
|----------|-----------|----------------|-------|
| **Ed25519** (recommended, default) | Ed25519 (RFC 8032) | Signs the message directly | Lower signing cost than RSA-PSS; 64-byte signatures |
| **RSA** (2048-bit) | RSA-PSS | MGF1 with SHA-256, salt length = digest length | Use when client only supports RSA-PSS (SDKs before 3.31.0) |

Headers, pre-sign text, permissions, and errors are **identical for both key types**. Kalshi selects the verification algorithm from the registered public key; sign with the algorithm matching your key.

**Important:** The PEM header does not reliably identify key type — PKCS#8 RSA keys (`openssl genpkey` output) also begin with `-----BEGIN PRIVATE KEY-----`. Determine type from the parsed key object.

### Host independence

The host does **not** change the signature payload. All hosts use the same signed path for a given request:

```
/trade-api/v2/portfolio/orders
```

This is true for `external-api.kalshi.com`, `api.elections.kalshi.com`, `external-api.demo.kalshi.co`, and `demo-api.kalshi.co`.

### Header transport

Headers, not query parameters. The signature is sent in the `KALSHI-ACCESS-SIGNATURE` header; the key ID in `KALSHI-ACCESS-KEY`; the timestamp in `KALSHI-ACCESS-TIMESTAMP`.

### Auth failure mode

There is **no Bearer token, no session cookie, no simpler mode**. Every single request must be signed. An invalid signature returns `401 Unauthorized` with no further explanation.

---

## 2. API Key Scopes and Permissions

### Source
- **Kalshi Docs — Create API Key (OpenAPI):** https://docs.kalshi.com/api-reference/api-keys/create-api-key
- **Kalshi Docs — Generate API Key:** https://docs.kalshi.com/api-reference/api-keys/generate-api-key
- **Kalshi Docs — Get Balance:** https://docs.kalshi.com/api-reference/portfolio/get-balance

### Full scope list

`POST /trade-api/v2/api_keys` accepts a `scopes` array. Valid `ApiKeyScope` enum values:

| Scope | What it grants |
|-------|---------------|
| `read` | Parent scope — all read endpoints |
| `write` | Parent scope — all write endpoints (order placement, cancels, amends, RFQ, block trades, transfers) |
| `read::block_trade_accept` | Child scope — block trade accept reads only |
| `read::portfolio_balance` | Child scope — balance/portfolio balance reads only |
| `write::trade` | Child scope — order trading only |
| `write::transfer` | Child scope — fund transfers only |
| `write::fcm_risk` | FCM subtrader creation, trading blocks, daily premium caps, margin caps |
| `write::block_trade_accept` | Child scope — block trade accept writes only |

### Scope rules

- **Defaults:** If `scopes` is not provided, the key gets **full access** (`read`, `write`).
- **Parent→child dependency:** If the broad `write` parent scope is included, `read` must **also** be included. (The reverse is not required — child scopes can be granted without the parent.)
- **Child scopes without parent:** Child scopes such as `read::portfolio_balance`, `write::trade`, etc. may be granted **without** the broad parent scope — this is the principle of least privilege.
- **`read::portfolio_balance` specifically:** The `GET /trade-api/v2/portfolio/balance` endpoint accepts API keys with the `read::portfolio_balance` scope. This is useful for read-only monitoring keys that should not be able to place orders.

### What happens if a key lacks a scope

- If a key lacks `write` (or `write::trade`) and you attempt to place an order, you receive `403 Forbidden` — insufficient permissions.
- If a key lacks `read` (or `read::portfolio_balance`) and you attempt `GET /portfolio/balance`, you receive `403 Forbidden`.
- A key with only `read::portfolio_balance` can read balance but **cannot** read positions, orders, or markets, and **cannot** place trades.
- An FCM-subtrader-bound key (`fcm_subtrader_id`) is **denied on every REST endpoint**, including key management; it is for FIX order-entry and margin WebSocket sessions only.
- A subaccount-restricted key (`subaccount` set) may only read and trade on that sub-account; it cannot act on other sub-accounts or transfer funds.

---

## 3. Demo vs Production Environments

### Source
- **Kalshi Docs — API Environments and Endpoints:** https://docs.kalshi.com/getting_started/api_environments
- **Kalshi Docs — Test In The Demo Environment:** https://docs.kalshi.com/getting_started/demo_env
- **Kalshi Docs — Quick Start: Authenticated Requests:** https://docs.kalshi.com/getting_started/quick_start_authenticated_requests
- **Predictefy — Kalshi Demo API:** https://blog.predictefy.com/kalshi-demo-api

### Base URLs (REST Trade API)

| Environment | Recommended base URL | Also supported |
|-------------|---------------------|----------------|
| **Production** | `https://external-api.kalshi.com/trade-api/v2` | `https://api.elections.kalshi.com/trade-api/v2` |
| **Demo** | `https://external-api.demo.kalshi.co/trade-api/v2` | `https://demo-api.kalshi.co/trade-api/v2` |

The `external-api` hosts are dedicated to the external Trade API and are **recommended** for API traders. The shared hosts remain supported for compatibility with existing clients.

### WebSocket URLs

| Environment | Recommended URL | Also supported |
|-------------|-----------------|----------------|
| Production | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` | `wss://api.elections.kalshi.com/trade-api/ws/v2` |
| Demo | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` | `wss://demo-api.kalshi.co/trade-api/ws/v2` |

### Credentials are NOT shared

> "Kalshi provides separate production and demo environments. Credentials are not shared between environments, so demo API keys only work against demo endpoints and production API keys only work against production endpoints."

- **Demo API keys only work against demo endpoints.**
- **Production API keys only work against production endpoints.**
- A production key will **not** authenticate a demo request, and vice versa. This is a common source of 401 errors.
- The setup process is the same for both environments (log in, Account & security → API Keys → Create Key).
- Demo mirrors production semantics with simulated balances and markets — safe for development.

### Signing is host-independent

The signed path is identical regardless of which host you hit. E.g., for orders:

```
/trade-api/v2/portfolio/orders
```

Whether the full URL is `https://external-api.kalshi.com/trade-api/v2/portfolio/orders` or `https://external-api.demo.kalshi.co/trade-api/v2/portfolio/orders`, the signed string uses the same path.

---

## 4. Fee Model

### Source
- **Kalshi Fee Schedule (official PDF):** https://kalshi.com/docs/kalshi-fee-schedule.pdf
- **Allium — Kalshi Fees: Why the Cost Peaks Near 50 Cents:** https://www.allium.so/blog/kalshi-fees-why-the-cost-peaks-near-50-cents/
- **Predictefy — Kalshi Fees Explained (2026):** https://blog.predictefy.com/kalshi-fees
- **Kalshi Help Center — Fees:** https://help.kalshi.com/en/articles/13823805-fees
- **BotForKalshi / QuantVPS / OddsShopper / PredictReport** fee explainers (search results)

### Taker fee (general event contracts)

```
fee = round_up( 0.07 × contracts × price × (1 − price) )
```

- `contracts` = number of contracts (C)
- `price` = price per contract in dollars (P), e.g. 0.56 for 56¢
- `0.07` = the general fee multiplier (7%)
- `P × (1 − P)` peaks at P = 0.50 (value 0.25), drops toward zero at P → 0 or P → 1
- **Rounding:** fee is **rounded up to the next cent** ($0.01). On accounts held directly with Kalshi, API docs say rounding is to a hundredth of a cent ($0.0001) with overpayment rebated across an order's fills; accounts cleared through a broker round to the cent ($0.01).
- Charged **per order** (on taker/marketable orders), not per contract individually.

### Cost examples (100 contracts)

| Contract price | P × (1−P) | Raw fee | Fee charged (rounded up) | Fee as % of stake |
|----------------|-----------|---------|--------------------------|-------------------|
| 10¢ (0.10) | 0.09 | $0.63 | $0.63 | 6.3% |
| 25¢ (0.25) | 0.1875 | $1.3125 | $1.32 | 5.3% |
| 50¢ (0.50) | 0.25 | $1.75 | $1.75 | 3.5% |
| 75¢ (0.75) | 0.1875 | $1.3125 | $1.32 | 1.8% |
| 90¢ (0.90) | 0.09 | $0.63 | $0.63 | 0.7% |

- **Peak:** ~1.75¢ per contract at 50¢ (the "coin-flip" market — maximum uncertainty).
- **Symmetric:** buying YES at 90¢ costs the same fee as buying NO at 10¢ (mirror images).
- Small orders pay proportionally more due to the round-up ceiling.

### Maker vs taker

| Fee type | Rate | When it applies |
|----------|------|-----------------|
| **Taker** | `0.07 × C × P × (1 − P)`, rounded up | Orders that are immediately matched against resting liquidity |
| **Maker** | **25% of the taker rate** | Resting limit orders that later fill, **only on markets designated as maker-fee markets** |

- Maker fees apply **only on designated maker-fee markets**; many markets charge makers **nothing at all**.
- Canceling a resting order is **always free**.
- A resting limit order at the same price can cost a quarter of the taker fee, or zero, depending on the market — the single biggest lever for reducing fees.

### Other costs (not part of the trading formula)

- **Settlement:** No separate settlement fee. Contracts resolve at $1.00 (yes) or $0.00 (no). The trading fee paid on entry is the whole cost of a hold-to-settlement position.
- **Deposits:** ACH free; debit card up to 2%; wire free but $1,000 minimum (under $1,000 returned).
- **Withdrawals:** ACH and debit free (at time of writing).
- **Membership / API:** No subscriptions, no inactivity fees, **free API access**.
- **Perpetual futures:** Separate product with volume-tiered maker/taker rates on **notional** value — does **not** use the 0.07 × P × (1−P) formula. Requires application.

### Caveats

- The `0.07` multiplier is the **current general baseline**, not a permanent constant. Certain product families (some financial index event contracts, special events like elections/awards/championships) carry their own separate fee schedules.
- Kalshi's API reports a per-series fee multiplier; some series run below the standard rate.
- **Always pull the live official fee schedule PDF** before sizing any strategy on a specific series — the schedule is updated over time.

---

## 5. REST API Rate Limits

### Source
- **Kalshi Docs — Rate Limits and Tiers:** https://docs.kalshi.com/getting_started/rate_limits
- **Kalshi Docs — Get Balance (own bucket):** https://docs.kalshi.com/api-reference/portfolio/get-balance
- **Kalshi Docs — List non-default endpoint costs:** https://docs.kalshi.com/api-reference/account/list-non-default-endpoint-costs
- **BotForKalshi / Predictefy / QuantVPS / Kairos** rate-limit explainers (search results)

### Token-based model

Every authenticated request costs **tokens**. Your tier sets a per-second **budget** (refill rate) for each bucket. Sustained rate for an endpoint = `budget ÷ cost`.

- **Default cost: 10 tokens** per request.
- Non-default costs are listed via `GET /trade-api/v2/account/endpoint_costs`.
- Most order-related requests cost 10 tokens (one order per 10 tokens).

### Two independent buckets

| Bucket | Covers |
|--------|--------|
| **Read** | `GET` endpoints and anything not routed to Write |
| **Write** | Order placement, amends, cancels, order groups, RFQ quote flow, block trade proposal accepts |

The split is by operation type, not protocol — **REST and FIX drain the same buckets**.

### Balance endpoint — its own bucket

`GET /trade-api/v2/portfolio/balance` is metered in **its own bucket**, separate from Read:
- **Basic & Advanced tiers:** 1 call per second
- **All higher tiers:** 5 calls per second
- Bucket holds **one second of budget** — no burst.
- Balance calls do **not** draw down Read budget, and other reads do not draw down this bucket.
- Balance only changes on fills, settlements, and transfers — react via the user-fills WebSocket channel rather than polling.

### Bucket capacity and bursting

| Bucket type | Capacity |
|-------------|----------|
| Basic & Advanced Predictions Read | 3 seconds of budget |
| Write buckets above Basic tier | 3 seconds of budget |
| Predictions Read buckets above Advanced | 1 second of budget |
| Perps Read buckets | 1 second of budget |
| Basic-tier Write buckets | 1 second of budget |

- Buckets refill continuously at the per-second budget rate, up to capacity.
- No fixed windows, no per-second resets.
- After 3 quiet seconds, a 3-second-capacity bucket is full, allowing up to **3× the per-second budget in a single burst**.
- Basic Write buckets hold only 1 second — spend a full second's budget at once, idle time banks nothing beyond that.

### Per-second token budgets (Predictions / event contracts)

| Tier | Read budget | Write budget | Sustained orders/sec (Write ÷ 10) |
|------|-------------|--------------|-----------------------------------|
| Basic | 200 | 100 | ~10 |
| Advanced | 300 | 300 | ~30 |
| Expert | 600 | 600 | ~60 |
| Premier | 1,200 | 1,200 | ~120 |
| Paragon | 2,400 | 2,400 | ~240 |
| Prime | 4,800 | 4,800 | ~480 |
| Prestige | 12,000 | 9,600 | ~960 |

Write bucket capacity = 3× per-second budget (above Basic tier).

### Perps budgets (separate from event contracts)

| Tier | Read budget | Write budget |
|------|-------------|--------------|
| Basic | 400 | 100 |
| Advanced | 600 | 300 |
| Expert | 600 | 600 |
| Premier | 1,000 | 1,000 |
| Paragon | 2,000 | 2,000 |
| Prime | 4,000 | 4,000 |
| Prestige | 6,000 | 8,000 |

Perps and event-contract budgets are **independent** — neither draws down the other.

### Shard-scoped writes (exchange sharding)

A single order write targeting an exchange shard (`exchange_index >= 1`) draws from that shard's Write bucket (full tier budget per shard). Shard 0 uses the unscoped Write bucket. Auto-routed traffic (`exchange_index: -1` or omitted with `market_ticker`) bills **every shard's** Write bucket. Batch REST creates/cancels always bill to the **unscoped** Write bucket regardless of `exchange_index`.

### When you hit the limit

- Rate-limited request returns **`429 Too Many Requests`** with body `{"error": "too many requests"}`.
- **No `Retry-After` or `X-RateLimit-*` headers** are currently included.
- **No penalty or cooldown.** The bucket keeps refilling; next request succeeds once balance covers cost.
- At 1,200 tokens/sec refill, an empty bucket replenishes 10 tokens (one order) in ~8.3 ms.
- **Apply exponential backoff on 429.**

### Batch endpoints don't save tokens

A batch costs the same as individual calls — every item billed separately:
- Batch Create 25 orders = 25 × 10 = 250 tokens (whole batch must fit in bucket at once, or entire batch is rejected).
- Batch Cancel 25 orders = 25 × 2 = 50 tokens.

### WebSocket connection limits (per user, concurrent open connections)

| Tier | Max connections |
|------|-----------------|
| Basic | 200 |
| Advanced | 200 |
| Expert | 200 |
| Premier | 500 |
| Paragon | 750 |
| Prime | 1,000 |
| Prestige | 1,000 |

New WebSocket handshakes return `429` when the limit is reached.

### Tier qualification

- **Basic:** complete account signup.
- **Advanced:** call the Upgrade Account API Usage Level endpoint.
- **Expert, Premier, Paragon, Prime, Prestige:** earned automatically from trailing 30-day trading volume (volume share = your 30-day volume ÷ (previous month's exchange volume × 2)), or assigned manually by Kalshi. Earn/Keep thresholds: Expert 0.075%/0.05%, Premier 0.125%/0.10%, Paragon 0.25%/0.20%, Prime 0.50%/0.40%, Prestige 1.00%/0.80%. Grants last 30 days and renew while qualifying.

---

## 6. Order Placement — Limit Orders, Time-in-Force, Self-Trade Prevention

### Source
- **Kalshi Docs — Create Order (V2):** https://docs.kalshi.com/api-reference/orders/create-order-v2
- **Kalshi Docs — Batch Create Orders (V2):** https://docs.kalshi.com/api-reference/orders/batch-create-orders-v2
- **Kalshi Docs — Quick Start: Create your first order:** https://docs.kalshi.com/getting_started/quick_start_create_order
- **Kalshi Docs — Fix Order Entry:** https://docs.kalshi.com/fix/order-entry

### Endpoint migration

- **V2 endpoint:** `POST /trade-api/v2/portfolio/events/orders` (single-book `bid`/`ask` side, fixed-point dollar prices).
- **Legacy endpoint:** `POST /trade-api/v2/portfolio/orders` — **deprecated no earlier than May 6, 2026**. Clients should migrate to V2.
- Both are authenticated; signing follows the same rules (path includes `/trade-api/v2`).

### V2 Create Order request fields (required)

| Field | Type | Description |
|-------|------|-------------|
| `ticker` | string (required) | Market ticker, e.g. `HIGHNY-24JAN01-T60` |
| `side` | `bid` \| `ask` (required) | `bid` = buy YES, `ask` = sell YES (YES leg only; selling YES ≡ buying NO at 1−price) |
| `count` | FixedPointCount string (required) | Quantity in contracts, 2 decimals, e.g. `"10.00"`. Min granularity 0.01. Fractional supported. |
| `price` | FixedPointDollars string (required) | Price in fixed-point dollars, e.g. `"0.5600"`. Requests accept 2–4 decimals. |
| `time_in_force` | enum (required) | `fill_or_kill`, `good_till_canceled`, `immediate_or_cancel` |
| `self_trade_prevention_type` | enum (required) | `taker_at_cross`, `maker` |

### Optional V2 fields

| Field | Type | Description |
|-------|------|-------------|
| `client_order_id` | string | Client-generated UUID for idempotency/tracking |
| `expiration_time` | int64 (seconds) | Unix timestamp for expiration; only with `time_in_force: good_till_canceled`. Cannot combine with `immediate_or_cancel`. `GTT` is not a valid API value. |
| `post_only` | boolean | Post-only (maker-only) order |
| `cancel_order_on_pause` | boolean | Cancel order if trading is paused |
| `reduce_only` | boolean | Caps order place count by current position; requires `time_in_force: immediate_or_cancel` |
| `subaccount` | integer 0–63 | Subaccount number; 0 = primary. Subaccount-restricted keys must omit or pass their locked subaccount. |
| `order_group_id` | string | Order group this order is part of |
| `exchange_index` | integer | Exchange shard index. Omitted = auto-route when ticker provided; `-1` = require auto-routing. |

### Time-in-force values

| Value | Meaning |
|-------|---------|
| `fill_or_kill` (FOK) | Must fill entirely or be canceled immediately |
| `good_till_canceled` (GTC) | Remains active until filled or canceled (use `expiration_time` for a timed GTC) |
| `immediate_or_cancel` (IOC) | Fill whatever is possible immediately, cancel the rest |

### Self-trade prevention types

| Value | Behavior |
|-------|----------|
| `taker_at_cross` | Cancels the **taker** order when it would trade against another order from the same user. Execution stops; any partial fills already matched are executed. |
| `maker` | Cancels the **resting maker** order and continues matching. |

### V2 response fields

| Field | Description |
|-------|-------------|
| `order_id` | UUID of the created order |
| `client_order_id` | Echo of client-supplied ID |
| `fill_count` | Contracts filled immediately upon placement |
| `remaining_count` | Contracts remaining after placement |
| `average_fill_price` | VWAP of fills (only when fill_count > 0) |
| `average_fee_paid` | VWAP fee per contract (only when fill_count > 0) |
| `ts_ms` | Matching engine timestamp (Unix epoch ms) |

### Rate limit cost

- Create Order V2: **10 tokens per order** (default cost) from the Write bucket.
- Batch Create Orders: 10 tokens per order in the batch; whole batch must fit in bucket at once.

### Order lifecycle gotchas

- `reduce_only` orders **must** use `immediate_or_cancel`; otherwise rejected.
- `immediate_or_cancel` cannot be combined with `expiration_time`.
- `GTT` is an internal execution type, **not** a valid API `time_in_force` value.
- Prices must be within the market's valid quote intervals (constrained by the market's price level structure).
- Price `1`–`99` cents; invalid price → `400 Bad Request`.

---

## 7. Historical Data Endpoints

### Source
- **Kalshi Docs — Historical Data:** https://docs.kalshi.com/getting_started/historical_data
- **Kalshi Docs — Get Historical Market Candlesticks:** https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks
- **Kalshi Docs — Get Historical Cutoff Timestamps:** https://docs.kalshi.com/api-reference/historical/get-historical-cutoff-timestamps

### Overview

Kalshi partitions exchange data into **live** and **historical** tiers. Older settled markets, trades, orders, fills, and positions move from live endpoints to separate historical endpoints. Events and Series **always** remain available through their original endpoints.

### Cutoff timestamps — `GET /historical/cutoff`

Returns current cutoff timestamps per data type:

| Field | Partitioned by | Meaning |
|-------|---------------|---------|
| `market_settled_ts` | Market settlement time | Markets + candlesticks settled before this → historical only |
| `trades_created_ts` | Trade fill time | Trades before this → historical only (`/historical/trades`) |
| `orders_updated_ts` | Order cancel/execution time | Canceled/fully executed orders before this → historical only (`/historical/orders`) |
| `market_positions_last_updated_ts` | Position last-update time | Backfill horizon for settled positions → `/historical/positions` |

Cutoffs advance over time; each data type has its own cutoff — read the field for the data you need.

### Historical endpoints

| Endpoint | Description | Auth |
|----------|-------------|------|
| `GET /historical/cutoff` | Current cutoff timestamps | Public (no auth) |
| `GET /historical/markets` | Settled markets older than cutoff | Public |
| `GET /historical/markets/{ticker}` | Single historical market by ticker | Public |
| `GET /historical/markets/{ticker}/candlesticks` | Candlestick data for historical markets | Public |
| `GET /historical/trades` | All trades older than cutoff | Public |
| `GET /historical/fills` | User-scoped trade fills older than cutoff | Authenticated |
| `GET /historical/orders` | Canceled/executed orders older than cutoff | Authenticated |
| `GET /historical/positions` | User-scoped settled positions archived from live | Authenticated |

### `GET /historical/markets/{ticker}/candlesticks` — exact spec

**Path:** `/trade-api/v2/historical/markets/{ticker}/candlesticks`  
**Method:** GET  
**Auth:** None required (public endpoint)

**Query parameters (all required):**

| Parameter | Type | Description |
|-----------|------|-------------|
| `ticker` | path string | Market ticker |
| `start_ts` | int64 (Unix seconds) | Candlesticks ending on or after this time |
| `end_ts` | int64 (Unix seconds) | Candlesticks ending on or before this time |
| `period_interval` | int | Candle length in minutes: `1` (1 min), `60` (1 hour), `1440` (1 day) |

**Response fields (per candlestick):**

| Field | Type | Description |
|-------|------|-------------|
| `end_period_ts` | int64 | Inclusive end of candle period (Unix ms) |
| `yes_bid` | BidAskDistribution | OHLC for YES buy offers (open, low, high, close in dollars) |
| `yes_ask` | BidAskDistribution | OHLC for YES sell offers |
| `price` | PriceDistribution | OHLC + mean + previous for trade prices (open, low, high, close, mean, previous — all nullable) |
| `volume` | FixedPointCount string | Contracts bought during the period |
| `open_interest` | FixedPointCount string | Contracts held by end of period |

**Pagination:** Historical endpoints support the same cursor-based pagination as live counterparts.

### Live endpoints affected by archival

| Live endpoint | Stops returning after cutoff |
|---------------|------------------------------|
| `GET /markets`, `GET /markets/{ticker}` | Settled markets + candlesticks older than cutoff |
| `GET /events` (with_nested_markets=true) | Nested markets older than cutoff |
| `GET /markets/trades` | Trades older than cutoff |
| `GET /portfolio/fills` | Fills older than cutoff |
| `GET /portfolio/orders` | Completed/canceled orders older than cutoff (resting orders unaffected) |
| `GET /portfolio/positions` | Defaults to unsettled; settled positions still live need `settlement_status=settled` or `all` |

### Migration guidance

1. Call `GET /historical/cutoff` first.
2. Query both live and historical endpoints for complete history (positions: live first, then historical; deduplicate by subaccount + ticker).
3. `GET /portfolio/settlements` reads only settled positions still in the live data set — after archival, use `GET /historical/positions` (no historical endpoint with the same settlement-record fields yet).

---

## 8. Known Authentication Issues and Gotchas

### Source
- **Kalshi Docs — Quick Start: Authenticated Requests (Common Issues):** https://docs.kalshi.com/getting_started/quick_start_authenticated_requests
- **Kalshi Docs — API Keys:** https://docs.kalshi.com/getting_started/api_keys
- **Kalshi Docs — API Environments (Request Signing):** https://docs.kalshi.com/getting_started/api_environments
- **PredictAndProfit — Kalshi API Python Guide:** https://predictandprofit.io/blog/kalshi-api-python-guide
- **BotForKalshi — Kalshi API Tutorial:** https://www.botforkalshi.com/blog/kalshi-api-tutorial
- **QuantVPS — Kalshi API Guide:** https://www.quantvps.com/blog/kalshi-order-book-api-endpoints-explained
- **Reddit r/Kalshi — "Is the Kalshi API Bugged…"**: https://www.reddit.com/r/Kalshi/comments/1odbtdi/is_the_kalshi_api_bugged_or_am_i_just_doing/

### Gotcha 1 — Path double-prefixing (very common)

If your `BASE_URL` already ends with `/trade-api/v2`, pass only the endpoint path to your helper (e.g. `/portfolio/balance`), **not** `/trade-api/v2/portfolio/balance`. Otherwise the request URL becomes `.../trade-api/v2/trade-api/v2/portfolio/balance` → 404 / path-not-found, and the signature won't match either.

### Gotcha 2 — Query parameters in the signed path

Always strip `?` and everything after it from the path before signing. Signing the full URL including query string produces a signature mismatch → `401`. This only fails on requests **with** parameters, making it hard to debug.

### Gotcha 3 — Timestamp in seconds instead of milliseconds

The signed timestamp must be milliseconds. Using seconds → `401 Unauthorized`. Kalshi rejects timestamps more than a few seconds old.

### Gotcha 4 — Clock drift / NTP

Kalshi rejects requests with timestamps more than a few seconds old. If your server clock is not NTP-synced, **every** request fails with `401`. Generate the timestamp fresh per request (`time.time() * 1000`); never cache it at startup.

### Gotcha 5 — Wrong key type / PEM header ambiguity

- Ed25519 signs the message directly; RSA uses RSA-PSS with SHA-256 (MGF1 SHA-256, salt length = digest length).
- The PEM header does **not** identify key type — PKCS#8 RSA keys (`openssl genpkey`) also start with `-----BEGIN PRIVATE KEY-----`. Determine type from the parsed key object and sign accordingly.
- Mismatched algorithm → signature verification fails → `401`.

### Gotcha 6 — Demo vs production credential mismatch

Credentials are **not shared** between environments. A production key will not authenticate a demo request (and vice versa) → `401`. Double-check which environment your key was created in and match the base URL.

### Gotcha 7 — Wrong Key ID

The Key ID is a UUID; copy it exactly. There is no validation error for a wrong Key ID — you just get `401` on every request.

### Gotcha 8 — Missing required scope

If a key lacks `write`/`write::trade` and you try to place an order → `403 Forbidden`. If it lacks `read`/`read::portfolio_balance` and you read balance → `403 Forbidden`. Scope errors can be mistaken for auth errors.

### Gotcha 9 — Method case

The HTTP method in the signed string must be uppercase (`GET`, `POST`). Lowercase `get`/`post` → signature mismatch → `401`.

### Gotcha 10 — No separator in signed message

The signed message is `timestamp + method + path` with **no spaces, colons, or separators**. Adding a space (e.g. `1703123456789 GET /path`) → signature mismatch → `401`.

### Gotcha 11 — 429 has no Retry-After

Rate-limited responses return `429` with body `{"error": "too many requests"}` and **no `Retry-After` or `X-RateLimit-*` headers**. You must implement your own exponential backoff; do not rely on server-provided retry timing.

### Gotcha 12 — Legacy endpoint deprecation

`POST /trade-api/v2/portfolio/orders` (V1) is deprecated no earlier than May 6, 2026. Migrate to `POST /trade-api/v2/portfolio/events/orders` (V2) which uses `bid`/`ask` side and fixed-point dollar prices.

### Gotcha 13 — FCM subtrader-bound keys denied on REST

An API key bound to an FCM subtrader (`fcm_subtrader_id`) is **denied on every REST endpoint**, including key management. It is for FIX order-entry and margin WebSocket sessions only. Do not attempt REST calls with such a key.

### Gotcha 14 — Balance polling vs event-driven

`GET /portfolio/balance` has its own tiny bucket (1/sec Basic/Advanced, 5/sec higher). Balance only changes on fills, settlements, transfers — poll sparingly; use the user-fills WebSocket channel instead.

### Gotcha 15 — Signature message must match the actual request path

The signed path must exactly match the path the API receives. If your HTTP client normalizes or redirects the path differently than what you signed, you get `401`. Sign the path **as the server will see it** (after any proxy/redirect normalization).

---

## 9. Quick Reference: Minimal Signed Request (Python)

```python
import base64, datetime, requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BASE_URL = "https://external-api.demo.kalshi.co"  # prod: external-api.kalshi.com
API_KEY_ID = "your-key-id"
PRIVATE_KEY_PATH = "kalshi.key"

def load_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def sign(private_key, timestamp_ms, method, path):
    path_no_query = path.split("?")[0]          # strip query params
    message = f"{timestamp_ms}{method.upper()}{path_no_query}".encode("utf-8")
    if isinstance(private_key, Ed25519PrivateKey):
        sig = private_key.sign(message)
    else:
        sig = private_key.sign(message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())
    return base64.b64encode(sig).decode()

def request(method, path, base_url=BASE_URL, **kwargs):
    pk = load_key(PRIVATE_KEY_PATH)
    ts = str(int(datetime.datetime.now().timestamp() * 1000))  # ms
    sig_path = path.split("?")[0]                          # sign path w/o query
    signature = sign(pk, ts, method, sig_path)
    headers = {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "Content-Type": "application/json",
    }
    return requests.request(method, base_url + path, headers=headers, **kwargs)

# Example: get balance
# resp = request("GET", "/trade-api/v2/portfolio/balance")
```

**Key rules to remember:**
1. Sign `timestamp_ms + METHOD + /trade-api/v2/.../path` (no separators, path includes `/trade-api/v2` prefix).
2. Strip `?query` from the path before signing.
3. Timestamp in **milliseconds**, fresh per request.
4. Method **uppercase**.
5. Signature **base64-encoded**.
6. Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`.
7. Demo and production keys are **not interchangeable**.

---

## Summary of Source URLs

| Topic | URL |
|-------|-----|
| API Keys / signing | https://docs.kalshi.com/getting_started/api_keys |
| Quick Start: Authenticated Requests | https://docs.kalshi.com/getting_started/quick_start_authenticated_requests |
| API Environments & Endpoints | https://docs.kalshi.com/getting_started/api_environments |
| Rate Limits & Tiers | https://docs.kalshi.com/getting_started/rate_limits |
| Create Order (V2) | https://docs.kalshi.com/api-reference/orders/create-order-v2 |
| Historical Data | https://docs.kalshi.com/getting_started/historical_data |
| Get Historical Market Candlesticks | https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks |
| Get Balance | https://docs.kalshi.com/api-reference/portfolio/get-balance |
| Create API Key (scopes) | https://docs.kalshi.com/api-reference/api-keys/create-api-key |
| List endpoint costs | https://docs.kalshi.com/api-reference/account/list-non-default-endpoint-costs |
| Kalshi Fee Schedule (PDF) | https://kalshi.com/docs/kalshi-fee-schedule.pdf |
| Kalshi Help Center — Fees | https://help.kalshi.com/en/articles/13823805-fees |
| Allium — Kalshi Fees deep-dive | https://www.allium.so/blog/kalshi-fees-why-the-cost-peaks-near-50-cents/ |
| Predictefy — Kalshi Fees | https://blog.predictefy.com/kalshi-fees |
| Predictefy — Kalshi Demo API | https://blog.predictefy.com/kalshi-demo-api |
| PredictAndProfit — Kalshi API Python Guide | https://predictandprofit.io/blog/kalshi-api-python-guide |
| BotForKalshi — API Tutorial | https://www.botforkalshi.com/blog/kalshi-api-tutorial |
| QuantVPS — Kalshi API Guide | https://www.quantvps.com/blog/kalshi-order-book-api-endpoints-explained |
