# Kalshi SuperBot — Ponytail Audit Report

**Date:** 2026-11-13  
**Auditor:** Senior Quantitative Systems Architect & Latency Engineer  
**Scope:** Cross-Venue Polymarket US & Kalshi Execution, Subsecond WebSocket Spot Momentum, Compounding Micro-Balances ($1.67 → $100k)  
**Repo:** `/data/data/com.termux/files/home/kalshi_deepbot`  
**Mode:** Read-only — no code changes made

---

## What This Repo Does & Load Assumed

**System:** A single-operator live-trading bot for Kalshi 15-minute binary crypto markets. Places favourite-maker limit orders (YES/NO at 75–99c), evaluates signals via an LLM with 7-layer fallback, writes signals/orders/bot-state to Supabase, routes settlement P&L through n8n webhooks.

**Core Loop (trader.py):**
- Cycle interval: 15s (config.LOOP_INTERVAL_SECONDS)
- Fetches balance, positions, 9 crypto 15M series via parallel ThreadPoolExecutor
- Runs signal engine (signals.py) → LLM pre-filter (llm_eval.py) → Pre-entry gates (gates.py) → ML fair-value override → Order placement
- Manages exits (take-profit/stop-loss), settlements (idempotent via local SQLite pnl_ledger), risk limits (cash floor, exposure ceiling, drawdown halt, per-market daily loss)
- Dry-run by default; `--live` requires `KALSHI_CONFIRM_LIVE=true`

**Cross-Venue Infrastructure:**
- `crossvenue.py` / `crossvenue_collect.py` / `crossvenue_history.py`: Read-only deviation studies between Kalshi (CF BRTI settlement) and Polymarket (Chainlink/Binance settlement)
- `polymarket_us_client.py`: Authenticated client for api.polymarket.us (US retail API) — crypto Up/Down 15m markets are **preprod only**, not production
- `scripts/crossvenue_live.py`: Standalone live deviation monitor

**Spot & Momentum Feeds (binance_feed.py):**
- REST: data-api.binance.vision (geo-accessible), OKX, Coinbase, Kraken fallbacks
- WebSocket: Binance combined `!miniTicker@arr`, Coinbase Advanced Trade `ticker`, RedStone (optional) — subsecond cache `_ws_prices`
- Volatility: 1-minute log-return sigma (Binance klines primary, OKX fallback)

**Compounding Architecture:**
- Zero maker fee on Kalshi crypto 15m (config.AGGRESSIVE_FILL=False for maker)
- Survival-first Kelly sizing: 1 contract per $10 cash, max 3 contracts, max 2 concurrent positions (BTC/ETH/SOL/XRP/DOGE move together)
- Paper mode compounds on its own bankroll (paper_state.json) — isolated from live
- Local SQLite first, Supabase async sync (30s interval)

**Assumed Load:**
- 9 series × ~5 markets each = 45 markets/cycle
- ~10 REST calls/cycle (balance, positions, 9 series markets, orderbooks for filtered)
- WebSocket streams: 3 background threads (Binance, Coinbase, RedStone) updating `_ws_prices` dict
- Cycle target: <30s (CYCLE_TIMEOUT_SECONDS)

---

## Numbered Findings

### Must Fix

#### 1. **Cross-Venue Execution Path Does Not Exist — `polymarket_us_client.py` only reads preprod, no production crypto markets** `polymarket_us_client.py:477-487`
- **What this is:** `_place_poly_order()` in trader.py attempts to place a concurrent fee-free maker order on Polymarket US when a Kalshi signal fires. The function logs "no production crypto Up/Down 15m market on api.polymarket.us (preprod only)" and returns without placing anything.
- **Problem:** The entire cross-venue execution layer is a no-op. The bot places Kalshi orders but never hedges on Polymarket, eliminating the stated "maker on Kalshi first, hedge second" strategy. The code paths for slug resolution (`GET /v1/markets?categories=crypto` matching `assetPriceTerms.asset.symbol`) are commented as future work.
- **Fix:** Either (a) wire the international Polymarket CLOB (`clob.polymarket.com`) with L2 credentials (passphrase + L1 wallet key, see `scripts/derive_poly_creds.py`), or (b) remove the dead code and document that cross-venue is not yet implemented. The US retail API will not have crypto 15m markets until Polymarket launches them.
- **If we skip it:** The bot runs single-venue only. The AGENTS.md "Cross-Venue Latency & Asymmetry" section describes a strategy that does not exist in code.

