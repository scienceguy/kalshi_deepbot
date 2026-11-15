# Polymarket US — Settlement Latency Monitor: Technical Specification

**Status:** design spec (no implementation applied). Supersedes nothing; extends
`docs/poly_us_settlement_latency.md` (research, "R1..R6" recommendations).
**Target:** paper trading engine `poly_us_trader.py` (`check_settlements`).
**Constraints honoured:** no real orders placed, no live execution path modified,
stdlib only, no new dependencies, paper mode untouched by default.

---

## 1. Scope and deliverables

- Polls settled markets at configurable intervals (default 30s after close).
- Detects settled positions, credits cash at the venue settlement price
  (100c win / 0c loss), books PnL.
- Logs settlement latency: `close_time -> settlement detection -> cash credit`.
- Appends settlement events to `local_data/poly_us_paper_ledger.jsonl`
  with `payout`, `pnl`, `cash_after`.
- Alert threshold: flag if settlement detection takes `> 60s` after market close.

Deliverables of this document: mechanics research (Section 2), monitor design
(Section 3), pseudocode (Section 4), latency math (Section 5), alert
thresholds (Section 6), failure modes and tests (Sections 7-8).

**Out of scope:** live-mode order placement, WebSocket migration of the paper
engine, auto-reset/bankroll policy, market scanning and entry signals.

---

## 2. Settlement mechanics (researched against `api.polymarket.us` / `gateway.polymarket.us`)

### 2.1 Endpoints

| Purpose | Endpoint | Auth | Notes |
|---|---|---|---|
| Market by slug | `GET /v1/market/slug/{slug}` | public | `status`, `closed`, `assetPriceTerms.windowEnd` |
| Settlement | `GET /v1/markets/{slug}/settlement` | public | `{"slug", "settlement": 0.00\|1.00}` |

`polymarket_us_client.get_market_settlement()` wraps the SDK call
`markets.settlement(slug)` and normalises the result to either
`{"slug": slug, "settlement": float}` or `None`. `None` conflates
404-not-settled, 404-not-found, 500s and network errors (see R2 of the
research doc).

### 2.2 Semantics and status enums (observed, not assumed)

Live probe of closed crypto markets on 2026-11-14:

- Terminal state field is `status="MARKET_STATUS_RESOLVED"` (enum carries the
  `MARKET_STATUS_` prefix). Intermediate state is `MARKET_STATUS_RESOLVING`,
  and `MARKET_STATUS_CLOSED` appears around expiry. **The current code in
  `check_settlements()` compares `status in ("RESOLVED", "RESOLVING", "CLOSED")`,
  which never matches** — a real bug, see Section 8.1.
- Settlement returns `settlement: 0.0` or `1.0`. Winning contracts pay $1.00,
  losers $0.00 (Exchange Rulebook; per-market alternative terms possible).
- 404 "not settled" and 404 "not found" are indistinguishable from the status
  code alone; the docs state both use 404.
- Settlement of `updown-5m` / `updown-*` crypto windows is keyed to
  `assetPriceTerms.windowEnd`, the scheduled settlement instant, which for
  15-minute and hourly families lands at `:00 UTC` (e.g. `...T13:00:00Z`).
  Use `windowEnd`, not `endDate` / `gameStartTime`.
- Portfolio (real, authenticated) updates automatically when settlement
  completes; the paper engine must replicate this by crediting cash.

### 2.3 Finality timing

Polymarket publishes **no SLA** between `windowEnd` and first published
`settlement`. Per the Market Resolution doc: resolution occurs after the
event outcome is publicly confirmed; timing varies by event type. For crypto
up/down families driven by Coinbase/Chainlink prints, settlement is expected
shortly after the window closes and the price feed is confirmed; measured
deltas come from the monitor itself (Section 5), not from documentation.

Recommended interpretation for the paper engine:

- NORMAL: settlement published within ~60s of `windowEnd`.
- MONITORING: `windowEnd` to +5min, poll at poll interval.
- DEGRADED: >5min unresolved (possible price-feed issue or slow oracle
  confirmation) — keep position, back off, but do not discard it.

