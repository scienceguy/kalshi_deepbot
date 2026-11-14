# Polymarket US WebSocket Stream Integration — Technical Spec

**Date:** 2026-11-14
**Status:** draft
**Owner:** termux:opencode:kalshi_deepbot
**Venue:** Polymarket US (`api.polymarket.us`) — authenticated, Ed25519 API keys
**Scope:** Real-time top-of-book (BBO) quotes for active hourly and 15m crypto markets; auto-reconnecting async worker.

---

## 1. WebSocket specification (from venue docs)

### 1.1 Endpoints

| Path | Full URL | Carries | Auth |
| --- | --- | --- | --- |
| `/v1/ws/markets` | `wss://api.polymarket.us/v1/ws/markets` | Order book, best bid/offer, trades, market state | API key |
| `/v1/ws/private` | `wss://api.polymarket.us/v1/ws/private` | Order fills, position changes, balance updates | API key |

This worker uses `/v1/ws/markets` only (read-only market data). The two sockets are separate surfaces: a single connection can never carry both market data and private account events.

### 1.2 Authentication (WebSocket handshake)

Same API-key authentication as the REST API. Three headers sent with the WebSocket upgrade request:

| Header | Value |
| --- | --- |
| `X-PM-Access-Key` | API key ID |
| `X-PM-Timestamp` | Epoch milliseconds (integer as string) |
| `X-PM-Signature` | Base64(Ed25519-sign(`timestamp + "GET" + path`)) |

`path` is `/v1/ws/markets` or `/v1/ws/private`. The secret key is a base64-encoded Ed25519 private key; a 64-byte decoded key is truncated to the 32-byte seed. Signature construction and header names are implemented in the installed SDK at `polymarket_us.auth.create_auth_headers` and reused by this worker — no custom signing code.

Credentials resolve in this order (matching `polymarket_us_client.py`):
1. `config.POLYMARKET_KEY_ID` / `config.POLYMARKET_SECRET_KEY` (env-backed)
2. `POLYMARKET_KEY_ID` / `POLYMARKET_SECRET_KEY` environment variables
3. Bitwarden mirror `~/.secrets/bw/polymarket_api_key.json` / `polymarket_secret_key.json` (field `value`)

Never log or print credential values.

### 1.3 Subscription framing (client → server)

All messages are JSON, camelCase field names on the wire.

Subscribe:

```json
{
  "subscribe": {
    "requestId": "<unique-id>",
    "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE",
    "marketSlugs": ["cpc-btc-updown-15m-...", "cpc-eth-updown-15m-..."]
  }
}
```

Unsubscribe (references the original `requestId`):

```json
{
  "unsubscribe": {
    "requestId": "<original-request-id>"
  }
}
```

Market subscription types (markets endpoint):

| Value | Meaning |
| --- | --- |
| `SUBSCRIPTION_TYPE_MARKET_DATA` | Full order book + market stats |
| `SUBSCRIPTION_TYPE_MARKET_DATA_LITE` | Lightweight BBO + price (top-of-book) |
| `SUBSCRIPTION_TYPE_TRADE` | Real-time trade notifications |

Limits & rules:
- Maximum 100 markets per subscription; use multiple subscriptions beyond that.
- `requestId` must be unique per subscription and is echoed on the ack; reuse it to unsubscribe.
- A market data type requires at least one `marketSlugs` entry.
- Unknown fields may cause the frame to be rejected; send only documented fields.

### 1.4 Messages (server → client)

Top-of-book payload (`SUBSCRIPTION_TYPE_MARKET_DATA_LITE`):

```json
{
  "requestId": "bbo-1",
  "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE",
  "marketDataLite": {
    "marketSlug": "cpc-btc-updown-15m-...",
    "currentPx": {"value": "0.55", "currency": "USD"},
    "lastTradePx": {"value": "0.55", "currency": "USD"},
    "bestBid": {"value": "0.54", "currency": "USD"},
    "bestAsk": {"value": "0.56", "currency": "USD"},
    "bidDepth": 5,
    "askDepth": 4,
    "sharesTraded": "150000",
    "openInterest": "500000"
  }
}
```

Full book (`SUBSCRIPTION_TYPE_MARKET_DATA`) adds `bids`/`offers` arrays (best-to-worst, `px`/`qty` per level), `state`, `stats` (lastTradePx, sharesTraded, openInterest, highPx, lowPx), `transactTime`.

Trade:

```json
{
  "requestId": "trade-1",
  "subscriptionType": "SUBSCRIPTION_TYPE_TRADE",
  "trade": {
    "marketSlug": "...",
    "price": {"value": "0.555", "currency": "USD"},
    "quantity": {"value": "0.50", "currency": "USD"},
    "tradeTime": "2024-01-15T10:30:00Z",
    "maker": {"side": "ORDER_SIDE_BUY", "intent": "ORDER_INTENT_BUY_LONG"},
    "taker": {"side": "ORDER_SIDE_SELL", "intent": "ORDER_INTENT_SELL_LONG"}
  }
}
```