#### 2. **WebSocket Spot Cache Has No Backpressure & Staleness Is Silent** `binance_feed.py:73-100`
- **What this is:** `_ws_prices` dict updated by 3 daemon threads (Binance, Coinbase, RedStone). `spot_price()` prefers WS price if `< WS_SPOT_STALE_SECONDS` (default 5s), else falls back to REST composite.
- **Problem:** 
  - No circuit breaker: if all 3 WS streams die (common on Android/Termux due to network changes), `spot_price()` silently falls back to REST which is 1-2s stale. The gates then score with stale spot, producing wrong `cushion_z` and `q`.
  - `WS_SPOT_STALE_SECONDS=5.0` is too long for subsecond momentum — a 5s stale price in a 15-minute binary is an eternity near expiry.
  - No metric emits when WS goes stale; operator cannot tell from logs whether momentum signal is fresh.
- **Fix:** 
  - Reduce `WS_SPOT_STALE_SECONDS` to 1.0 (env configurable)
  - Add `ws_health` metric to gate logs: `{"ws_fresh": bool, "ws_age_seconds": float, "fallback_used": bool}`
  - In `gates.py:spot_for()`, log a WARNING when falling back to REST due to WS staleness
  - Consider a `require_ws=True` mode for the final 2 minutes where momentum is decisive

#### 3. **Gates Shadow Mode Default Masks Real Block Rate — No Visibility Into What Would Be Blocked** `config.py:179, gates.py:479-484`
- **What this is:** `GATES_SHADOW_MODE = True` by default. When a gate would block, it returns `allowed=True` with `decision="would_block"` and logs the reason. The order proceeds.
- **Problem:** In production, the gates **never actually block**. The operator sees "GATE ... would_block" in logs but the trade executes anyway. There is no dashboard, alert, or summary of "would_block rate vs actual fill rate". The gates are effectively decorative.
- **Fix:** 
  - Default `GATES_SHADOW_MODE=False` (opt-in shadow for calibration only)
  - Add a per-cycle counter: `state.gates_would_block_count` incremented in `evaluate_entry()` when `would_block` and `GATES_SHADOW_MODE=True`
  - Fire n8n alert when `would_block_count / evaluated > 0.5` (gates blocking majority)
  - Gate log must include `shadow_mode: true/false` so post-hoc analysis can separate

#### 4. **Measured-Q Floor Is a Rubber Stamp — Opt-In Only, But When Enabled It Bypasses Model Entirely** `config.py:221-224, gates.py:438-453`
- **What this is:** `GATES_MEASURED_Q=0.9928` (empirical win rate of 95-99c favourite). When `GATES_MEASURED_FLOOR_ENABLED=True`, Gate 3 floors `q = max(q, 0.9928)` for YES-equivalent prices in 95-99c band.
- **Problem:** The config comment admits: "That is not a gate, it is a rubber stamp... the only reason the 95-99c band is reachable at all." The Brownian model has no drift, so `q ≈ p` inside the band → Gate 2 EV check always fails. The floor replaces the model probability instead of checking it, guaranteeing passage.
- **Fix:** 
  - Remove `GATES_MEASURED_FLOOR_ENABLED` entirely — it is a backdoor that invalidates the gate architecture
  - If the GBM model cannot clear breakeven in the 95-99c band, the band should be blocked by Gate 2 (which is the correct behavior)
  - The fix is to calibrate `MODEL_VOLATILITY` dynamically (see Finding 5) so the model itself clears breakeven, not to paper over it