### 2.4 REST polling vs WebSocket push

- International CLOB market channel (`wss://ws-subscriptions-clob.polymarket.com/ws/market`)
  emits `market_resolved` (`winning_asset_id`, `winning_outcome`) when
  `custom_feature_enabled: true`. **This is the global CLOB, not the US gateway
  (`gateway.polymarket.us` / `api.polymarket.us`)**; the US paper engine does
  not subscribe to it.
- US private WS (`/v1/ws/private`, referenced in the US rate-limit guidance)
  pushes position/balance changes on settlement. The **paper** engine has no
  real venue position to receive on; the WebSocket worker was removed (A11)
  because nothing used it and REST polling covers the paper engine.
- **Decision:** the paper monitor uses REST polling only. WebSocket can be an
  R6 fast-path for live deployment later. REST remains the source of truth
  and the reconciliation path.

### 2.5 Rate limits

Retail API limit is ~25 req/s shared across endpoints; 5 req/s is the
project-safe ceiling. `polymarket_us_client.POLYMARKET_MIN_GAP_SECONDS=0.20`
is respected by every helper. Back off exponentially on 429 (current client
does 1s, 2s, 4s across three attempts).

---

## 3. Monitor design in `poly_us_trader.py`

### 3.1 Where it lives

All changes are confined to paper-mode paths:

- `check_settlements()` — rewrite per research doc R1-R5 (status branch,
  `windowEnd` keying, adaptive polling, idempotency).
- `cycle()` — unchanged except that `check_settlements()` is called every
  cycle; the new poll cadence comes from a per-position next-poll timestamp
  (poll pacing), not from the outer loop.
- `_append_ledger()` — reuse as-is; settlement events get extra fields.
- `_save_state()` — unchanged (atomic write already).

No other function signatures change. Live-mode order placement is untouched.

### 3.2 Configuration (env / config.py, stdlib only)

| Knob | Default | Meaning |
|---|---|---|
| `POLY_SETTLEMENT_POLL_INTERVAL` | `30` | seconds between settlement polls once past `windowEnd` (clamped to `[5, 300]`) |
| `POLY_SETTLEMENT_DETECT_SLA_SECONDS` | `60` | soft alert threshold (detection after `windowEnd`); drives logs + ledger flag |
| `POLY_SETTLEMENT_HARD_GIVE_UP_SECONDS` | `3600` | stop expecting settlement, mark stale, keep cash locked but surface to operator |
| `POLY_SETTLEMENT_MAX_BACKOFF` | `300` | cap for exponential backoff on persistent errors |
| `SETTLEMENT_ALERT_HOOK` | unset | optional shell command run on SLA breach (best-effort, never blocks the loop) |

### 3.3 State schema (positions get settlement-tracking fields)

Add to each position dict in `state["positions"]` (all additive, missing keys
mean "not yet tracked", backward compatible):

```
close_time            ISO-8601 UTC, from assetPriceTerms.windowEnd (fallback endDate/gameStartTime)
settlement_state      "open" | "resolving" | "settled" | "stale"
last_settle_poll_ts   epoch seconds of last settlement REST call
next_settle_poll_ts   epoch seconds; poll only when now >= this
settlement_payload    settled payload (settlement float), once observed
settlement_detected_at ISO-8601, when settlement first observed 200
settlement_latency_seconds float, close_time -> detected (see Section 5)
settlement_credit_latency_seconds float, close_time -> cash credited
settle_alerts         list of alert keys already fired for this position (idempotency for alerts)
```

`state["positions"]` keeps its current shape for MTM, rotation, and display;
only three new event-ish keys on `state` itself:

```
state["last_settlement_at"]   ISO-8601 of last credited settlement
state["settlement_stats"]      {count, latency_secs: [bounded rolling list, cap 200]}
state["pending_settle_alerts"] maybe-list; not required if alert hook is disabled
```

### 3.4 Ledger schema (`local_data/poly_us_paper_ledger.jsonl`)

