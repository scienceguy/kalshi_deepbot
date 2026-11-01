# Trading Bot Troubleshooting Research Report

**Project:** kalshi_superbot  
**Target file:** `/data/data/com.termux/files/home/kalshi_superbot/TROUBLESHOOTING_RESEARCH.md`  
**Prepared:** Sun Nov 01 2026  
**Scope:** Automated trading bot crash recovery, position reconciliation, PnL integrity, rate limiting, circuit breakers, dry-run vs live safety, n8n webhook reliability, and logging/observability.

> **Ponytail note:** This is a research reference document. It contains no code changes to the bot itself. Implementation should be done incrementally, one section at a time, with tests for each non-trivial addition.

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Topic 1 — Crash Recovery & API Failure Reconnection](#topic-1-crash-recovery)
3. [Topic 2 — Position Reconciliation on Restart](#topic-2-position-reconciliation)
4. [Topic 3 — PnL Double-Counting Prevention](#topic-3-pnl-double-counting)
5. [Topic 4 — REST API Rate Limit Handling](#topic-4-rate-limiting)
6. [Topic 5 — Circuit Breaker Patterns](#topic-5-circuit-breakers)
7. [Topic 6 — Dry-Run vs Live Trading Safety](#topic-6-dry-run-vs-live)
8. [Topic 7 — n8n Webhook Reliability](#topic-7-n8n-webhooks)
9. [Topic 8 — Trading Bot Logging & Observability](#topic-8-logging-observability)
10. [Cross-Cutting Recommendations](#cross-cutting)
11. [Startup / Recovery Checklist](#startup-checklist)
12. [Source Index](#source-index)

---

## Executive Summary

Automated trading bots fail in predictable ways: API timeouts leave order state ambiguous, restarts collide with open positions, retries duplicate fills, rate limits cascade into outages, and silent state drift corrupts PnL. The research converges on a small set of defensive patterns:

- **Trust the exchange as source of truth** for positions and orders; treat local state as a cache. Reconcile on every startup and periodically during operation.
- **Idempotency keys (client order IDs)** on every order make retries and restarts safe — without them you cannot ask "did this order go through?"
- **Three failure modes** on restart: orphan orders (exchange has, local doesn't), ghost orders (local has, exchange doesn't), stale fills (filled on exchange, not processed locally).
- **If reconciliation fails, do not trade.** Operating with unknown state is how accounts get blown up.
- **Circuit breakers** halt new orders when error rates breach a threshold, then resume via a half-open probe state.
- **Dry-run and live must be strictly separated** (different API keys, different accounts), with tested kill switches and pre-trade risk checks before any order hits the wire.
- **n8n webhooks** need idempotency gates on delivery IDs, bounded retries, and a dead-letter queue for exhausted failures.
- **Structured JSON logging with correlation IDs** and alerting on error rates, not just exceptions.

The sections below detail each topic with key insights, source URLs, and practical implementation advice.

---

## Topic 1 — Crash Recovery & API Failure Reconnection

### Key Insight / Recommendation

A crash leaves the last order in an ambiguous state: the bot sent the order, the exchange may or may not have received it, and the acknowledgment was never recorded. The bot must not guess. The fix is a **reconciliation loop** that runs on every startup (and periodically) before trading is re-enabled, comparing local state to exchange reality and correcting three failure modes:

1. **Orphan orders** — exchange has the order, local state does not (risk: double orders on restart).
2. **Ghost orders** — local state records an order that never reached the exchange (risk: wrong position sizing).
3. **Stale fills** — order filled on exchange but the fill notification was never processed (risk: completely wrong position tracking).

**Exchange always wins.** Local state is a cache. If reconciliation itself fails (exchange down, rate limited), the bot must refuse to start, not trade blind.

### Source URLs

- https://matrixtrak.com/blog/crash-recovery-reconciliation-loops-trading-bots
- https://matrixtrak.com/errors/crash-recovery-double-orders-on-startup
- https://newhope.uk.com/trading-bot-restart-recovery-checklist
- https://vantixs.com/blog/handling-exchange-outages-crypto-bots

### Practical Implementation Advice

**Idempotency keys are the prerequisite.** Every order must carry a deterministic client order ID (e.g., `strategy-symbol-timestamp-random`) stored *before* the order is placed. Most exchanges support this: Binance `newClientOrderId`, Bybit `orderLinkId`, OKX `clOrdId`, Kraken `userref`. Without these you cannot safely ask "did this specific order go through?"

**Startup sequence (order matters):**
1. Load local state from durable storage.
2. Create exchange connection with trading disabled.
3. Run reconciliation loop (orphans → ghosts → stale fills → position).
4. Verify corrected position is within risk limits.
5. Enable trading.
6. Connect WebSocket live data.

**Reconciliation loop phases:**
- Phase 1: `fetchOpenOrders()` on exchange; any order not in local `orderIds` is an orphan — adopt it or cancel depending on strategy horizon.
- Phase 2: For each local open order, `fetchOrder(clientOrderId)`; if not found on exchange, it's a ghost — remove from local state.
- Phase 3: For each local order not marked filled, check remote status; if `filled`, backfill the fill into local state.
- Phase 4: `fetchPosition(symbol)`; if size/entryPrice drift beyond threshold, correct local state and log an audit event.

**Error handling in reconciliation:** Catch `RATE_LIMITED` → wait and retry; catch `EXCHANGE_DOWN` → exit(1), do not trade; unknown error → return null and halt. Recovery should complete in under ~10 seconds for most accounts; batch order queries to respect rate limits.

**Periodic reconciliation:** Run a light position-drift check every few minutes (e.g., 5 min) and a full reconciliation on any WebSocket reconnect. WebSocket is the fast path; REST reconciliation is the verification path.

**WebSocket reconnection:** Use exponential backoff with jitter on reconnect attempts, heartbeat ping-pong to detect dead connections, and message gap detection. On reconnect, run reconciliation before resuming trading (messages may have been dropped while disconnected).

---

## Topic 2 — Position Reconciliation on Restart

### Key Insight / Recommendation

Your bot's ledger and the exchange's reality *will* drift — through missed fills, manual trades placed outside the bot, fee dust, partial-fill rounding, and exchange corrections. A reconciliation loop catches that drift before it corrupts sizing and risk calculations. On restart, seed position state from exchange data (`fetchPosition`, `fetchBalances`) rather than trusting in-memory or on-disk state that may be stale.

Position reconciliation is the *final* check after order-level reconciliation. Even when every order is accounted for, position can still be wrong due to WebSocket messages lost during a crash, manual intervention, or retroactive exchange corrections.

### Source URLs

- https://blockcircle.com/blog/position-reconciliation-bot-exchange-state
- https://cotrading.io/learn/position-reconciliation
- https://matrixtrak.com/blog/crash-recovery-reconciliation-loops-trading-bots

### Practical Implementation Advice

**Use thresholds, not exact equality.** Never compare position size to the last decimal — fee dust and rounding make exact match impossible. Use a two-band rule:
1. **Relative band:** mismatch as % of position value (e.g., >0.1% of notional).
2. **Absolute floor:** mismatch in account currency (e.g., >$5).

A gap must clear *both* bands to count as a problem. Below both bands → silently snap ledger to exchange and log as dust (track in a dust accumulator per instrument; a growing one-directional total flags a rounding or fee bug).

**Classify before correcting:**
- Gap under thresholds → dust, snap silently, log for trend watching.
- Gap over thresholds but explainable (missed fill found in trade history) → replay the fill into the ledger, mark reconciled, low-severity alert.
- Gap over thresholds and *unexplained* → **halt trading that instrument and alert loudly.** An unexplained gap means either another writer is touching the account or your accounting has a live bug. Halting is safer than auto-correcting and papering over the symptom.

**Persist reconciliation decisions.** When you snap the ledger to exchange, write an explicit reconciliation event (append-only) recording what changed, when, and why. If the process dies after an in-memory correction but before persistence, you restart into the same wrong state. The audit trail also lets you reconstruct what the bot did during a bad hour.

**Reconcile on every startup before placing a single order**, and on a schedule during operation (market-making bots: every 1–2 minutes light, every few minutes full; slower strategies: every 15 minutes). Two writers touching the same account is a common drift source reconciliation will expose.

---

## Topic 3 — PnL Double-Counting Prevention

### Key Insight / Recommendation

PnL double-counting happens when the same fill/settlement is processed more than once — typically due to retries, duplicate webhook deliveries, or restart reprocessing. Prevention rests on **idempotency**: every fill, settlement, and PnL event must carry a globally unique identifier, and your ledger must reject or deduplicate any event whose ID has already been applied. Settlement reconciliation against exchange trade history provides an independent audit trail that catches drift your internal counters miss.

### Source URLs

- https://blog.n8n.io/idempotency-api/
- https://dev.to/dexoryn/how-to-prevent-duplicate-orders-in-a-polymarket-trading-bot-with-python-3p20
- https://matrixtrak.com/blog/crash-recovery-reconciliation-loops-trading-bots
- https://didit.me/blog/mastering-webhook-reliability-retry-and-dead-letter-queue-strategies/

### Practical Implementation Advice

**Idempotency keys on every state-changing event.** Generate a unique key per fill/settlement (from order ID + fill ID, or exchange trade ID). Store processed event IDs in a durable deduplication log (database table with a UNIQUE constraint, or append-only ledger). Before applying any fill to PnL, check if the event ID was already processed; if yes, return the cached result without side effects.

**Database-level enforcement.** Use unique constraints / conditional writes so that even concurrent duplicate requests cannot create duplicate PnL entries. This shifts part of the idempotency burden into the persistence layer.

**Natural idempotency where possible.** Prefer operations that are inherently safe to repeat (e.g., replacing a position snapshot with PUT-style semantics) over append-only counter increments. Incrementing a running PnL counter on every fill is dangerous; recompute PnL from the authoritative fill ledger instead.

**Settlement reconciliation.** Periodically reconcile your computed PnL against the exchange's realized PnL / trade history over REST. Any divergence beyond dust thresholds triggers an alert. This catches double-counting, missed fills, and fee misaccounting that internal counters alone won't reveal.

**Audit trail.** Log every PnL-affecting event with: event ID, order ID, symbol, side, size, price, fee, timestamp, and a `reconciled: true/false` flag. This gives you the trail to reconstruct PnL during a bad hour and to prove what was double-counted (or missed) after an incident.

**n8n-specific:** Generate idempotency keys from input data (e.g., order ID), not `execution.id`, when you need cross-retry idempotency (manual re-triggers get a new `execution.id`). Use n8n's Data table or an external DB to deduplicate inbound webhook delivery IDs before processing.

---

## Topic 4 — REST API Rate Limit Handling

### Key Insight / Recommendation

Rate limits (HTTP 429) are inevitable when trading against exchange REST APIs. Naive fixed-interval retry loops make things worse by synchronizing retries into "retry storms." The correct pattern is **exponential backoff with jitter** (wait, retry, double the wait, add randomness) so retries desynchronize, combined with a **token bucket** rate limiter that paces outbound requests proactively rather than reacting to rejections. Always respect `Retry-After` headers when present.

### Source URLs

- https://www.moesif.com/blog/technical/api-development/Mastering-API-Rate-Limiting-Strategies-for-Efficient-Management/
- https://truto.one/blog/best-practices-for-handling-api-rate-limits-and-retries-across-multiple-third-party-apis/
- https://api7.ai/blog/rate-limiting-guide-algorithms-best-practices
- https://www.lunar.dev/post/the-fundamentals-of-managing-api-rate-limits-developers-best-practices
- https://www.back4app.com/glossary/api-rate-limiting-throttling/

### Practical Implementation Advice

**Token bucket for outbound pacing.** Maintain a bucket per exchange (or per API key) with a capacity = burst allowance and refill rate = sustained requests/second. Each outbound request consumes a token; if the bucket is empty, delay the request rather than firing blindly. This lets controlled bursts through while holding the average rate under the limit.

**Exponential backoff with jitter on 429/5xx.** On a rate-limit or transient error:
- Wait `base * 2^attempt` seconds (e.g., 1s, 2s, 4s, 8s…).
- Add jitter: `wait = random(wait * 0.5, wait * 1.5)` or full jitter `random(0, wait)` so multiple clients don't retry in lockstep.
- Cap max retries (e.g., 3–5) and max wait (e.g., 60s) so a persistent failure doesn't consume executions forever.

**Respect `Retry-After`.** If the exchange returns a `Retry-After` header, wait at least that long before retrying. Ignoring it is a fast way to get IP/API-key banned.

**Distinguish retryable from non-retryable errors.** Retry on 429, 502, 503, 504, and network timeouts. Do *not* retry on 400, 401, 403, 404, or signature errors — retrying those just burns rate-limit budget. Circuit-breaker integration (Topic 5) should trip on sustained 429s.

**Batch queries during reconciliation.** Startup reconciliation may need many `fetchOrder`/`fetchOpenOrders` calls. Batch where the exchange allows it, and pace batches through the token bucket so reconciliation doesn't itself trigger rate limits.

---

## Topic 5 — Circuit Breaker Patterns

### Key Insight / Recommendation

A circuit breaker stops your bot from repeatedly calling a failing dependency (exchange API, n8n webhook, data feed) and allows the system to "fail fast" instead of cascading failures. It has three states:
- **CLOSED** — normal operation, requests pass through, failures counted.
- **OPEN** — failure rate exceeds threshold; requests are rejected immediately without calling the dependency.
- **HALF-OPEN** — after a cool-down, a limited number of trial requests are allowed; if they succeed, close the breaker; if they fail, reopen.

For trading, the circuit breaker should halt *new orders* (not necessarily cancel existing positions) and resume only after a safe probe confirms the dependency is healthy.

### Source URLs

- https://resilience4j.readme.io/docs/circuitbreaker
- https://www.geeksforgeeks.org/system-design/what-is-circuit-breaker-pattern-in-microservices/
- https://dev.to/shpreeti/how-the-circuit-breaker-pattern-prevents-cascading-failures-in-microservices-3nbi
- https://www.bookmap.com/blog/trading-circuit-breakers-and-halts-how-they-protect-markets-and-what-traders-should-know

### Practical Implementation Advice

**Configure per-dependency breakers.** Use separate circuit breakers for: exchange REST API, exchange WebSocket, market data feed, n8n webhook destination. A failure in market data should not necessarily kill order placement, and vice versa.

**Failure threshold.** Open the breaker when the failure rate (errors / total requests over a sliding window, e.g., last 100 calls) exceeds a threshold (e.g., 50%) or when consecutive failures hit a count (e.g., 5). For trading, be conservative: a small number of consecutive order failures should trip the breaker.

**Cool-down / reset.** After the breaker opens, wait a configurable cool-down (e.g., 30–60s) before entering HALF-OPEN. In HALF-OPEN, allow one probe request; success closes the breaker, failure reopens it and resets the cool-down (often with backoff).

**Trading halt semantics.** When OPEN:
- Reject new order submissions immediately (fail fast).
- Keep WebSocket/market-data listening if healthy so you can still monitor positions.
- Alert loudly (page/Slack/email) that trading is halted.
- Do not auto-resume; require either a successful HALF-OPEN probe or manual confirmation before re-enabling trading.

**Fallbacks.** Define a fallback for each protected call (e.g., return last-known price instead of fetching, or return "trade blocked" instead of placing). Never let a circuit-breaker rejection propagate as an unhandled exception that crashes the bot.

**Resume safely.** Before closing the breaker after a probe, verify position and order state are reconciled (Topic 2) — a prolonged outage may have left fills unprocessed, and you don't want to size new orders on stale state.

---

## Topic 6 — Dry-Run vs Live Trading Safety

### Key Insight / Recommendation

Dry-run (paper) and live trading must be strictly separated: different API keys, different accounts, and ideally different environment configs. The transition from paper to live is where most blowups happen (overfitted backtests, slippage, fees, leverage, execution latency). Guardrails include pre-trade risk checks before *every* order, allocation limits, kill switches that halt new orders and alert for manual review, and — critically — **tested** kill switches (an untested kill switch is decorative).

### Source URLs

- https://coinbureau.com/guides/crypto-trading-bot-mistakes-to-avoid
- https://www.quodfinancial.com/pre-trade-risk-controls-in-electronic-trading-guardrails-before-the-order-hits-the-wire/
- https://vantixs.com/blog/crypto-trading-bot-risk-limits-checklist
- https://dev.to/samhartley_dev/my-paper-trading-bot-went-live-this-week-heres-the-guardrail-stack-i-built-first-e48
- https://www.iatp.org/kill-switches-and-price-limits-safety-valves

### Practical Implementation Advice

**Hard separation.** Use distinct API keys (and distinct exchange accounts/subaccounts where possible) for dry-run and live. Load config from environment variables or a config file keyed by `TRADING_MODE=paper|live`, never by a commented-out line in source code. Assert at startup that the mode matches the key's intended environment.

**Pre-trade risk controls (guardrails before the order hits the wire).** On every order, check:
- Max position size per instrument and total portfolio exposure.
- Max notional per order and per day.
- Allowed instruments and order types.
- Min/max price sanity checks (reject orders far from mid).
- Cooldown between orders (prevent order loops).
- Circuit breaker state (reject if OPEN).

If any check fails, reject the order and log the reason. Do not let a buggy strategy bypass these checks.

**Kill switch.** Provide a kill switch that stops new orders immediately and alerts for manual review. It should be triggerable from multiple channels (bot command, external API/n8n webhook, phone/alert). Test it weekly in paper trading — an untested kill switch is decorative. The kill switch should cancel or leave open orders per policy (flat vs. flat-and-cancel) and persist its state across restarts.

**Dry-run first, then small live.** Run the strategy in paper until it produces stable results, then go live with a small size (a fraction of planned capital) and validate that fills, fees, slippage, and PnL match expectations before scaling.

**Audit the transition.** Log every mode switch (paper→live) with operator, timestamp, and reason. Keep paper and live PnL completely separate so you can't accidentally paper over live losses with paper gains.

---

## Topic 7 — n8n Webhook Reliability

### Key Insight / Recommendation

n8n webhooks are at-least-once delivery by default: the same webhook event can arrive multiple times (provider retries, timeouts, client retries). Without an idempotency gate, each delivery re-executes the workflow and can duplicate orders, fills, or PnL entries. The reliable pattern is: **bounded retries (3–5) → dead letter queue (DLQ)** for exhausted failures, plus **idempotent webhook handlers** that deduplicate on delivery/event ID before any side effect.

### Source URLs

- https://blog.n8n.io/idempotency-api/
- https://community.n8n.io/t/way-to-handle-dead-letter-queues-in-n8n-for-failed-jobs/298210
- https://techdome.io/insights/webhook-retries-idempotency-keys
- https://didit.me/blog/mastering-webhook-reliability-retry-and-dead-letter-queue-strategies/
- https://community.n8n.io/t/preventing-duplicate-webhook-executions-in-n8n-idempotency-gate-workflow/275249
- https://www.reddit.com/r/n8n/comments/1rkuh6x/webhook_retries_can_cause_duplicate_executions_in/

### Practical Implementation Advice

**Idempotency gate on inbound webhooks.** Extract the webhook's delivery ID / event ID from the payload or headers. Before processing, check it against a deduplication store (n8n Data table, Redis, or SQL with UNIQUE constraint). If already processed, return 200 OK without side effects. If new, record it, then process. This makes retries safe.

**Idempotency keys on outbound requests.** When n8n calls the exchange REST API, generate a unique idempotency key per operation (from order ID, not `execution.id` if you need cross-retry idempotency) and send it as an `Idempotency-Key` header. n8n's `execution.id` works for retries within one execution, but manual re-triggers get a new `execution.id` — key off business data for true dedup.

**Bounded retries + DLQ.** Configure the HTTP Request node with Max Tries and Wait Between Tries (exponential backoff preferred). When a workflow still fails after exhausting retries, route it via n8n's **Error Trigger** node to a DLQ: log the failed execution with its idempotency key, and route to a retry queue or alert. Never retry forever — a persistent error consumes executions and can cascade.

**Retry safety check before production:**
- Check whether POST/PATCH endpoints support idempotency keys; only enable automatic retries on idempotent endpoints.
- Use the same key when retrying an operation; use a new key for genuinely new operations.
- Put a hard limit on retry count.
- Monitor execution counts so a custom retry loop can't eat quota on a persistent failure.

**Separate concerns:** n8n handles orchestration reliability (retries, DLQ, error triggers); the bot's own idempotency keys and dedup log handle business-level dedup. Use both.

---

## Topic 8 — Trading Bot Logging & Observability

### Key Insight / Recommendation

Structured logging (JSON, one log per line, fixed field schema) is the foundation of observability for a trading bot. You must log not just errors, but order lifecycle events (sent/ack/filled/rejected/cancelled), reconciliation decisions, circuit breaker state changes, killswitch activations, and rate-limit events. Correlate logs across services with a correlation ID (order ID or execution ID). Alert on *error rates* and *state drift*, not just exceptions — silent drift (position wrong but no error) is more dangerous than a crash.

### Source URLs

- https://openobserve.ai/blog/structured-logging-best-practices/
- https://www.honeycomb.io/blog/engineers-checklist-logging-best-practices/
- https://www.parseable.com/blog/logging-best-practices/
- https://www.newrelic.com/blog/log/best-log-management-practices/
- https://logz.io/blog/logging-and-observability/

### Practical Implementation Advice

**Structured JSON logging.** Emit JSON with consistent fields: `timestamp`, `level`, `service`, `correlation_id` (order/execution ID), `event_type` (order_sent/order_filled/reconciliation/killswitch/rate_limit/circuit_open/…), `symbol`, `side`, `size`, `price`, `fee`, `message`, `error_code`. Avoid free-form strings as the sole carrier of information; put structured data in fields so you can query/alert on them.

**What to log (minimum set):**
- Every order sent to exchange (with client order idempotency key).
- Every exchange response/ack, fill, rejection, cancel.
- Every reconciliation run and its outcome (orphans found, ghosts removed, fills backfilled, position corrected).
- Every circuit breaker state transition (CLOSED→OPEN→HALF-OPEN→CLOSED).
- Every killswitch activation and every pre-trade risk rejection.
- Every rate-limit (429) and retry.
- Every mode switch (paper↔live).
- Every PnL-affecting event with event ID for dedup/audit.

**Correlation IDs.** Thread a correlation ID (order ID or execution ID) through all logs related to a single trade so you can trace an order from signal → API call → fill → PnL across restarts and services.

**Alert on the right signals.** Alert on: error rate spike (e.g., >5% of requests failing over 5 min), circuit breaker OPEN, killswitch activation, reconciliation drift beyond thresholds, unexplained position gap, duplicate event ID detected, missed heartbeat/WebSocket gap. Do not alert on every exception — alert on patterns and on business-impact conditions.

**Correlate logs with metrics and traces.** Logs tell you what happened; metrics (order rate, fill rate, error rate, position drift) tell you the trend; traces tie a request across services. Use metrics for alerting thresholds and logs for debugging individual incidents.

**Log retention and redaction.** Keep logs long enough to investigate incidents (weeks to months). Never log secrets (API keys, private keys, passwords). Redact or hash sensitive fields if they must appear.

---

## Cross-Cutting Recommendations

1. **Reconciliation is the master pattern.** It appears in Topics 1, 2, and 3. Build it first, test it, run it on every startup and on a schedule. It is the single highest-ROI reliability investment.
2. **Idempotency keys everywhere.** Client order IDs on orders; event IDs on fills/PnL/webhooks. Without them, retries and restarts are unsafe by default.
3. **Fail closed, not open.** When reconciliation fails, the exchange is down, the circuit breaker is OPEN, or drift is unexplained → halt trading and alert. Do not trade with unknown state.
4. **Test your safety gear.** Kill switches, circuit breakers, and reconciliation must be exercised in paper trading before trusting them live. Untested safety code is decorative.
5. **Separate paper and live completely.** Different keys, accounts, configs; assert mode at startup.
6. **Bounded retries + DLQ.** Never retry forever; exhausted failures go to a dead-letter queue with alerting.
7. **Structured logs with correlation IDs** and alerting on error rates/drift, not just exceptions.

---

## Startup / Recovery Checklist

Copy/paste this for every bot restart:

- [ ] Load local state from durable storage
- [ ] Create exchange connection with trading disabled
- [ ] Run reconciliation: orphan orders → ghost orders → stale fills → position
- [ ] Verify corrected position within risk limits
- [ ] Confirm circuit breaker is CLOSED (not OPEN)
- [ ] Confirm killswitch is not engaged
- [ ] Enable trading
- [ ] Connect WebSocket live data
- [ ] Verify heartbeat and first market-data message received
- [ ] Log reconciliation result and resume event

---

## Source Index

| # | Topic | Source | URL |
|---|-------|--------|-----|
| 1 | Crash recovery | MatrixTrak — Crash Recovery Reconciliation Loops | https://matrixtrak.com/blog/crash-recovery-reconciliation-loops-trading-bots |
| 2 | Crash recovery | MatrixTrak — Bot restart double orders | https://matrixtrak.com/errors/crash-recovery-double-orders-on-startup |
| 3 | Crash recovery | New Hope — Restart Recovery Checklist | https://newhope.uk.com/trading-bot-restart-recovery-checklist |
| 4 | Crash recovery | Vantixs — Exchange Outages Safe Recovery | https://vantixs.com/blog/handling-exchange-outages-crypto-bots |
| 5 | Position reconciliation | Blockcircle — Position Reconciliation | https://blockcircle.com/blog/position-reconciliation-bot-exchange-state |
| 6 | Position reconciliation | CoTrading — Position Reconciliation | https://cotrading.io/learn/position-reconciliation |
| 7 | PnL/idempotency | n8n Blog — API Idempotency | https://blog.n8n.io/idempotency-api/ |
| 8 | PnL/idempotency | Dev.to — Prevent duplicate orders Polymarket | https://dev.to/dexoryn/how-to-prevent-duplicate-orders-in-a-polymarket-trading-bot-with-python-3p20 |
| 9 | PnL/idempotency | Didit — Webhook Reliability Retry & DLQ | https://didit.me/blog/mastering-webhook-reliability-retry-and-dead-letter-queue-strategies/ |
| 10 | Rate limiting | Moesif — API Rate Limiting Strategies | https://www.moesif.com/blog/technical/api-development/Mastering-API-Rate-Limiting-Strategies-for-Efficient-Management/ |
| 11 | Rate limiting | Truto — API Rate Limits & Retries | https://truto.one/blog/best-practices-for-handling-api-rate-limits-and-retries-across-multiple-third-party-apis/ |
| 12 | Rate limiting | API7 — Rate Limiting Algorithms | https://api7.ai/blog/rate-limiting-guide-algorithms-best-practices |
| 13 | Circuit breaker | Resilience4j — CircuitBreaker | https://resilience4j.readme.io/docs/circuitbreaker |
| 14 | Circuit breaker | GeeksForGeeks — Circuit Breaker Pattern | https://www.geeksforgeeks.org/system-design/what-is-circuit-breaker-pattern-in-microservices/ |
| 15 | Circuit breaker | Dev.to — Circuit Breaker Cascading Failures | https://dev.to/shpreeti/how-the-circuit-breaker-pattern-prevents-cascading-failures-in-microservices-3nbi |
| 16 | Dry-run vs live | Coin Bureau — Crypto Trading Bot Mistakes | https://coinbureau.com/guides/crypto-trading-bot-mistakes-to-avoid |
| 17 | Dry-run vs live | Quod Financial — Pre-Trade Risk Controls | https://www.quodfinancial.com/pre-trade-risk-controls-in-electronic-trading-guardrails-before-the-order-hits-the-wire/ |
| 18 | Dry-run vs live | Vantixs — Crypto Trading Bot Risk Limits Checklist | https://vantixs.com/blog/crypto-trading-bot-risk-limits-checklist |
| 19 | Dry-run vs live | Dev.to — Paper Trading Bot Guardrail Stack | https://dev.to/samhartley_dev/my-paper-trading-bot-went-live-this-week-heres-the-guardrail-stack-i-built-first-e48 |
| 20 | n8n webhooks | n8n Community — Dead Letter Queues | https://community.n8n.io/t/way-to-handle-dead-letter-queues-in-n8n-for-failed-jobs/298210 |
| 21 | n8n webhooks | n8n Community — Idempotency Gate | https://community.n8n.io/t/preventing-duplicate-webhook-executions-in-n8n-idempotency-gate-workflow/275249 |
| 22 | n8n webhooks | Reddit n8n — Webhook Retries Duplicates | https://www.reddit.com/r/n8n/comments/1rkuh6x/webhook_retries_can_cause_duplicate_executions_in/ |
| 23 | Logging/obs | OpenObserve — Structured Logging Best Practices | https://openobserve.ai/blog/structured-logging-best-practices/ |
| 24 | Logging/obs | Honeycomb — Engineers Checklist Logging | https://www.honeycomb.io/blog/engineers-checklist-logging-best-practices/ |
| 25 | Logging/obs | Parseable — Logging Best Practices | https://www.parseable.com/blog/logging-best-practices/ |
| 26 | Logging/obs | New Relic — Log Management Best Practices | https://www.newrelic.com/blog/log/best-log-management-practices/ |

---

*End of report. This document is a reference for implementation; it does not modify any bot code. See [STARTUP_CHECKLIST](#startup-checklist) for the immediate operational actions.*