#### 5. **Fixed GBM Volatility (0.80) Produces -0.01c/trade; Realized Vol Produces +1.03c — But Dynamic Vol Is Not Validated Against Live Outcomes** `config.py:328-335, model.py:31-70`
- **What this is:** `MODEL_VOLATILITY=0.80` (80% annualized) is the fallback. `MODEL_VOL_DYNAMIC=True` uses `binance_feed.composite_sigma()` (realized per-minute vol, annualized) clamped to [0.20, 5.00].
- **Problem:** 
  - Backtest on HF data (14 days): fixed 0.80 → -0.01c/trade; realized vol → +1.03c/trade (n=727, win 98.6%)
  - **However:** the realized vol is computed from 1-minute close-to-close returns, which understates intra-minute moves. Near expiry, the lognormal is a poor description of a single print (binance_feed.py:612-614 comment).
  - No live validation: the bot has never tracked "model_prob vs actual settlement" to know if dynamic vol is actually calibrated. The `markout_log.jsonl` (trader.py:1629-1661) records fill-vs-settlement but not model_prob.
- **Fix:** 
  - Add `model_prob` to `markout_log.jsonl` entries so every fill carries the model's prediction
  - Implement a rolling calibration metric: `Brier score` and `AUC` over last N settlements, logged per cycle
  - If Brier > 0.18 or AUC < 0.75, auto-disable `MODEL_VOL_DYNAMIC` and alert (model is miscalibrated)
  - The fixed 0.80 should be a last-resort fallback, not the default

#### 6. **NO Side Entry Band Uses YES Measured-Q — Category Error** `config.py:299-303, gates.py:438-453`
- **What this is:** `MIN_NO_ENTRY_CENTS=75`, `MAX_NO_ENTRY_CENTS=99`. Gate 3 floors NO fills at 97c with the YES measured-Q (99.28%).
- **Problem:** A NO fill at 97c means YES is at 3c — the contract settles NO only ~3% of the time. Applying the YES win rate (99.28%) to a NO leg is a category error. The gate will pass NO fills that have a true win probability of ~3%.
- **Fix:** 
  - NO side must have its own measured-Q (empirical NO win rate in the band) or be blocked by Gate 2
  - Add `GATES_MEASURED_Q_NO` config (measured separately) and use `yes_equiv = 100 - entry_price_cents` for NO side floor check
  - Or simpler: disable NO favourite entry until measured; `MIN_NO_ENTRY_CENTS=99` effectively disables it

#### 7. **Settlement Queue Replay Can Double-Count PnL If n8n Webhook Succeeds But Local Ledger Write Fails** `trader.py:939-985, local_db.py:415-450`
- **What this is:** `_replay_settlement_queue()` applies PnL to local pnl_ledger (idempotency key `settle:<event_id>:<order_id>`), then fires n8n webhook. If local write fails (`applied=None`), PnL is NOT credited and entry is re-queued.
- **Problem:** The order of operations is:
  1. Apply to local ledger → `applied=True` (PnL credited)
  2. Write to Supabase
  3. Fire n8n webhook
  4. Remove from queue
  
  If step 1 succeeds but step 3 fails, the entry is re-queued. On replay, step 1 returns `applied=False` (duplicate) so PnL is not re-credited — **correct**.
  
  **BUT**: if step 1 fails (`applied=None` — DB error), the entry stays in queue. On next replay, step 1 might succeed (`applied=True`), crediting PnL. However, if step 1 succeeded on the FIRST attempt but the exception was raised AFTER the insert (e.g., connection dropped after commit), the ledger has the row but `applied=None` was returned. Replay would then insert a SECOND row with same idempotency key → `ON CONFLICT DO NOTHING` prevents duplicate, but `changes()` returns 0 → `applied=False` → PnL not credited. **This is actually correct.**
  
  **Real issue:** The n8n webhook fires regardless of Supabase write success (step 2). If Supabase fails but n8n succeeds, the downstream workflow sees the settlement but Supabase doesn't. Reconciliation drifts.
- **Fix:** Make n8n webhook conditional on BOTH local ledger AND Supabase succeeding. Add `supabase_synced` flag to queue payload.

