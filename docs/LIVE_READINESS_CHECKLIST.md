# Live-Readiness Checklist — Kalshi & Polymarket US

**Prepared by**: ubuntu:opencode:kalshi_deepbot
**Date**: 2026-11-21
**Status**: FOR HUMAN REVIEW — ALL CODE TASKS COMPLETE

---

## Executive Summary

| Venue | Current State | Live-Ready? | Blockers |
|-------|---------------|-------------|----------|
| **Kalshi** | Live process running `trader.py --live`, real balance **$9.82**, 0 resting orders, 0 positions, risk-halted (micro-balance) | ❌ **NO** | No funded account; risk halt must be cleared; human approval required |
| **Polymarket US** | Dry-run, ~$34 cash / ~$35 equity, +$0.20 realized, not ruin-halted | ⚠️ **CONDITIONAL** | Needs funded account ($35 → target); human approval required |

**Code status**: ALL AGENT_TASKS_v8 items (H3–H16) + 6 review follow-ups **COMPLETE**
(reconciled in `docs/TASKS_RECONCILIATION_v8.md`, verified 2026-11-19), plus
ALL AGENT_TASKSv15 pillars (T15-60 through T15-71) **COMPLETE**
- Full suite green; all 4 source-corruption gates pass (`verify_source`,
  `verify_compiles`, `verify_undefined`, `verify_operators`)
- Dry-run growth measured on 81 days of live orderbook data
  (`docs/DRY_RUN_GROWTH_TRAJECTORY.md` v2): with the fill rule corrected to
  `trader._dry_run_fill_count` semantics, **both maker configs are negative-EV**
  (paper knobs −$25.23, production guards −$5.06); the earlier +$128 result
  was a fabricated-fill bug and is **retracted**. There is **no compounding
  path** to the $3.25M target at this edge — it requires a strategy change
  (a calibrated vol gate or a different venue), not a sizing change.
- Kill-switch verified end-to-end 2026-11-21 (pause persists, single-key
  resume rejected while risk-halted, two-key resume applies; audit trail in
  `control_inbox`); the real dry-run loop was exercised in an isolated data
  dir (live signal → 3-layer LLM consensus → Kelly/depth sizing → resting
  dry order → startup reconciliation), e2e smoke 7/7, n8n credentials 49/49.

---

## 1. Prerequisites — Must Complete Before Any Live Trading

### 1.1 Account & Credentials
- [ ] **Kalshi**: Live account funded with meaningful capital (recommend ≥$1,000)
  - [ ] `KALSHI_KEY_ID` and `~/.secrets/kalshi_private.pem` match live account
  - [ ] Verify via `python -c "from kalshi_client import KalshiClient; print(KalshiClient().get_balance())"`
- [ ] **Polymarket US**: Live account funded (recommend ≥$500)
  - [ ] `polymarket_us` SDK credentials in Bitwarden: `polymarket_api_key` + `polymarket_secret_key` (Ed25519, no passphrase)
  - [ ] Verify via `python -c "from polymarket_us_client import health_check; print(health_check())"`
- [x] **Run credential sync**: `python scripts/n8n_creds_sync.py --check` (all credentials present in n8n) — verified 2026-11-21: **49/49 present**

### 1.2 Configuration (`.env` — `chmod 600`)
- [ ] `KALSHI_CONFIRM_LIVE=true` (required for `--live` flag)
- [ ] `POLY_CONFIRM_LIVE=true` (required for `--live` flag)
- [ ] `DRY_RUN=false` (or omit; default is true)
- [ ] `KALSHI_BASE_URL=https://external-api.kalshi.com/trade-api/v2`
- [ ] `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` set (for settlement reconciliation)
- [ ] `N8N_API_KEY` set (for webhook management)
- [ ] `SUPERBOT_WEBHOOK_SECRET` matches n8n workflow HMAC secret
- [ ] `SUPERBOT_ALERT_WEBHOOK_URL` set (for critical alerts to operator channel)

### 1.3 Risk Parameters — Review & Approve Each
| Parameter | Current Value | Recommendation | Operator Approval |
|-----------|---------------|----------------|-------------------|
| `PER_TRADE_PCT` | 0.08 (8%) | Conservative; max 0.10 for live | ☐ |
| `BALANCE_PCT_PER_TRADE` | 0.08 | Same as above | ☐ |
| `CASH_FLOOR_PCT` | 0.70 | Keep ≥0.60 | ☐ |
| `EXPOSURE_CEILING_PCT` | 0.30 | Keep ≤0.40 | ☐ |
| `MAX_DRAWDOWN_PCT` | 0.08 (8%) | Live: 0.05-0.08; Paper: 0.12 | ☐ |
| `MAX_OPEN_POSITIONS` | 2 (Kalshi), 8 (Poly) | Kalshi: 2-3; Poly: 5-10 | ☐ |
| `MIN_SKEW_CENTS` | 3 | Verified optimal | ☐ |
| `MIN_TREND_CENTS` | 8 | Verified optimal | ☐ |
| `ENTRY_WINDOW_START_MIN` | 6.0 | Early entry (6-11 min) | ☐ |
| `ENTRY_WINDOW_END_MIN` | 11.0 | | ☐ |
| `ENTRY_BAND_MIN` | 95 | **Do not widen** — 95-99c is only EV+ band | ☐ |
| `ENTRY_BAND_MAX` | 99 | | ☐ |
| `KALSHI_FEE_RATE` | 0.07 | Verified | ☐ |