The existing settlement record is retained verbatim (event, slug, side, qty,
entry_price, payout, pnl, cash_after) with new latency/alert fields appended:

```json
{
  "event": "settlement",
  "slug": "cpc-btc-updown-15m-2026-11-14-1400z",
  "side": "yes",
  "qty": 5,
  "entry_price": 0.97,
  "cost": 4.85,
  "payout": 5.0,
  "pnl": 0.15,
  "cash_after": 85.17,
  "settlement": 1.0,
  "payout_per_contract": 1.0,
  "close_time": "2026-11-14T14:00:00+00:00",
  "settlement_detected_at": "2026-11-14T14:00:41+00:00",
  "settlement_credited_at": "2026-11-14T14:00:41+00:00",
  "latency_detect_seconds": 41.2,
  "latency_total_seconds": 41.3,
  "latency_source": "windowEnd",
  "sla_breach": false,
  "poll_interval_seconds": 30,
  "settlement_state": "settled",
  "timestamp": "2026-11-14T14:00:41+00:00"
}
```

`timestamp` stays the credit wall-clock for backward compatibility with any
reader keying on it; `latency_*` fields are additive.

### 3.5 Algorithm (what the monitor does each cycle)

```
for each position p in state["positions"]:
    resolve close_time(p)  (cached onto position; else parse terms.windowEnd)
    if p.settlement_state == "settled": continue            # idempotent, should not happen
    if now < poll-eligible time: keep, continue              # no API call before close
    if p has no close_time and market_map lacks it: keep

    poll-eligible = (now - close_time) > 0 and now >= next_settle_poll_ts
    if not poll-eligible: continue

    fetch settlement (respect 0.20s rate limit spacing)
    classify: settled(200) | not_settled(404) | error(5xx/network/429)

    if settled:
        compute payout, pnl
        credit cash, set realized_pnl
        cal_monitor.reconcile_settlement(slug, outcome)     # existing behaviour preserved
        mark position settled, store latency fields
        append ledger settlement record (Section 3.4)
        save state
        emit SLA alert if latency_detect > POLY_SETTLEMENT_DETECT_SLA_SECONDS
    elif not_settled:
        backoff: next_settle_poll_ts = now + POLL_INTERVAL   # steady poll after close
    else (error):
        exponential backoff: next = min(MAX_BACKOFF, POLL_INTERVAL * 2**consecutive_errors)
        if consecutive_errors large, log warning; never drop position
    if now - close_time > POLY_SETTLEMENT_HARD_GIVE_UP_SECONDS:
        mark stale; raise a "settlement stuck" alert once; keep position
```

Key properties: no settlement REST call before close (0 calls for live
markets), one call per `POLL_INTERVAL` per closed position, idempotent credit,
per-position pacing decoupled from the 15s main loop, and SLA breach detection
with alert idempotency.

### 3.6 Code skeleton (reference, not applied)