#### 8. **Bid History Persistence Is Single-Process — Multi-Process Will Diverge** `signals.py:145-163, 196-231`
- **What this is:** `_bid_hist` dict persisted to `bid_hist_{dry,live}.json` every 5s with file locking.
- **Problem:** The comment at line 159-163 admits: "If the bot is ever run in multiple processes, this state will diverge between processes and must be moved into a shared store." The AGENTS.md mentions concurrent agents editing the same checkout. If two trader processes run (e.g., dry-run + live, or two dry-runs), they share the same JSON file but the in-memory `_bid_hist` is not synced between processes except via the 5s write/TTL reload. A process can overwrite another's updates.
- **Fix:** 
  - Move bid history to local SQLite (already has WAL mode, handles concurrent readers/writers)
  - Or use Redis if available
  - At minimum, add `fcntl` lock on READ (not just write) in `_load_bid_hist()` so concurrent processes don't read torn state

---

### Should Fix

#### 9. **Cross-Venue Latency Measurement Is Read-Only — No Execution Latency Tracking** `crossvenue.py, crossvenue_collect.py`
- **What this is:** `crossvenue_collect.py` snapshots Kalshi/Polymarket ladders against spot every 60s. `crossvenue_history.py` reconstructs historical basis from settled markets.
- **Problem:** These measure **price deviation**, not **execution latency**. The AGENTS.md claims "Kalshi AWS us-east-2 orderbook updates carry ~9.4ms median latency; Polymarket CLOB has ~4.0ms median but 43ms–3s p99 tails." This data is not collected anywhere in the codebase. There is no measurement of:
  - Order ack latency (Kalshi /portfolio/events/orders POST → response)
  - WebSocket orderbook update latency (receive → process)
  - Cross-venue hedge leg latency (Kalshi fill → Polymarket order placed)
- **Fix:** Add latency instrumentation:
  - In `kalshi_client.py:_request()`, record `latency_ms` for each endpoint
  - In `trader.py:_place_order()`, measure `order_latency_ms = time.time() - start`
  - In `_place_poly_order()` (when implemented), measure cross-venue hedge latency
  - Log percentiles (p50, p95, p99) per cycle to a `latency_log.jsonl`

#### 10. **LLM Evaluation Consensus Uses Simple Average — No Weighting By Layer Reliability** `llm_eval.py:537-554`
- **What this is:** `evaluate_trade()` calls up to `LLM_CONSENSUS_QUORUM` layers (default 2), averages `probability_of_net_profit` and `confidence_hold_to_expiry`.
- **Problem:** Vertex AI (Gemini 3.6 Flash), NVIDIA Bolt, NVIDIA Lightning have vastly different latencies, costs, and empirical accuracy. A simple average treats a 200ms NVIDIA Lightning response the same as a 5s Vertex AI response. No layer has a tracked accuracy score.
- **Fix:** 
  - Track per-layer `accuracy = (prob > 0.5) == (trade_won)` over rolling window
  - Weight consensus by `accuracy * (1 / latency)` or similar
  - Skip layers with accuracy < 0.55 (worse than random)

#### 11. **Risk Manager Drawdown Halt Uses Cost-Basis Exposure By Default** `risk.py:54-65, trader.py:1886-1888`
- **What this is:** `update_balance(cash, exposure, mark_to_market_exposure=None)` uses `mark_to_market_exposure` if provided, else `exposure` (cost basis).
- **Problem:** In `trader.py:1848-1852`, dry-run uses `paper_mtm_exposure` (mark-to-market) but live uses `_mark_to_market_exposure(positions, markets)` which falls back to cost basis when orderbook mid is unavailable. During a drawdown, cost-basis exposure understates true loss → halt fires late or not at all.
- **Fix:** 
  - Make `mark_to_market_exposure` mandatory (fail closed if None)
  - In `_mark_to_market_exposure()`, if any position lacks orderbook mid, log WARNING and use a conservative estimate (e.g., cost basis × 1.1) rather than silently using stale cost basis