Heartbeat (application level):

```json
{"heartbeat": {}}
```

Error:

```json
{"requestId": "...", "error": "Error description", "subscriptionType": "..."}
```

Market states: `MARKET_STATE_OPEN`, `PREOPEN`, `SUSPENDED`, `HALTED`, `EXPIRED`, `TERMINATED`, `MATCH_AND_CLOSE_AUCTION`.

### 1.5 Heartbeat / PING-PONG

Two layers:

1. **Transport layer** — the `websockets` client sends WebSocket `Ping` frames automatically (`ping_interval`) and expects `Pong` within `ping_timeout`; failure closes the connection and triggers reconnect. This is the standard PING/PONG keep-alive.
2. **Application layer** — the server periodically emits `{"heartbeat": {}}`. The worker tracks the last application heartbeat and treats a silent socket (no heartbeat or data within the heartbeat timeout) as stalled, forcing a reconnect even if the transport is technically open.

The worker does **not** need to send an application-level PONG; responding to transport Pings and monitoring application heartbeats satisfies the requirement ("respond to heartbeats or implement your own keep-alive").

### 1.6 Debouncing

`responsesDebounced: true` in a subscribe frame batches updates at regular intervals instead of every change. Useful for high-frequency markets to reduce message volume. Disabled by default in this worker (freshest quote preferred).

---

## 2. Worker design

### 2.1 Public API

`polymarket_ws.PolymarketWSWorker`

```python
worker = PolymarketWSWorker(
    market_slugs=[...],          # slugs to stream top-of-book for
    on_quote=callback,           # async callable(MarketQuote) -> None
    on_event=callback,           # optional async callable(dict) for trades/errors/state
    heartbeat_timeout=30.0,      # seconds of socket silence before forced reconnect
    reconnect_max_attempts=None, # None = retry forever
    reconnect_base=1.0,          # initial backoff seconds
    reconnect_max=60.0,          # cap
    ping_interval=20.0,          # transport Ping interval (seconds)
    ping_timeout=30.0,           # transport Pong timeout (seconds)
)
await worker.start()             # connects, subscribes, runs until stop()
await worker.stop()              # clean shutdown
```

`MarketQuote` (dict): `market_slug`, `best_bid`, `best_ask`, `last_trade_px`, `bid_depth`, `ask_depth`, `shares_traded`, `open_interest`, `state`, `transact_time`, `received_at` (monotonic ts), `currency`.

Prices are floats in dollars (0–1) parsed from `{"value": "...", "currency": "USD"}`.

### 2.2 Reconnection & resilience

- On any disconnect (transport close, handshake failure, heartbeat timeout, unhandled error), the worker stops the current socket, waits an exponential backoff (`base * 2^(n-1)`, jittered, capped at `max`), then reconnects.
- **Re-subscription on reconnect:** every active subscription is re-issued automatically after a new handshake. Callers do not need to re-subscribe.
- `reconnect_max_attempts` bounds retries; when exhausted the worker stops and reports `stopped`. `None` = retry forever (suitable for a long-running trading loop).
- First failure retries immediately (no backoff); subsequent failures back off — mirrors `resilience.CycleSupervisor`.
- Authentication errors (401/403 / bad signature) are **not** retried endlessly: after a configurable number of consecutive auth failures the worker stops, since re-sending bad creds cannot succeed. Transient network errors keep retrying.

### 2.3 Concurrency model

Single `asyncio` task owns the socket lifecycle (`_run_loop`). Quote callbacks are invoked on that task. A slow `on_quote` blocks the socket reader — callers must keep `on_quote` non-blocking (enqueue to an `asyncio.Queue`, never await network/DB inside the callback). If a queue-based fan-out is needed, the caller provides it.

Thread-safety: `start()`/`stop()` and `subscribe()`/`unsubscribe()` are safe to call from other threads via `asyncio.run_coroutine_threadsafe`; the socket itself is only touched by the owner task.

### 2.4 Market selection

The worker subscribes to the slugs it is given. It does **not** discover markets itself. The caller obtains active hourly/15m crypto slugs from the public REST API (`polymarket_us_client.get_events` / `search_events`, filtered to crypto up/down 15m/1h) and passes them in. Slugs can be added/removed at runtime via `subscribe()`/`unsubscribe()`, which take effect immediately when connected and are queued for the next connect otherwise.

Example slug shape (from existing integration): `cpc-btc-updown-15m-...` — the **market slug**, not the event slug.

---