```python
# ponytail: stdlib only (time, calendar, json, logging, os, subprocess)
import calendar
from datetime import datetime, timezone

SETTLE_POLL = int(os.environ.get("POLY_SETTLEMENT_POLL_INTERVAL", "30"))
SETTLE_SLA = float(os.environ.get("POLY_SETTLEMENT_DETECT_SLA_SECONDS", "60"))
SETTLE_FRIDGE = float(os.environ.get("POLY_SETTLEMENT_HARD_GIVE_UP_SECONDS", "3600"))
SETTLE_BACKOFF_CAP = float(os.environ.get("POLY_SETTLEMENT_MAX_BACKOFF", "300"))
SETTLE_ALERT_HOOK = os.environ.get("SETTLEMENT_ALERT_HOOK")  # optional shell cmd

def _iso_to_ts(s):
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt.timestamp()

def check_settlements_with_latency(self, market_map):
    remaining = []
    now_ts = time.time()
    for pos in self.state.get("positions", []):
        m_info = market_map.get(pos["slug"]) or {}
        terms = m_info.get("assetPriceTerms") or {}
        close_time_str = (pos.get("close_time") or terms.get("windowEnd")
                          or m_info.get("endDate") or m_info.get("gameStartTime"))
        if not close_time_str:
            remaining.append(pos)          # no timing info: hold forever, as today
            continue
        close_ts = pos.get("close_ts") or _iso_to_ts(close_time_str)
        pos["close_ts"] = close_ts
        pos["close_time"] = datetime.fromtimestamp(close_ts, timezone.utc).isoformat()

        if now_ts < close_ts:
            pos.setdefault("settlement_state", "open")
            remaining.append(pos)          # pre-close: never hit the settlement endpoint
            continue

        if now_ts < pos.get("next_settle_poll_ts", 0.0):
            remaining.append(pos)          # steady cadence paced per position
            continue

        if pos.get("settlement_state") == "settled":
            remaining.append(pos)
            continue

        # pace: steady poll after close, exponential backoff on errors
        errors = pos.get("settle_errors", 0)
        wait = SETTLE_POLL if errors == 0 else min(SETTLE_BACKOFF_CAP, SETTLE_POLL * (2 ** min(errors, 8)))
        pos["last_settle_poll_ts"] = now_ts

        try:
            time.sleep(0.05)
            settle = polymarket_us_client.get_market_settlement(pos["slug"])
        except Exception as e:
            pos["settle_errors"] = errors + 1
            pos["next_settle_poll_ts"] = now_ts + wait
            log.debug("Settlement poll error %s: %s", pos["slug"], e)
            remaining.append(pos)
            continue

        if settle is None or "settlement" not in settle:
            pos["settle_errors"] = errors + 1     # 404 / 429 / transient
            pos["next_settle_poll_ts"] = now_ts + wait
            pos["settlement_state"] = "resolving"
            if now_ts - close_ts > SETTLE_FRIDGE and "fridged" not in pos:
                pos["settlement_state"] = "stale"
                self._append_ledger({"event": "settlement_stale", "slug": pos["slug"],
                                      "close_time": pos["close_time"],
                                      "timestamp": datetime.now(timezone.utc).isoformat()})
                self._maybe_alert("settlement_stuck", pos, now_ts)
            remaining.append(pos)
            continue

        settlement = float(settle["settlement"])
        payout_per = settlement if pos["side"] == "yes" else 1.0 - settlement
        total_payout = round(payout_per * pos["qty"], 4)
        cost = pos["cost"]
        pnl = round(total_payout - cost, 4)
        detected_ts = now_ts
        credited_ts = time.time()
        lat_detect = round(detected_ts - close_ts, 3)
        lat_total = round(credited_ts - close_ts, 3)

        self.state["cash"] = round(self.state["cash"] + total_payout, 4)
        self.state["realized_pnl"] = round(self.state["realized_pnl"] + pnl, 4)
        try:
            self.cal_monitor.reconcile_settlement(pos["slug"], 1 if settlement >= 0.5 else 0)
        except Exception:
            pass

        pos.update({
            "settlement_state": "settled",
            "settlement_payload": settlement,
            "settlement_detected_at": datetime.fromtimestamp(detected_ts, timezone.utc).isoformat(),
            "settlement_latency_seconds": lat_detect,
            "settlement_credit_latency_seconds": lat_total,
            "settle_errors": 0,
        })
        sla_breach = lat_detect > SETTLE_SLA
        self._append_ledger({
            "event": "settlement",
            "slug": pos["slug"], "side": pos["side"], "qty": pos["qty"],
            "entry_price": pos["price"], "cost": cost,
            "payout": total_payout, "pnl": pnl,
            "cash_after": self.state["cash"],
            "settlement": settlement,
            "payout_per_contract": payout_per,
            "close_time": pos["close_time"],
            "settlement_detected_at": pos["settlement_detected_at"],
            "settlement_credited_at": datetime.fromtimestamp(credited_ts, timezone.utc).isoformat(),
            "latency_detect_seconds": lat_detect,
            "latency_total_seconds": lat_total,
            "sla_breach": sla_breach,
            "poll_interval_seconds": SETTLE_POLL,
            "settlement_state": "settled",
            "timestamp": datetime.fromtimestamp(credited_ts, timezone.utc).isoformat(),
        })
        self._maybe_alert("sla_breach", pos, now_ts) if sla_breach else None
        log.info("SETTLED %s: detect_latency=%.1fs total=%.1fs payout=$%.2f pnl=%+.2f",
                 pos["slug"], lat_detect, lat_total, total_payout, pnl)
        # position removed: do NOT append to remaining
    # positions list replaced with remaining (same effect as today)
    self.state["positions"] = remaining
    self._save_state(self.state)
```

