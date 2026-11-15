# Comprehensive To-Do List — Kalshi UltraBot: $100 → $10,000 Compounding
**Goal:** Reach $10,000 paper balance with continuous compounding (no decreases)
**Current State:** $125 paper balance (+25% from settled trades)
**Method:** Continuous development loop using ponytail-audit + ponytail-review skills
**Status:** Active — Started 2026-11-09

---

## Phase 1: Must Fix (Critical — Data Loss / Bugs) ✅ COMPLETE
- [x] Settlement queue PnL data loss - Already fixed
- [x] Bid history 30s save interval - Already fixed (5s)
- [x] Schema warned flag - Already fixed
- [x] FAVOURITE confidence calculation - **FIXED** (signals.py:401)

---

## Phase 2: Should Fix (Risk / Missing Tests / Duplication) ✅ COMPLETE
- [x] Integration tests for gates - **ADDED** (tests/test_gates.py: 16 tests)
- [x] Integration tests for llm_eval - **ADDED** (tests/test_llm_eval.py: 13 tests)
- [x] Integration tests for paper_mode - **ADDED** (tests/test_paper_mode.py: 13 tests)
- [x] Integration tests for market_finder - **ADDED** (tests/test_market_finder.py: 13 tests)
- [x] Dead code removal - **COMPLETED** (moved to legacy/)
- [ ] Fee math deduplication - Acceptable (delegates to fees.py)
- [ ] Config validation auto-call - Acceptable (called in trader.py)
- [ ] Supabase circuit breaker 4xx - Acceptable
- [ ] Stale order cleanup O(n) - Acceptable
- [ ] Bid history reload - Acceptable
- [ ] Paper mode two sources - Acceptable (local_db is backup)

---

## Phase 3: Nice to Have (Optimization / Lean)
- [ ] ThreadPoolExecutor workers configurable
- [ ] Request gap lower (0.2s → 0.1s)
- [ ] Similar dataclasses consolidation
- [ ] ATR hurdle constants repetitive → dict
- [ ] Momentum function module-level → model.py
- [ ] Split trader.py into modules

---

## Phase 4: Operational / Compounding to $10,000 🎯 **ACTIVE**
- [ ] **4.1** Verify current compounding logic is correct
- [ ] **4.2** Run extended dry-run (100+ cycles) to measure compounding rate
- [ ] **4.3** Optimize parameters for more trade frequency:
    - [ ] Adjust ENTRY_WINDOW_START_MIN/END_MIN for wider window
    - [ ] Adjust ENTRY_BAND_MIN/MAX for more opportunities
    - [ ] Verify MODEL_MIN_EDGE_CENTS allows sufficient trades
    - [ ] Check GATES_SHADOW_MODE allows trades through
- [ ] **4.4** Run continuous dry-run until $10,000 reached
- [ ] **4.5** Monitor paper balance every cycle, ensure no decreases
- [ ] **4.6** Log compounding progress to MemPalace

---

## Phase 5: Verification & Commit
- [ ] All tests pass (252+)
- [ ] Paper balance ≥ $10,000 with continuous compounding
- [ ] Git history clean, all changes committed
- [ ] Final verification with ponytail-review

---

## MemPalace Coordination
- `wing: ultrabot, room: todo, drawer: phase4` — Compounding tasks
- `wing: ultrabot, room: decisions` — Key parameter decisions
- `wing: ultrabot, room: status` — Balance milestones
- `correlation_id: task_compound_<n>` — Task tracking

---

**Last Updated:** 2026-11-09
**Next Action:** Phase 4.1 — Verify compounding logic, then run extended dry-run