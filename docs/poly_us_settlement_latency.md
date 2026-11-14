# Polymarket US — Settlement Speed & Resolution Latency Analysis

**Status:** research report (from official `docs.polymarket.us` + live 404 probe of `gateway.polymarket.us`).
**Target:** `poly_us_trader.py` settlement polling loop (`check_settlements`, `polymarket_us_client.get_market_settlement`).

---

## 1. Settlement lifecycle (official API spec)

Both endpoints live on `https://gateway.polymarket.us` and are **public (no auth)**.

### `GET /v1/market/slug/{slug}` — Get Market By Slug
Returns `{"market": {...}}`. Relevant fields:
- `status` (`MarketStatus` enum): `OPEN` / `CLOSED` / `RESOLVING` / `RESOLVED` / `HALTED` / `UNSPECIFIED`.
  - `RESOLVING`: "Past expiry, final settlement not yet published — payouts are pending."
  - `RESOLVED`: "Final settlement published; the outcome will not change."
- `endDate`, `gameStartTime`: legacy timestamps.
- `assetPriceTerms.windowEnd` (crypto price markets): **"End of the measurement window and scheduled settlement instant. Use this rather than endDate for the settlement time."**
- `assetPriceTerms.settlementPrice`: null until resolved.

### `GET /v1/markets/{slug}/settlement` — Get Market Settlement
Returns `{"slug": "<slug>", "settlement": <decimal>}` where settlement is `1.00` (YES wins) or `0.00` (NO wins).

### Settlement semantics (docs)
- Winning contracts settle at **$1.00**, losing at **$0.00**.
- Portfolio updates automatically when settlement completes.
- All settlements are final (per the Exchange Rulebook).
- Some markets carry alternative settlement terms (per market rules).

### Status-code matrix

| Market state | `GET /v1/market/slug/{slug}` | `GET /v1/markets/{slug}/settlement` |
|---|---|---|
| Open / trading | 200, `status=OPEN` | **404** "not settled" |
| Past expiry, not yet settled | 200, `status=RESOLVING` | **404** "not settled" |
| Resolved | 200, `status=RESOLVED` | **200** `{slug, settlement: 1.00\|0.00}` |
| Unknown slug | 404 "not found" | 404 "not found / not settled" |
| Transient server error | 500 | 500 |
| Rate limited | 429 | 429 |

Live-verified: an unknown slug returns `404 {"code":5,"message":"The server was unable to process your request.","details":[]}` on **both** endpoints (gRPC `code: 5` = NOT_FOUND). Docs confirm 404 covers both "not found" and "not settled" on the settlement endpoint — the client cannot distinguish them from status alone today.

---

## 2. Time delta: scheduled expiration → settlement publication

**Polymarket does not publish a fixed delta.** The Market Resolution doc states resolution occurs "after the event outcome is publicly confirmed" and "timing varies by event." No SLA between `windowEnd` (e.g. `14:00:00 UTC`) and first `settlement=1.00/0.00` publication is documented.

### Recommended measurement (run once per market family)
1. Read `assetPriceTerms.windowEnd` (scheduled settlement instant) from `GET /v1/market/slug/{slug}`.
2. Starting at `windowEnd`, poll `GET /v1/markets/{slug}/settlement` every N seconds.
3. Record `t_first_200` = first timestamp returning `200` with `settlement ∈ {0.00, 1.00}`.
4. `delta = t_first_200 − windowEnd`. Repeat across windows; report median / p90 / max.

Placeholder pending measurement:
- `delta_median = ? s`
- `delta_p90 = ? s`
- `delta_max observed = ? s`

---

## 3. Problems in the current polling loop (`poly_us_trader.py`)