#### 12. **Paper Mode Cash Calculation Ignores Mark-to-Market Exposure By Default** `paper_mode.py:180-191`
- **What this is:** `cash(open_risk, mark_to_market_exposure=None)` uses `mark_to_market_exposure` if provided, else `open_risk` (cost basis).
- **Problem:** In `trader.py:1389-1409`, paper MTM exposure is computed but only passed to `paper_mode.cash()` at line 1409. The `cash()` function at line 1910 (for logging) does NOT pass it. The logged paper cash uses cost-basis exposure, overstating available cash during drawdowns.
- **Fix:** Always pass `mark_to_market_exposure` to `paper_mode.cash()` — change signature to require it or default to the computed MTM value.

#### 13. **Order Fill Confirmation in Live Mode Has Race Window** `trader.py:2218-2232`
- **What this is:** After placing an order, live mode calls `client.get_orders(status="resting")` to confirm fill. If the order filled and was removed from resting between placement and this call, `confirmed_fill_count` stays `None` and the order is kept resting.
- **Problem:** A fill that occurs in the ~100ms between `place_order` and `get_orders` is missed. The position is not recorded locally, so settlement P&L is never booked (unless adoption logic at line 1495-1519 catches it next cycle).
- **Fix:** Use the order response `fill_count_fp` directly (already available at line 2160) instead of re-fetching. The API returns fill count on placement response for IOC/taker orders. Only re-fetch for maker orders that rest.

#### 14. **No Per-Market Correlation Limit — BTC/ETH/SOL/XRP/DOGE Move Together** `config.py:366-368`
- **What this is:** `MAX_OPEN_POSITIONS=2` with comment "BTC/ETH/SOL/XRP/DOGE move together, so concurrent 15M positions are one correlated crypto bet."
- **Problem:** The limit is on **count**, not **correlation**. The bot could hold KXBTC15M and KXETH15M (correlation ~0.85) — one correlated bet. Or KXBTC15M and KXDOGE15M (correlation ~0.75). No check prevents holding 2 highly correlated positions.
- **Fix:** Track rolling 60-minute correlation matrix from `binance_feed.composite_klines_range()`. Block new entry if `abs(correlation) > 0.7` with any open position. Or simpler: `MAX_OPEN_POSITIONS=1` for crypto (enforce single bet).

#### 15. **Local DB Sync to Supabase Has No Dead Letter Queue — Failed Rows Block Forever** `local_db.py:586-659`
- **What this is:** `_sync_table()` iterates unsynced rows, tries to push to Supabase. On failure, logs warning and continues — the row stays `synced=0`.
- **Problem:** A schema mismatch (e.g., Supabase missing `fill_price_cents` column) causes permanent failure. The row is retried every 30s forever, logging the same error. No alert, no dead-letter, no max-retry.
- **Fix:** 
  - Add `sync_attempts` column to each table
  - After 5 failed attempts, move row to `*_dead_letter` table and fire n8n alert
  - Provide `scripts/fix_dead_letter.py` to inspect/replay

---

### Nice to Have

#### 16. **WebSocket Reconnection Lacks Exponential Backoff Cap** `binance_feed.py:102-131, 149-165, 186-220`
- **What this is:** WS loops reconnect on any exception with fixed 2s sleep.
- **Problem:** During extended outage (e.g., Binance maintenance), 3 threads hammer reconnect every 2s. No jitter, no cap.
- **Fix:** Add `backoff = min(backoff * 2, 60)` with jitter `±25%`, reset on success.

#### 17. **Gate Metrics Log Is JSONL But No Schema Version** `gates.py:297-299, config.py:259-262`
- **What this is:** `gates_log.jsonl` appended per decision change.
- **Problem:** No `schema_version` field. If fields are added/removed, downstream analysis breaks silently.
- **Fix:** Add `"schema_version": 1` to every record.