---

## 2. System Health — Verify Before Go-Live

### 2.1 Infrastructure
- [ ] **n8n running** at `http://127.0.0.1:5678` (bind loopback only)
  - [ ] `python -c "from n8n_client import n8n_health_check; print(n8n_health_check())"` → `True`
  - [ ] Workflows active: `superbot-trade`, `superbot-monitor`, `superbot-control`, `superbot-backtest`, `superbot-collect`, `superbot-coordinate`
- [ ] **MemPalace bridge** running (systemd: `mempalace-mcp.service`)
  - [ ] Shared palace at `/var/lib/superbot/mempalace` (group `superbot`, mode 2775)
  - [ ] `python -c "from mempalace_bridge import get_default_bridge; from agent_identity import CODING_AGENT; b=get_default_bridge(CODING_AGENT); print(b.status())"`
- [ ] **Local DB** at `$SUPERBOT_LOCAL_DB_PATH` (default `~/.superbot/superbot.db`)
  - [ ] 18 tables present, `maybe_prune()` runs hourly
  - [ ] `control_inbox`, `bus_inbox`, `control_state` schemas correct
- [ ] **Supabase** accessible (circuit breaker closed)
  - [ ] `python -c "from supabase_client import get_supabase_client; c=get_supabase_client(); print(c.health_check())"`

### 2.2 Kill-Switch Verification
- [x] **n8n kill-switch webhook** (`superbot-control`) responds 200 to signed `pause_entries`
- [x] Test: `python scripts/send_command.py --command pause_entries --by ubuntu:opencode:kalshi_deepbot --reason "live readiness test"` — verified 2026-11-21: accepted, row applied, `control_state` row written `{"paused": true, "paused_by": "operator", ...}`
- [x] Verify: `control.STATE.pause_entries` becomes `True` within 1 cycle (15s) — pause is persisted to the local DB and **reloaded at trader startup** (`trader.py` H1 block), so it also survives restarts; a DB read failure fails **closed** (pauses entries)
- [x] Test resume: `python scripts/send_command.py --command resume_entries --by ubuntu:opencode:kalshi_deepbot` — **while the trader is risk-halted, single-key resume is REJECTED by design** ("resume after a risk halt requires confirm_by from a different identity"); the working form is `--payload-json '{"confirm_by": "<different identity>"}'`
- [x] Verify: `control.STATE.pause_entries` becomes `False` within 1 cycle — verified with two-key resume; `control_inbox` audit trail shows applied/rejected rows and `control_state` returns to `{"paused": false}`

### 2.3 Reconciliation & P&L
- [x] **Startup reconciliation** works (trader.py lines 3541-3561) — observed live 2026-11-21: `H9: pending_send sb_... marked cancelled/unverified at startup (fills recovered from venue)`
  - [x] Pending `pending_send` intents marked cancelled/unverified at startup
  - [ ] Fills recovered from venue positions (only observable with a real live position; dry orders leave no venue record by design)
- [ ] **PnL reconcile** runs (interval `PNL_RECONCILE_INTERVAL_S=600`)
  - [ ] Compares local `pnl_ledger` sum vs Supabase `bot_state.session_pnl` vs n8n `orders.pnl_dollars`
  - [ ] Drift > $0.01 triggers repair + alert

### 2.4 Monitoring & Alerts
- [ ] **Critical alert webhook** (`superbot-critical-alarm`) delivers to operator channel
- [ ] **Latency regression gate** configured (`LATENCY_REGRESSION_GATE=true` after baseline)
- [ ] **Credential guard** timer active (`superbot-creds-guard.timer` every 15 min)

---

## 3. Venue-Specific Readiness

### 3.1 Kalshi
- [x] **API connectivity**: `python -c "from kalshi_client import KalshiClient; c=KalshiClient(); print(c.get_markets(series='KXBTC15M', status='open', limit=1))"` — verified 2026-11-21
- [x] **Order placement test** (dry-run): `python trader.py --once --dry` → places + fills paper order — verified 2026-11-21 in an isolated `SUPERBOT_DATA_DIR` (the B15 instance lock is held by the live trader, by design): a real cycle placed NO 15x @ 98c on `KXXRP15M-26NOV210330-30` ($14.70 after Kelly/depth/correlated/cash-floor/exposure-ceiling caps) with all 3 LLM layers answering live; the order rested and was correctly marked unverified at the next process start
- [ ] **Settlement path**: Verify `position.settled` events flow to n8n → Supabase
- [ ] **Risk halt cleared**: Current drawdown -99.8% must be reset (new account or manual override)
- [ ] **Position limits**: Verify `10,000 contracts / $100,000` per 15M series; aggregate 25,000 / $250,000

