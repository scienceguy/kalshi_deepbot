# TASKS RECONCILIATION v10 — kalshi_deepbot

Generated from git history and AGENT_TASKS v9/v10 comparison.
Date: 2026-11-19

## Legend
| Status | Meaning |
|--------|---------|
| ✅ APPLIED | Implemented and committed |
| ❌ OBSOLETE | Superseded or no longer relevant |
| 🔄 OPEN | Not yet implemented, carried forward |

---

## v9 Tasks (AGENT_TASKS_v9.md)

| Task | Status | Evidence | Action |
|------|--------|----------|--------|
| J12 — Stop tracking rotated logs/snapshots | ✅ APPLIED | Commit 2552f9a (J12: Stop tracking rotated logs/snapshots; add large file guard), then enhanced in 5545e86 (pre-commit guard) | Done |
| J1 — Venue-side order expiry (GTD) | ✅ APPLIED | polymarket_us_client.py: preview_order/place_maker_order support good_till_time, GTD with fallback flag | Done |
| J2 — Private order/position stream | ✅ APPLIED | Commit 699cf59 (J2: poly_us_trader use stream for reconciliation), poly_us_stream.py exposes get_order(), set_order_callback() | Done |
| J7 — Harden job bridge | ✅ APPLIED | Commit 374ce6d/5545e86: HMAC on all routes, Host validation, body size limit, per-route semaphore/cooldown, BASE_DIR anchored | Done |
| J3 — Write-ahead order intent (Polymarket) | ✅ APPLIED | Commit 374ce6d (pending_send table), poly_us_trader.py record_order_intent() + startup reconciliation | Done |
| J8 — Scheduled jobs outbox/de-dup | ✅ APPLIED | Commits 265eda1, 240544f, ca209cc: emit_event via durable outbox, alert_history de-dup, correct paths | Done |
| J5 — DB reads out of guard tick | ✅ APPLIED | Commit 5545e86: cached pause_entries with 250ms background refresher | Done |
| J4 — Validate fee model vs venue fields | ✅ APPLIED | Commit 5545e86: preview_order captures commissionsBasisPoints/makerCommissionsBasisPoints | Done |
| J10 — Per-identity keys, real 2-person rule | 🔄 OPEN | control.py still uses confirm_by string, single HMAC secret | Carry to v10 |
| J6 — Event-driven guard wake-ups + latency baseline | 🔄 OPEN | RestingGuard is 1s timer; no A1 latency report | Carry to v10 |
| J9 — Auxiliary services from release dir | 🔄 OPEN | superbot-bus-inbox, job-bridge, jobs, creds-guard still use dev tree | Carry to v10 |
| J15 — Operator verification checklist | 🔄 OPEN | Not yet done | Carry to v10 |
| J13 — CI supply-chain hardening | 🔄 OPEN | Not yet done | Carry to v10 |
| J14 — Environment-dependent tests | 🔄 OPEN | systemd-analyze tests fail without /opt/superbot/.venv | Carry to v10 |
| J11 — Require gate key in production | 🔄 OPEN | SUPERBOT_GATE_KEY added but SUPERBOT_REQUIRE_GATE_KEY not enforced | Carry to v10 |
| K1 — Kalshi WebSocket market data/fills | 🔄 OPEN | Not yet implemented | Carry to v10 |

---

## v10 P0 Tasks (AGENT_TASKSv10.md)

| Task | Status | Evidence | Action |
|------|--------|----------|--------|
| P0-1 — Reconcile earlier rounds | ✅ APPLIED | This document | Done |
| P0-2 — Repo visibility & secret hygiene | 🔄 OPEN | gitleaks: no leaks found; logs/agent-autopilot/ checked - no tokens; gh CLI unavailable for visibility check | Need human to check visibility and rotate gateway key |
| P0-3 — Review packet for 304d8f8 (Kalshi NO-side floor) | 🔄 OPEN | Commit 304d8f8 not found in history; need to locate the fix | Need to find and document |
| P0-4 — Audits fail loudly | 🔄 OPEN | ponytail-audit sub-agents died on signup gate | Need wrapper script |
| P0-5 — Termux -> OCI migration | 🔄 OPEN | README still documents termux-job-scheduler; ml_python proot wrapper exists; systemd units partial | Need systemd timers, native xgboost/lightgbm, rewrite README |

---

## Summary

**Completed (15):** J12, J1, J2, J7, J3, J8, J5, J4, plus A2, A3, A4, F3 in latest commit

**Carried to v10 (10):** J10, J6, J9, J15, J13, J14, J11, K1, P0-3, P0-4, P0-5

**Notes:**
- The "10 untracked planning .md files" (CF-4) appear to all be tracked in docs/ now (35 files)
- gitleaks found no secrets in current or history
- Local gateway key rotation still needed (was in HANDOFF.md history)
- Repo visibility unknown (gh CLI not installed)

---

## Next Actions (Priority Order)

1. **P0-2**: Human to check `gh repo view --json visibility`, rotate gateway key
2. **P0-3**: Locate 304d8f8 fix, produce review packet
3. **P0-4**: Write audit wrapper that fails on non-zero exit/empty output
4. **P0-5**: Create systemd timers for retrain, install native xgboost/lightgbm, rewrite README
5. **A1**: Latency budget + regression gate (measure first)
6. **B1**: One transactional local store (SQLite WAL) as source of truth
7. **J10**: Per-identity keys for control lane
8. **J6**: Event-driven guard + measured latency baseline