`_maybe_alert` is a tiny helper: appends one ledger `settlement_alert` record
per `(slug, alert_key)` (checked against `pos["settle_alerts"]`), logs a
WARNING, and optionally runs `SETTLEMENT_ALERT_HOOK <slug> <key>` via
`subprocess.run(..., timeout=5)`, never raising. No new dependency.

### 3.7 Interaction with existing paper paths (regression guardrails)

- The 92-99c favourite entry logic, calibration monitor wiring, tier sizing,
  MTM rotation, milestone tracking and the `<= $34.95` auto-reset are
  untouched.
- `test_check_settlements_yes_win` / `test_check_settlements_no_win` continue
  to pass: the new path credits cash and removes the position exactly the
  same way when settlement is observed. New unit tests should mock
  `next_settle_poll_ts` pacing or set it to 0.
- `cal_monitor.reconcile_settlement` behaviour preserved.
- Before close, the monitor issues **zero** settlement REST calls - this only
  removes wasteful polling, it cannot change fill behaviour.

---

## 4. Pseudocode (compact form)

```
SETTLE_MONITOR(positions, market_map, now):
  for p in positions:
    close_ts = p.close_ts or parse(market_map[p.slug].assetPriceTerms.windowEnd)
    if close_ts is None: keep(p); continue
    if now < close_ts: mark p.state=open; keep(p); continue        # no API call pre-close
    if p.state == settled: keep(p); continue
    if now < p.next_settle_poll_ts: keep(p); continue               # paced polling

    res = GET /v1/markets/{p.slug}/settlement   # via get_market_settlement()
    case res:
      200 {settlement s}:
        payout = (s if p.side=="yes" else 1-s) * p.qty
        pnl = payout - p.cost
        cash += payout; realized_pnl += pnl
        lat_detect = now - close_ts
        lat_total = time_after_credit - close_ts
        ledger.append(settlement event, payout, pnl, cash_after,
                      close_time, detected_at, credited_at, lat_detect, lat_total,
                      sla_breach = lat_detect > SLA)
        remove p from positions
        if sla_breach: ALERT("sla_breach", p)
      404 not settled:
        p.state = resolving
        p.next_settle_poll_ts = now + POLL_INTERVAL
        if now - close_ts > GIVE_UP and not p.stale_flagged:
          p.state = stale; ledger.append(settlement_stale); ALERT("settlement_stuck", p)
        keep(p)
      5xx / network / 429:
        p.settle_errors += 1
        p.next_settle_poll_ts = now + min(BACKOFF_CAP, POLL_INTERVAL * 2**errors)
        keep(p)
  save_state(); save ledger
```

---

## 5. Latency tracking: definitions and math

All times are Unix epoch seconds (UTC). Let:

- `T_close` = scheduled settlement instant = `assetPriceTerms.windowEnd`
  (fallback `endDate` / `gameStartTime` only when `windowEnd` absent).
- `T_detect` = wall clock of the first poll returning HTTP 200 with
  `settlement in {0.0, 1.0}` for that slug.
- `T_credit` = wall clock immediately after the cash credit + ledger append
  complete (same cycle, so `T_credit - T_detect < 1s` in practice).

Metrics per settled position (rounded to 0.1s):

```
latency_detect_seconds  = T_detect - T_close      # venue + polling detection lag
latency_total_seconds   = T_credit - T_close      # end-to-end incl. booking
credit_overhead_seconds = T_credit - T_detect     # booking cost (expect < 1s)
poll_gap_seconds        = T_detect - T_prev_poll  # observed cadence actually used
```

