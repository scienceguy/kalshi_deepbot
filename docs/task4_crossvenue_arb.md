# Task 4 — Cross-Venue Arbitrage & Distributional Basis Mapping (Kalshi vs Polymarket US)

Status: research formulation. Grounded in the live codebase (`crossvenue.py`, `crossvenue_edge.py`, `crossvenue_history.py`, `poly_us_trader.py`, `model.py`, `binance_feed.py`, `config.py`).

---

## 1. Settlement mechanisms compared

| | Kalshi 15-minute crypto | Polymarket US hourly / 15-minute |
|---|---|---|
| **Settlement reference** | CF Benchmarks BRTI — a 60-second volume-weighted average index | Reference index / Binance 1-minute candle resolution |
| **Settlement value source** | `expiration_value` on `/historical/markets` (settled `result`) | `get_market_settlement(slug)` → `settlement` field (0.0 or 1.0) |
| **Contract form** | Threshold "greater" strike: `P(S_T > K)` at expiry, ticker `KXBTCD-...`/`KXBTC15M-...` | `assetPriceTerms` with `strike` / `priceToBeat` / `rangeLower`/`rangeUpper` + `windowEnd`; slugs like `cpc-btc-updown-15m-...` |
| **Time basis** | Ticker hour is US Eastern; expiry parsed from `KX...DDHHMM` | `windowEnd` ISO timestamp in market metadata |
| **Final-minute behaviour** | The 60-second BRTI average locks in second by second; remaining uncertainty scales ~`r^1.5` (`model.locked_share_probability`) | Market resolves once `windowEnd` passes; settlement read after close |
| **Auth for settlement data** | Public historical endpoints (no auth) for settled value; signed client for live orders | Public read endpoints; signed key/secret only for order placement |

Key structural difference, stated in `crossvenue.py` (lines 11–13):

> Settlement sources differ (Kalshi: CF Benchmarks BRTI 60s average; Polymarket: Binance 1m candle), so this is a **statistical anchor, not a riskless arb**. Call it a deviation study, not an arbitrage detector.

Consequence: even with identical strikes and expiries, the two venues settle on different underlying prints. A position held to expiry on one venue can lose money relative to the other purely through the settlement-basis gap — there is no guaranteed convergence. The engine must therefore treat any cross-venue deviation as a *signal with basis risk*, not as locked-in profit.

---

## 2. How systematic basis discrepancies arise between venues

### 2.1 Settlement-source mismatch (systematic, never diversifies away)
Kalshi settles on BRTI (a volume-weighted composite over 60 seconds). Polymarket US settles on a Binance 1-minute candle / reference index. The two prints can differ by real money. `binance_feed.price_sources` notes that on 2026-11-05 Binance quoted BTC at ~76,239 while Coinbase quoted ~76,040 — and `crossvenue.py:539–541` measures venue dispersion as a fraction of one 39-minute sigma:

> dispersion is N% of one 39-minute sigma ($sig_dollars) — this is the basis risk

On BTC, venue disagreement of ~$127 was measured, roughly 39% of one 39-minute sigma — larger than the edge being hunted. A single snapshot cannot distinguish genuine mispricing from reference error.

### 2.2 Stale-venue asymmetry
Per the `2026-11-05-polymarket-anchored-fair-value-design.md` spec (measurement results):

> Polymarket is the stale venue, not Kalshi. On hourly BTC/ETH ladders, Kalshi tracks the spot-implied fair value to within a few cents while Polymarket's ladder sat ~20c rich across every strike.

So Polymarket is systematically *rich* (overpriced) on ladders, not underpriced — which matters for directionality (section 3). The anchor that matters is spot-implied fair value; Polymarket is a cross-check.

### 2.3 Non-equivalent "equivalent" markets
The design doc cites a study of ~100,000 "equivalent" cross-venue events where ~44% were not equivalent: off-by-one strikes, different tie handling ("exactly at the strike" resolving YES on one venue, NO on the other), different settlement sources. A mismatch does not lose an edge — it loses the whole position. `crossvenue.py` sidesteps this by treating each venue's ladder as a sampled CDF and interpolating, so off-by-one strikes and different strike spacing cease to be a problem.

### 2.4 Liquidity / spread structure
`crossvenue.Ladder.sorted_rungs` drops rungs with spread wider than `MAX_SPREAD = 0.08` ("a wide book is not a price"). Resting maker fills occur when the favourite stops being the favourite (adverse selection). `grid_latency.py` / `POLYMARKET_DIAGNOSIS_REPORT.txt`: when venues are consistent (fair ≈ ask), the 7% taker fee makes `buy_yes_net` negative → correctly 0 tradable. There is no fee-clearing edge when venues agree.

### 2.5 Fee asymmetry
Kalshi charges taker fees only on immediately-matched orders; resting maker orders are free; settlement is not a trade. Polymarket US is fee-free for maker limit orders (`polymarket_us_client`). The cheapest cross-venue structure is maker entry, hold to settlement — leaving adverse selection as the only cost.

---

## 3. How `crossvenue_edge.py` captures "Polymarket underprices the high-certainty favorite relative to Kalshi's implied CDF"

### 3.1 What the engine currently does
`crossvenue_edge.evaluate_crossvenue_edge(market, spot_override)`:
1. Extracts `(symbol, strike, end_dt)` from the Polymarket market's `assetPriceTerms` (`extract_asset_and_strike`).
2. Gets live composite spot (Binance, 5s cache) and annualized vol (60s cache, fallback 0.60).
3. Computes a GBM reference probability `ref_prob = P(S_T >= K)` via `model.gbm_probability_yes_wins`.
4. Reads Polymarket `outcomePrices` (`long_p`, `short_p`).
5. `edge_long = (ref_prob - long_p) * 100` cents; positive = Polymarket underpricing YES vs spot-implied fair value.