#### 18. **Crossvenue History Basis Calculation Uses Implied Spot — Circular** `crossvenue_history.py:270`
- **What this is:** `implied = ls.implied_spot(rungs, sigma, mins_left)` then `basis = implied - spot`.
- **Problem:** `ladder_strategy.implied_spot()` inverts the Kalshi ladder to find the spot price it implies. Subtracting actual spot from this gives the basis. But `implied_spot` uses the same `sigma` from Binance that the gates use — if the model is wrong, the basis is wrong. It's not an independent measurement.
- **Fix:** Also compute basis as `spot - (strike_at_50c_from_ladder)` for a model-free anchor.

#### 19. **No Health Check Endpoint for External Monitoring** `trader.py, n8n_client.py`
- **What this is:** n8n workflows call `superbot-trade` webhook with `action: "health_check"`. The trader has no HTTP server.
- **Problem:** Cannot monitor bot liveness from outside (e.g., uptime robot, k8s liveness probe). The n8n health check only tests the webhook path, not the trader process.
- **Fix:** Add a tiny HTTP server (aiohttp/FastAPI) on port 8080 with `/healthz` returning `{"status": "ok", "cycles": N, "last_cycle_ts": ..., "risk_halted": bool}`.

#### 20. **ML Fair-Value Model Is Loaded But Never Validated** `trader.py:111-122, 1255-1311, ml_model.py`
- **What this is:** `--ml-model` loads a `MLFairValue` torch model. `_ml_filter_signals()` runs it via `ladder_strategy.plan_orders()` and blocks if ML disagrees with venue on direction or fair value below entry bar.
- **Problem:** No live tracking of ML model accuracy. The model could be garbage and the bot would silently filter good trades or pass bad ones. No Brier score, no AUC, no confusion matrix.
- **Fix:** Log `ml_prob`, `ml_side`, `venue_side`, `actual_outcome` for every ML-evaluated signal. Compute rolling metrics.

---

## Verdict

**Overall:** The codebase is production-grade for single-venue Kalshi trading with solid risk management, idempotent settlement, and local-first persistence. The three audit focus areas have critical gaps:

| Focus Area | Status | Blocking Issue |
|------------|--------|----------------|
| Cross-Venue Execution | **Not Implemented** | Polymarket US has no crypto 15m markets; CLOB path not wired |
| Subsecond Spot Momentum | **Partial** | WS cache has no health metric, 5s staleness threshold too long |
| Compounding $1.67→$100k | **Theoretically Sound** | But depends on GBM vol calibration (Finding 5) and gate enforcement (Finding 3) |

**Lean (-N lines):**
- Remove dead cross-venue code in `trader.py:_place_poly_order()` (~60 lines) and `polymarket_us_client.py` place_order path (~50 lines) until CLOB is wired
- Remove `GATES_MEASURED_FLOOR_ENABLED` and associated logic (~20 lines) — it's a backdoor
- Consolidate `paper_mode.cash()` and `risk.py` MTM exposure handling (~30 lines duplicated)

**Not Checked:**
- n8n workflow definitions (injected via `inject_n8n_workflows.py` into n8n SQLite)
- Supabase schema (RLS policies, indexes)
- `scripts/derive_poly_creds.py` — L2 credential derivation for international CLOB
- `ml_model.py` — torch model architecture and training pipeline
- `ladder_strategy.py` — order planning logic used by ML filter
- Android/Termux specific issues (file locking, `/tmp` restrictions, process lifecycle)

---

## Immediate Action Items (Priority Order)

1. **Set `GATES_SHADOW_MODE=False`** — gates must actually block to provide value
2. **Reduce `WS_SPOT_STALE_SECONDS=1.0`** — subsecond momentum requires fresh spot
3. **Add model calibration logging** — `markout_log.jsonl` must include `model_prob`
4. **Disable NO favourite entry** — `MIN_NO_ENTRY_CENTS=99` until measured
5. **Wire Polymarket CLOB or remove cross-venue dead code** — clarify strategy
6. **Add correlation limit** — `MAX_OPEN_POSITIONS=1` for crypto or correlation check
7. **Add dead-letter queue for Supabase sync** — prevent silent data loss