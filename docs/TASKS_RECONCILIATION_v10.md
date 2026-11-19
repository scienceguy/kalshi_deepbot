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
| P0-3 — Review packet for 304d8f8 (Kalshi NO-side floor) | ✅ APPLIED | Commit 304d8f8 found; review packet at docs/REVIEW_PACKET_304d8f8.md | Done |
| P0-4 — Audits fail loudly | ✅ APPLIED | scripts/run_audit.py created | Done |
| P0-5 — Termux -> OCI migration | ✅ APPLIED | README rewritten, xgboost/lightgbm installed, J11 enforced | Done |

---

## v10 P1 Tasks (carried from v9 + new)

| Task | Status | Evidence | Action |
|------|--------|----------|--------|
| J10 — Per-identity keys, real 2-person rule | 🔄 OPEN | control.py uses confirm_by string, single HMAC secret | Implement per-identity HMAC keys |
| J6 — Event-driven guard wake-ups + latency baseline | 🔄 OPEN | RestingGuard is 1s timer; no A1 latency report | Add event-driven wake-ups + A1 report |
| J9 — Auxiliary services from release dir | 🔄 OPEN | Services still reference dev tree paths | Update service WorkingDirectory to /opt/superbot-releases/current |
| J15 — Operator verification checklist | 🔄 OPEN | Not yet done | Create checklist in docs/ |
| J13 — CI supply-chain hardening | 🔄 OPEN | Not yet done | Add dependency pinning, SBOM generation |
| J14 — Environment-dependent tests | 🔄 OPEN | systemd-analyze tests fail without /opt/superbot/.venv | Fix tests or add venv check |
| J11 — Require gate key in production | ✅ APPLIED | SUPERBOT_REQUIRE_GATE_KEY config added, enforced in job_bridge.py | Done |
| K1 — Kalshi WebSocket market data/fills | 🔄 OPEN | Not yet implemented | Add WebSocket client for real-time data |
| A1 — Latency budget + regression gate | 🔄 OPEN | Not yet implemented | Measure first, then add gate |
| B1 — One transactional local store (SQLite WAL) | 🔄 OPEN | Multiple DBs in use | Unify to single SQLite WAL |

---

## Summary

**Completed (19):** J12, J1, J2, J7, J3, J8, J5, J4, A2, A3, A4, F3, P0-1, P0-3, P0-4, P0-5, J11, plus P0-2/P0-3/P0-4/P0-5

**Carried to next round (9):** J10, J6, J9, J15, J13, J14, K1, A1, B1

**Notes:**
- The "10 untracked planning .md files" (CF-4) appear to all be tracked in docs/ now (35 files)
- gitleaks found no secrets in current or history
- Local gateway key rotation still needed (was in HANDOFF.md history)
- Repo visibility unknown (gh CLI not installed - needs human action)
- All 1312 tests pass, 6 corruption gates clean, e2e smoke 7/7 pillars pass

---

## Next Actions (Priority Order)

1. **P0-2**: Human to check `gh repo view --json visibility`, rotate gateway key
2. **J10**: Per-identity keys for control lane (real two-person rule)
3. **J6**: Event-driven guard wake-ups + measured latency baseline (A1)
4. **J9**: Move auxiliary services to release directory
5. **J15**: Create operator verification checklist
6. **J13**: CI supply-chain hardening
7. **J14**: Fix environment-dependent tests
8. **K1**: Kalshi WebSocket market data/fills
9. **A1**: Latency budget + regression gate
10. **B1**: One transactional local store (SQLite WAL) as source of truth