`poly_us_trader.evaluate_opportunity` then gates on `best_edge_cents >= 0.5` and `0.92 <= best_price <= 0.99` (the high-certainty favorite band).

### 3.2 The gap: it's spot-implied, not Kalshi-CDF-implied
The current reference is a *spot-implied GBM probability*, not *Kalshi's own implied CDF*. To capture "Polymarket underprices relative to Kalshi's implied CDF" specifically, the engine should compare the Polymarket price at strike K against Kalshi's ladder-implied `P(S_T > K)` at the same strike — removing spot-model error and the settlement-reference disagreement from the comparison.

### 3.3 Formulation (CDF-relative edge)

Let:
- `C_K(s)` = Kalshi's sampled CDF, interpolated: `P_K(S_T > s)` at strike `s` (from `crossvenue.to_cdf(kalshi_ladder)`).
- `p_poly(K)` = Polymarket's YES price at strike K (from `outcomePrices[0]` / `marketSides` YES price).

Then the cross-venue edge in cents:

```
edge_cents(K) = ( C_K(K) - p_poly(K) ) * 100
```

- **Positive** → Polymarket's YES price is below Kalshi's implied probability → **Polymarket underprices the high-certainty favorite** → signal: buy YES on Polymarket (if venue enabled), or avoid selling Kalshi YES / favor the Kalshi NO leg.
- **Negative** → Polymarket rich vs Kalshi → no trade (matches the measured "Polymarket ~20c rich" regime).

The ladder-CDF approach (`crossvenue.compare`) also handles the off-by-one-strike problem: interpolate each ladder and compare distributions at any strike, so Kalshi's `KXBTC15M-...` strikes and Polymarket's `priceToBeat` need not match pairwise.

### 3.4 Implementation
I added `evaluate_crossvenue_edge_cdf(market, kalshi_cdf, spot_override=None)` to `crossvenue_edge.py` (below). It is pure (no network), takes the Kalshi CDF as a parameter so the caller supplies it from `crossvenue.fetch_kalshi_ladder`, and returns the same dict shape as `evaluate_crossvenue_edge` plus `edge_source = "kalshi_cdf"`. `poly_us_trader` can consume it by gating on `cdf_edge_cents` alongside the existing GBM edge.

The high-certainty favorite filter stays: `0.92 <= price <= 0.99` and `edge >= 0.5c`, matching `config.MIN_YES_PRICE_CENTS`/`MAX_YES_PRICE_CENTS` and `poly_us_trader`.

---

## 4. Recommendations for latency-resilient cross-venue signal execution

### 4.1 Fail-closed on stale reference data (the biggest live risk)
- `crossvenue_edge` caches spot for 5s and vol for 60s. Adopt the `config.GATES_STALE_OK_SECONDS` pattern (300s max staleness) and **fail closed** beyond it: a transient Binance blip shouldn't knock the bot out, but a long outage must block entries visibly rather than price against a stale print.
- Never price a cross-venue signal from a single venue's spot. Use `binance_feed.composite_price` (median across venues) — a mean lets one stale/dislocated venue drag the reference.

### 4.2 Estimate the basis from history before trading
`crossvenue_history.py` rebuilds each settled Kalshi ladder per minute, inverts it to the spot Kalshi was centred on, and compares to the Binance composite at the same minute. The distribution of `implied - spot` is the basis. **Do not trade a cross-venue deviation until this basis is estimated and subtracted** — otherwise reference error is mistaken for edge. `main()` prints a `--basis-dollars` gate value.

### 4.3 Execution venue: Kalshi only; Polymarket read-only anchor
- `config.POLYMARKET_ENABLED` defaults to `False`: crypto Up/Down 15m markets are in PREPROD on `api.polymarket.us`, and no crypto markets exist on the US retail API yet (config.py:131–136). Polymarket is read-only for discovery/anchor; execution goes through Kalshi (`kalshi_client`, signed RSA-PSS).
- `crossvenue_edge` must not place Polymarket orders. `poly_us_trader.preview_order` validates order syntax against Polymarket US before any paper fill; keep that discipline if US crypto markets launch.

### 4.4 Settlement-basis risk is unhedgeable intraday
Because Kalshi settles on BRTI 60s VWAP and Polymarket on a Binance 1m candle, do not treat a cross-venue deviation as locked profit:
- Hold-to-settlement only when the model gate (GBM or locked_share in the final minute) clears fees + buffer.
- In the final minute use `model.locked_share_probability` — the BRTI average locks in second by second, so late prices are more informative (`model.model_probability_from_market`).
- Treat Polymarket as a cross-check, not the anchor (per the design doc's measurement result #2).

### 4.5 Idempotency and order hygiene
- Kalshi `_already_ordered()` blocks a second resting entry per ticker (trader.py:869) — prevents the coid-repost duplication bug (audit finding #61).
- `clear_resting_orders()` before closing; Kalshi blocks a close while any order rests.
- Circuit breakers stay on: drawdown halt, per-market daily loss limit (`risk.record_market_loss`), consecutive-loss cooldown — these are not loosened for cross-venue signals.

### 4.6 Settlement P&L routing
- n8n `superbot-monitor` is ack-only by design: running `trader.py` from a webhook would start a second cycle that can place orders once `DRY_RUN=false`. Settlement P&L webhooks return HTTP 200 and ack; they never trigger a new cycle.
- n8n REST API returns 401 (upstream bug #31011); webhooks + direct DB injection are the working path.

---

## Files
- `crossvenue_edge.py` — added `evaluate_crossvenue_edge_cdf` (CDF-relative edge; see diff below).
- `tests/test_crossvenue_edge.py` — added tests for the CDF-relative path.