Rolling aggregates in `state["settlement_stats"]` (cap list at 200 entries,
drop oldest):

```
n          = count(latency_detect_seconds)
median     = sort(lat)[n//2]                      # n odd; avg of middle two if even
p90        = sort(lat)[ceil(0.90*n) - 1]          # nearest-rank, no interpolation
mean       = sum(lat) / n
max        = max(lat)
sla_rate   = count(lat > SLA) / n                 # fraction breaching threshold
```

Cash-impact accounting (no new money, pure measurement):

```
expected_payout = (settlement == 1.0 ? qty : 0) for yes-side   # 100c win / 0c loss
actual_credit   = total_payout booked
delta           = actual_credit - expected_payout              # must be 0; alert if != 0
```

Interpretation: `latency_detect` is the only term the venue controls; the poll
interval contributes at most `POLL_INTERVAL` of additional detection delay, so
`latency_detect <= venue_lag + POLL_INTERVAL` by construction. The 60s SLA in
Section 6 therefore effectively requires `venue_lag <= 60 - POLL_INTERVAL`
when polling at the default 30s cadence.

---

## 6. Alert threshold definitions

Primary threshold (required by the task):

| Alert | Condition | Action |
|---|---|---|
| `sla_breach` | `latency_detect_seconds > 60` for any single position | log WARNING, append `settlement_alert` ledger record once per `(slug, key)`, run `SETTLEMENT_ALERT_HOOK` if set |

Supporting thresholds (recommended, all configurable):

| Alert | Condition | Rationale |
|---|---|---|
| `settlement_stuck` | `now - T_close > 3600` and still not settled | venue or oracle issue; position cash locked; operator must see it |
| `settle_error_storm` | `settle_errors >= 5` consecutive for one slug | API/5xx flapping; backoff has saturated at cap |
| `payout_mismatch` | `delta != 0` (Section 5) | booking bug; halt settlements path until inspected |
| `sla_rate_high` | rolling `sla_rate > 0.20` over last 20 settlements | systematic slowness, not a one-off |
| `credit_overhead_high` | `credit_overhead_seconds > 5` | booking path (state save, ledger fsync) regressed |

Threshold derivation for the 60s default: with `POLL_INTERVAL=30`, a venue
that publishes settlement within 30s of close is always detected within 60s.
Breaching 60s therefore means either venue lag > 30s (real delay) or the poll
was missed/backed off (engine-side issue) - both are operator-actionable and
both are exactly what this monitor exists to surface. The 3600s give-up is 60x
the SLA, far outside normal crypto-window resolution, and never discards cash.

Alert record shape (ledger):

```json
{"event": "settlement_alert", "slug": "...", "alert_key": "sla_breach",
 "latency_detect_seconds": 87.3, "close_time": "...", "timestamp": "..."}
```

Idempotency: `pos["settle_alerts"]` list holds fired keys; a key fires once
per position per run and is persisted in state, so restarts do not re-alert on
old breaches.

---

## 7. Failure modes and how the design handles them

| Failure | Behaviour |
|---|---|
| 404 not settled (normal pre-resolution) | keep position, poll again at next interval |
| 404 not found (dead slug) | indistinguishable from not-settled on status alone; `settlement_stuck` at +3600s surfaces it; cash stays booked as open position (conservative, never silently lost) |
| 5xx / network error / 429 | exponential backoff `POLL_INTERVAL * 2**errors`, cap `SETTLEMENT_BACKOFF_CAP`; position kept |
| Crash between cash credit and `_save_state` | position not removed => on restart the poll re-observes settlement; to make credit idempotent, guard with `pos["settlement_state"] == "settled"` and skip re-credit (R5 of research doc). Minimal viable guard: check `pos.get("settlement_state") == "settled"` before crediting; full idempotency key per `(slug, side, qty)` is a follow-up |
| `windowEnd` missing or malformed | fall back to `endDate`/`gameStartTime`; if both absent, hold position and never poll settlement (same as today) |
| Clock skew | all comparisons in epoch seconds from the same `time.time()` source; `windowEnd` parsed as UTC |
| Ledger append fails (disk full) | exception propagates to `cycle()`'s existing catch; position stays open; retry next cycle (credit is state-driven, not ledger-driven) |