## 3. WebSocket streaming vs 15-second REST polling

| Dimension | WebSocket (`/v1/ws/markets`) | REST polling every 15s |
| --- | --- | --- |
| **Connection** | 1 persistent TLS socket per worker (all markets multiplexed) | 1+ HTTP requests per market every 15s |
| **Data freshness** | Update pushed as it happens; subsecond–low-seconds latency | Stale up to nearly 15s plus request RTT |
| **Per-poll overhead** | None (incremental JSON frames only on change) | Full HTTP request/response headers + redundant body every 15s |
| **Redundant traffic** | None — only deltas/changes are sent | Full book/BBO re-fetched even when nothing changed |
| **Requests per market per minute** | ~0 after initial subscribe (event-driven) | 4 |
| **CPU** | Low — event loop wakes only on frames | Higher — repeated TLS/HTTP + JSON parse per poll |
| **Reconnect surface** | One socket to manage and heal | Each poll is independent; a failure loses one cycle |
| **Best for** | Live top-of-book streaming, low latency | Infrequent reference checks, one-off snapshots |

### 3.1 Illustrative cost model (estimates, not live measurements)

Assume `M` markets, quote changes at average rate `c` changes/minute/market, lite payload ≈ 220 bytes, REST BBO response ≈ 600 bytes + ~400 bytes HTTP headers.

| Metric | WebSocket | REST 15s polling |
| --- | --- | --- |
| Connections | 1 persistent | `M` requests every 15s (can reuse keep-alive connection) |
| Requests/minute | ~0 after subscribe | `4 × M` |
| Payload/minute (steady, no changes) | ~0 (heartbeats only) | `4 × M × 1000` bytes ≈ `4M KB` |
| Payload/minute (c changes/market/min) | `c × M × 220` bytes | `4 × M × 1000` bytes (unchanged regardless of activity) |
| Worst-case staleness | Network delay (ms–s) | ~15s + RTT |
| Handle `M` = 20 markets, `c` = 5 | ~2.2 KB/min | ~80 KB/min, staleness up to 15s |

WebSocket wins on latency and bandwidth whenever `c < ~18` changes/min/market — i.e. essentially always for top-of-book on these markets — and it is the only option that delivers sub-15s freshness without hammering the venue.

### 3.2 When REST polling is still appropriate

- One-off reference price (health check, settlement lookup).
- Markets not worth a persistent socket (rarely traded, low-priority).
- As a fallback when WebSocket credentials are unavailable (public market data endpoints do not require auth).

---

## 4. Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `POLYMARKET_WS_ENABLED` | `false` | Master switch for the WS worker |
| `POLYMARKET_WS_MARKETS` | `""` | Comma-separated market slugs to stream |
| `POLYMARKET_WS_HEARTBEAT_TIMEOUT` | `30` | Seconds of socket silence before forced reconnect |
| `POLYMARKET_WS_RECONNECT_MAX_ATTEMPTS` | `""` (unlimited) | Max reconnect attempts; empty = unlimited |
| `POLYMARKET_WS_RECONNECT_BASE` | `1.0` | Initial backoff seconds |
| `POLYMARKET_WS_RECONNECT_MAX` | `60.0` | Cap on backoff seconds |
| `POLYMARKET_KEY_ID`, `POLYMARKET_SECRET_KEY` | — | Ed25519 API key (shared with REST client) |

Credentials follow the existing resolution order (config → env → BW mirror).

---

## 5. Risks & open items

- **Auth failures are not self-healing.** A rotated/stale key produces repeated 401s; the worker stops after `reconnect_auth_max` consecutive auth failures and must be restarted with fresh credentials. Monitor `auth_failures` stat.
- **Silent stall.** A transport that stays `OPEN` but stops delivering frames is caught only by the application heartbeat watchdog; size `heartbeat_timeout` conservatively.
- **Callback blocking.** `on_quote` runs on the socket task; a blocking callback stalls all markets. Enqueue, don't await, inside the callback.
- **Slug churn.** Markets expire/terminate; subscriptions to dead markets produce no updates and may error. The caller should refresh the slug list periodically (e.g. each trading cycle) via `subscribe()`/`unsubscribe()`.
- **Venue limits.** Max 100 markets per subscription; split into multiple subscriptions if the list grows.
- `POLYMARKET_ENABLED` is `false` by default in `config.py` (US crypto markets are preprod/not yet live as of 2026-11-13). The worker itself is read-only and does not depend on that flag; it only needs valid read credentials.

---

## 6. Files

- `polymarket_ws.py` — the auto-reconnecting async WebSocket worker
- `tests/test_polymarket_ws.py` — tests (mocked websockets + auth, no real network)
- This spec: `docs/superpowers/specs/2026-11-14-polymarket-us-websocket-spec.md`