`cycle()` runs every 15 s; `check_settlements()` iterates open positions and, for each:
1. Computes `is_closed` from `endDate` / `gameStartTime` vs now (legacy timestamp, **not** `windowEnd`).
2. Calls `get_market_settlement(slug)` → `/v1/markets/{slug}/settlement`.
3. On 200: books payout, adds cash, removes position, writes ledger.
4. On `None` (covers 404-not-settled, 404-not-found, 500, network error): keeps position, no differentiation.

Issues:
- **Wrong expiry signal.** Uses `endDate`/`gameStartTime` instead of `assetPriceTerms.windowEnd` (docs: use `windowEnd`).
- **No status distinction.** Treats "not yet settled" (keep polling), "slug dead" (stop polling), "500 transient" (retry), and "network error" (retry) identically.
- **Wasteful polling / capital lock-in.** Polls settlement for every position every cycle even before expiry; capital stays locked from `windowEnd` until the first observed 200.
- **No idempotency guard.** Crash between cash credit and `_save_state` could double-book a settlement on restart.
- **No rate-limit handling.** Retail API limit is 25 req/s shared across endpoints; 429 → exponential backoff per docs.

---

## 4. Recommendations (smallest complete set)

### R1 — Poll settlement only after expiry, keyed on `windowEnd` / `RESOLVING`
In `check_settlements`, fetch `GET /v1/market/slug/{slug}` (cached — see R4) to obtain `status` and `assetPriceTerms.windowEnd`. Only call the settlement endpoint when `status == RESOLVING` or `now >= windowEnd`. This starts polling exactly at scheduled expiration and eliminates settlement calls for live markets, minimizing capital lock-in to the true settlement latency.

### R2 — Surface HTTP status from `get_market_settlement`; branch per status
Return a rich result (e.g. `{"settlement": float|None, "http_status": int, "kind": "settled|not_settled|not_found|server_error|network_error"}`). Caller:
- `200 settled` → book payout, remove position, recycle cash (already implemented).
- `404 not_settled` → keep position, continue polling (normal path).
- `404 not_found` / unknown slug → stop polling, mark position stale (don't lock cash forever on a dead slug).
- `500` / network error → retry with exponential backoff, keep position.

### R3 — Adaptive frequency after expiry
Before `windowEnd`: zero settlement calls. After `windowEnd`: poll every cycle (15 s) or faster (e.g. 5 s) until first `200`, then stop. This minimizes both lock-in (poll hard right after expiration) and API cost (no polling before expiration).

### R4 — Cache market metadata per slug
Cache `status` / `windowEnd` with a short TTL (30–60 s) rather than refetching the market endpoint every cycle. Respects the 25 req/s shared retail limit and Polymarket's "cache reference data" guidance.

### R5 — Idempotent settlement booking
Add an idempotency key per `(slug, side, qty)` settlement event; skip if already applied (mirrors the existing `pnl_ledger` idempotency pattern). Guards against double-credit on restart/crash between cash update and `_save_state`.

### R6 — Prefer WebSocket for live (non-paper) deployment
Polymarket's rate-limit doc: "Use WebSocket instead of polling"; position/balance resolution events stream via `/v1/ws/private`. Keep the REST settlement poll as fallback/reconciliation for the paper trader.

---

## 5. Expected impact

| Metric | Before | After |
|---|---|---|
| Settlement calls per cycle | N (one per open position, regardless of expiry) | ≤ N, only for expired/unsettled positions |
| Capital lock-in | from `endDate`-based close to first observed 200 | from `windowEnd` to first observed 200 (≈ settlement latency) |
| Dead-slug cash lock | possible (indefinite) | eliminated (stop polling on not_found) |
| Double-booking risk | yes (crash between credit and save) | no (idempotency key) |
| 429 exposure | unhandled | backoff per docs |

---

## 6. Open item

Measure the expiration→settlement delta for the 15-min crypto up/down family using §2 and plug the observed p90 into poll-frequency / backoff tuning. Do not loosen bands or gates based on this measurement alone — see the 2026-11-12 reality-tax findings (edge is thin and gate-dependent).