---

## 8. Testing and verification plan

Unit tests (stdlib `unittest`, mock `polymarket_us_client.get_market_settlement`):

1. Pre-close: no settlement HTTP call made; position retained.
2. Post-close 404: position retained, `next_settle_poll_ts` advanced by `POLL_INTERVAL`.
3. Settlement 200 YES win: cash credited `+qty*1.0`, pnl `qty - cost`, position removed, ledger record has `payout`, `pnl`, `cash_after`, `latency_detect_seconds`, `sla_breach=false`.
4. Settlement 200 NO win (settlement=0, side=yes): payout 0, pnl `-cost`.
5. SLA breach: `lat_detect = 90 > 60` => `sla_breach=true` in ledger, `settle_alerts` contains `sla_breach`, second breach does not re-alert.
6. Error storm: 5 consecutive errors => backoff saturates at cap, position kept, `settle_errors` grows.
7. Idempotency: `settlement_state == "settled"` position is skipped, no double credit after simulated restart.
8. Existing tests `test_check_settlements_yes_win` / `_no_win` still pass (they inject `{"closed": True}` market info, which the new path must keep honouring via the `closed` boolean OR `windowEnd` past).

Verification commands (paper only, no live flags):

```
python -m pytest tests/test_poly_us_trader.py -q
POLY_SETTLEMENT_POLL_INTERVAL=5 python poly_us_trader.py --once   # dry paper cycle
grep settlement local_data/poly_us_paper_ledger.jsonl | tail -1    # confirm schema
```

---

## 9. Known bug found during research (fix together with this spec)

`check_settlements()` lines 202-205 compare `status in ("RESOLVED", "RESOLVING", "CLOSED")`,
but the live API returns `MARKET_STATUS_RESOLVED` / `MARKET_STATUS_RESOLVING`
(verified 2026-11-14). The status branch therefore never fires; expiry is
detected only via the `closed` boolean and the `windowEnd` time comparison.
The new monitor should match on the suffix (`status.endswith("RESOLVED")`) or
strip the `MARKET_STATUS_` prefix.

---

## 10. Rollout order (smallest safe steps)

1. Fix the status-prefix bug (Section 9) behind the existing `closed`-boolean
   path - no behaviour change, strictly more correct.
2. Add the config knobs and per-position pacing fields (additive only).
3. Land the monitor with `POLY_SETTLEMENT_POLL_INTERVAL=30` default, SLA alert
   logging to ledger only (hook unset).
4. Observe `latency_detect_seconds` distribution for a few days of 15m windows;
   only then tune POLL_INTERVAL / SLA against measured p90 (research doc open item).
5. Optionally enable `SETTLEMENT_ALERT_HOOK` for operator notification.

No step places orders, touches live execution, or changes entry/sizing logic.

---

## 11. Summary

- Settlement on Polymarket US is REST-polled (`GET /v1/markets/{slug}/settlement`),
  terminal state `MARKET_STATUS_RESOLVED`, payout 100c/0c; no documented SLA,
  no US-gateway WS push for the paper engine.
- Monitor: zero polls before `windowEnd`, paced polls after, per-position
  exponential backoff on errors, idempotent credit, ledger carries
  `payout`/`pnl`/`cash_after` plus `latency_detect_seconds` and `sla_breach`.
- Latency math: `latency_detect = T_detect - T_close`, `latency_total = T_credit - T_close`;
  rolling median/p90/max/sla_rate in state.
- Alerts: primary `sla_breach` at `> 60s` detection latency; supporting
  stuck/storm/mismatch/rate/overhead thresholds, each fired once per position.
- Known bug: status enum prefix mismatch in the current code; fix with the rollout.