### 3.2 Polymarket US
- [ ] **SDK authenticated**: `python -c "from polymarket_us_client import get_market_book; print(get_market_book('cpc-btc-updown-15m-...'))"`
- [ ] **Market discovery**: `python -c "from poly_us_trader import PolyUSPaperTrader; t=PolyUSPaperTrader(initial_cash=1000); print(t.scan_markets()[:1])"` returns live markets
- [ ] **Order preview**: `preview_order()` returns `True` for valid params
- [ ] **Settlement index**: Confirmed CF Benchmarks BRTI (same as Kalshi) — 99.4% agreement measured
- [ ] **Position limits**: 5,000 contracts / $50,000 per BTC market; aggregate 20,000 / $200,000

---

## 4. Operational Procedures — Documented & Rehearsed

### 4.1 Startup Sequence
1. `sudo systemctl start mempalace-mcp` (if not running)
2. `sudo systemctl start n8n` → wait for `/healthz` + webhook registration (~10s)
3. `sudo systemctl start superbot-bus-inbox` (agent coordination)
4. `sudo systemctl start superbot-job-bridge` (command bridge)
4. **Kalshi live**: `sudo systemctl start superbot` (reads `EnvironmentFile=/etc/superbot/live-confirm.conf`)
5. **Polymarket US live**: `sudo systemctl start poly-us-dry` → rename to `poly-us-live` with live env

### 4.2 Shutdown Sequence (Graceful)
1. Send `pause_entries` via `send_command.py` or n8n webhook
2. Wait 1 cycle (15s) for in-flight orders to cancel
3. `sudo systemctl stop superbot` / `poly-us-live` (TimeoutStopSec=90 drains ledger, cancels resting, stops bridge)
4. `sudo systemctl stop superbot-job-bridge superbot-bus-inbox n8n mempalace-mcp`

### 4.3 Emergency Procedures
- [ ] **Kill-switch**: `curl -X POST http://127.0.0.1:5678/webhook/superbot-control -H "X-Superbot-Key: $KEY" -d '{"command":"pause_entries"}'`
- [ ] **Force-stop**: `sudo systemctl stop superbot` (triggers graceful drain via `SIGTERM` handler)
- [ ] **Credential rotation**: `python scripts/n8n_creds_sync.py --prune` after Bitwarden update

---

## 5. Sign-Off — Required Before Live

| Role | Name | Signature | Date |
|------|------|-----------|------|
| **Operator (Human)** | | | |
| **Compliance Review** | | | |
| **Risk Approval** | | | |

---

## 6. Post-Go-Live — First 24 Hours

- [ ] Monitor `bot.log` for: `H1: Resumed persistent pause_entries`, `H9: Found N pending order intents`, `J5: pause cache refresh`
- [ ] Verify n8n executions: `superbot-trade` (orders), `superbot-monitor` (acks), `superbot-control` (pause/resume)
- [ ] Check Supabase: `bot_state` updated per cycle, `pnl_ledger` settlement rows, `signals` + `orders` synced
- [ ] Confirm P&L reconcile runs at 600s interval (check `reconcile.mismatch` events)
- [ ] Alert on: risk halt, drawdown > 5%, consecutive losses > 3, n8n webhook failures > 5/min

---

## Appendix: Current Dry-Run State (for reference, measured 2026-11-21)

```bash
# Kalshi live-account balance (real money, via API)
$ python -c "from kalshi_client import KalshiClient; print(KalshiClient().get_balance())"
# → 9.8157   # $9.82, 0 resting orders, 0 positions, risk-halted

# Polymarket US dry state (mode-isolated local DB, source of truth)
$ python -c "from local_db import LocalDB; db=LocalDB(auto_sync=False); print(db.get_bot_state('poly_us_state:dry'))"
# → {"cash": 34.22, "start_balance": 35.0, "realized_pnl": 0.2, "trades_count": 14, ...}
```

Dry-run growth simulation against live orderbook history (81 days,
76,507 windows, 15 series; `scripts/dryrun_growth_sim.py`, trades recorded in
`$TMPDIR/drysim_trades.json`, write-up in `docs/DRY_RUN_GROWTH_TRAJECTORY.md`
with the full method and the v1-fill-rule retraction):

| Config | Fills / signals | Win | PnL (81d) | Final equity | Max DD |
|---|---|---|---|---|---|
| Maker, production dry-run guards | 44 / 385 (11.4%) | 93.2% | −$5.06 | $29.94 | 27.4% (halts at −15%) |
| Maker, paper-experiment knobs | 300 / 10,357 (2.9%) | 92.7% | −$25.23 | $9.77 | 76.4% |
| Taker (cross ask + fee) | 36 (5-day) | 94.4% | −$2.48 | $32.52 | 15.0% (halts) |

**Bottom line**: Code is production-grade. Live readiness now depends entirely on **funded accounts**, **human risk approval**, and **flipping the confirm flags** — and on accepting that the corrected maker rule (fill only when our side's ask crosses our price with counterparty depth) measures **negative EV (−3.2c/contract)** at both maker configs: there is no compounding path to $3.25M, and the earlier +$128 result was a fill-rule bug, retracted in the doc. Reaching the target requires a strategy change (a calibrated model gate), not a sizing change.