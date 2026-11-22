# T16 trader wiring review

Verdict: CHANGES REQUIRED

What this change does: the uncommitted `trader.py` diff wires kill-switch, session-limit, quote-freshness, and latency objects into `cycle()` and lets dry `--once` bypass the instance lock. This review covers the current diff, called implementations, and relevant tests. It assumes one authoritative trader per data directory.

Snapshot: `trader.py` SHA256 `740e100372a757612a73a22ad73f6151b6b12acfc6f22bbc0c90525e90180e60`; diff SHA256 `f9305147d782e5f30f7b397fc6389c2990b95c2ec25e205bcb60a3bf310e80bd`.

## Must fix

1. **Dry `--once` is not read-only, so the lock bypass is unsafe** (`trader.py:3471-3476`)
   - `_run_loop()` calls `_replay_settlement_queue(client)` before the `--once` branch (`trader.py:3912-3921`). Replay writes P&L, paper state, bot state, orders, webhooks, and the queue (`trader.py:1321-1405`). Cycles also write paper positions, order intents, fills, and latency JSONL (`trader.py:2670-2674,3151-3167,520-579`; `latency.py:55-57,63-69`).
   - The queue explicitly relies on the instance lock to serialize load-mutate-save (`trader.py:952-959`). Bypassing it permits concurrent replay and lost queue updates.
   - Kalshi order/sell/cancel methods do suppress venue writes in dry mode (`kalshi_client.py:742-746,796-800,805-809`), but local and outbound writes still occur.
   - **Smallest fix:** Restore exclusive locking for every `_run_loop()` call. Remove the unused `shared` option; `LOCK_SH` does not coexist with a running `LOCK_EX` holder despite its docstring (`trader.py:4130-4153`). A genuinely read-only probe must not call `_run_loop()` or cycle/replay code.

2. **The new kill switch has no durable control authority** (`trader.py:142,2030`)
   - `KillSwitch` starts in-memory as `ARMED` (`kill_switch.py:48-63`). No production code calls this object's `trip()` or `reset()`. Signed control instead changes `control.STATE.pause_entries` (`control.py:175-212`). Startup restores that pause and `state.risk`, not this switch (`trader.py:3577-3592,3867-3878`).
   - Thus the new check never trips in production. If an in-process caller trips it, restart silently re-arms trading, violating the durable halt requirement in T16-42.
   - **Smallest fix:** Prefer the existing persisted control/risk gates and remove this parallel switch. Otherwise connect authenticated trip/reset to durable local DB state and restore it at startup. Test trip, two-person resume, and restart through the real wiring.

3. **A halted kill switch stops management and monitoring, not just entries** (`trader.py:2029-2034`)
   - The return precedes control drain (`trader.py:2036-2045`), settlement/exits (`trader.py:2370-2700`), cycle-state reporting (`trader.py:2851-2853`), and the cycle watchdog (`trader.py:3365-3366`). The comment claiming cycle state is still emitted has no such call.
   - With an open position, a halt skips settlement and exits. Resting-order maintenance stops, and queued signed status/resume commands are not drained. This contradicts `kill_switch.py:72-76`.
   - **Smallest fix:** Move the halt check beside the existing hard stops after maintenance/reporting (`trader.py:2855-2864`). Test that a halted cycle still manages existing exposure and processes control/reporting but cannot enter.

4. **All three session limits are inert and fail open across restarts** (`trader.py:143,3090-3102`)
   - Counters start at zero (`session_limits.py:32-37`). No trader code calls `record_trade`, `record_market_loss`, `record_market_win`, or `reset_daily`. Existing fills/outcomes update only `state.risk` (`trader.py:520-579,1485-1510,2488-2491`), and UTC rollover resets only `state.risk` (`trader.py:2765-2770`).
   - Daily loss, daily trades, and consecutive-loss checks therefore stay false. Even if another caller mutates the object, restart clears it and the next UTC day does not reset daily counters.
   - **Smallest fix:** Do not maintain a second divergent risk authority: use persisted risk state and order/ledger records. If retained, update at every fill/settlement/replay/exit point, persist and reload, and reset at UTC rollover. Add actual-cycle and restart tests for each limit.

5. **Quote freshness refreshes a quote immediately before judging it** (`trader.py:2933-2941`)
   - `update()` defaults the timestamp to now and overwrites the old quote (`quote_freshness.py:28-31`). `is_fresh()` then measures age from that new timestamp (`quote_freshness.py:33-38`). A market with no price is appended without a freshness decision (`trader.py:2941`). `MarketInfo` carries no quote-receipt timestamp (`market_finder.py:60-68`), and orderbook failure can fall back to summary prices (`market_finder.py:178-187,235-244`).
   - A dead or repeatedly reused quote is stamped new and passes; missing quotes fail open. A focused stale-quote probe printed `stale_quote_after_trader_style_update_is_fresh=True`.
   - **Smallest fix:** Propagate a real successful book/venue timestamp with each market. Check it before recording a new observation, reject missing/stale timestamps, and update only on a genuinely new quote. Add a controlled-clock cycle test proving an old quote remains blocked on later cycles.

3. **A halted kill switch stops position management and control handling, not just entries** (`trader.py:2029-2034`)
   - The return precedes control drain (`trader.py:2036-2045`), settlement and exit management (`trader.py:2370-2700`), cycle-state reporting (`trader.py:2851-2853`), and the cycle watchdog (`trader.py:3365-3366`). The comment claiming cycle state is still emitted has no such call.
   - With an open position, a halt skips settlement and exits. Resting-order maintenance stops, and queued signed status/resume commands are not drained. This contradicts `kill_switch.py:72-76`, which says monitoring continues.
   - **Smallest fix:** Move the halt check beside `control.STATE.pause_entries` in the existing hard-stop section after maintenance/reporting (`trader.py:2855-2864`). Test that a halted cycle still manages existing exposure and processes control/reporting but cannot enter.

4. **All three session limits are inert and fail open across restarts** (`trader.py:143,3090-3102`)
   - Counters start at zero (`session_limits.py:32-37`). No trader code calls `record_trade`, `record_market_loss`, `record_market_win`, or `reset_daily`. Existing fills/outcomes update only `state.risk` (`trader.py:520-579,1485-1510,2488-2491`), and UTC rollover resets only `state.risk` (`trader.py:2765-2770`).
   - Daily loss, daily trades, and consecutive-loss checks therefore remain false. Even if another caller mutates the object, restart clears every counter and the next UTC day does not reset daily counters.
   - **Smallest fix:** Use the existing persisted risk state and order/ledger records instead of a second authority. If retained, update at every authoritative fill/settlement/replay/exit point, persist and reload, and reset daily counters at UTC rollover. Add actual-cycle and restart tests for each limit.

6. **Exceptions bypass latency-span cleanup** (`trader.py:2025-2027`)
   - The span is manually entered/exited with no `with` or `finally`. Discovery can raise at `trader.py:2091`, and `_run_loop()` catches outside `cycle()` (`trader.py:3991,4024-4026`). `LatencySpan` already provides context-manager cleanup (`latency.py:22-28`).
   - An exceptional cycle records no span. Forcing discovery to raise produced `exception_path_recorded_spans=0`. All 12 explicit normal returns currently do have an immediately preceding exit; the defect is exceptional exits and future return-path fragility.
   - **Smallest fix:** Make `cycle()` a thin wrapper around the current implementation using `with _latency.span(...):`, and delete all manual enter/exit calls. Test that an exception after entry records exactly one span.

## Should fix

7. **`tick_to_submit` measures housekeeping and no-order cycles, not tick-to-submit** (`trader.py:2025-2027,2855-3102,3365`)
   - The span includes control, discovery, settlement, exits, DB work, webhooks, and LLM evaluation. It also records risk, cash, no-market, no-signal, and session-limit returns where no submit occurred. `LatencyRecorder.budget_breaches()` consumes this stage for the 500 ms/1 s budgets (`latency.py:18-20,98-102`).
   - Slow settlement with no order creates a false submit-latency breach; fast no-signal cycles bias percentiles downward. These samples cannot validate T16-64.
   - **Smallest fix:** Record the whole-cycle wrapper as `cycle`. Record `tick_to_submit` only from a real tick/book receipt to each order submission, with no sample on no-order paths. Test zero submit samples for no-signal and one for one order.

8. **An expired session cooldown still blocks the next eligible cycle** (`trader.py:3098-3102`)
   - `consecutive_loss_exceeded()` returns true from the streak (`session_limits.py:67-75`). Only afterward does `cooldown_remaining()` clear an expired cooldown and return zero (`session_limits.py:77-90`); the caller still returns unconditionally. The probe printed `expired_cooldown_branch_entered=True remaining=0.0 streak_after=0`.
   - The first otherwise-eligible cycle after expiry is rejected even though the cooldown was just cleared.
   - **Smallest fix:** After arming/checking the streak, read `remaining` and return only when `remaining > 0`. Add a trader-level expiry regression, not only a `SessionLimits` unit test.

5. **Quote freshness always refreshes a quote immediately before judging it** (`trader.py:2933-2941`)
   - `update()` defaults the timestamp to the current clock and overwrites the old quote (`quote_freshness.py:28-31`). `is_fresh()` then measures age from that new timestamp (`quote_freshness.py:33-38`). A market with no price is appended without any freshness decision (`trader.py:2941`). `MarketInfo` carries no quote-receipt timestamp (`market_finder.py:60-68`), and orderbook failure can fall back to summary prices (`market_finder.py:178-187,235-244`).
   - A dead or repeatedly reused quote is stamped as new and passes. The configured age cannot detect the claimed dead-feed condition; missing quotes fail open too.
   - **Smallest fix:** Propagate a real successful book/venue timestamp with each market. Check that timestamp before recording a new observation, reject missing or stale timestamps, and update only on a genuinely new quote. Add a controlled-clock cycle test where the same old quote remains blocked on later cycles.

6. **Exceptions bypass latency-span cleanup** (`trader.py:2025-2027`)
   - The span is manually entered and exited at normal exits, with no `with` or `finally`. Discovery can raise at `trader.py:2091`, and `_run_loop()` catches outside `cycle()` (`trader.py:3991,4024-4026`). `LatencySpan` already supplies exception-safe context-manager exit behavior (`latency.py:22-28`).
   - An exceptional cycle records no span. Forcing discovery to raise produced `exception_path_recorded_spans=0`. All 12 explicit normal returns currently do have an immediately preceding exit; the defect is exceptional exits and future return-path fragility.
   - **Smallest fix:** Make `cycle()` a thin wrapper around the current implementation using `with _latency.span(...):`, and delete all manual enter/exit calls. Add a test that raises after span entry and asserts exactly one recorded span.

5. **Quote freshness refreshes a quote immediately before judging it** (`trader.py:2933-2941`)
   - `update()` defaults the timestamp to now and overwrites the old quote (`quote_freshness.py:28-31`). `is_fresh()` then measures age from that new timestamp (`quote_freshness.py:33-38`). A market with no price is appended without a freshness decision (`trader.py:2941`). `MarketInfo` carries no quote-receipt timestamp (`market_finder.py:60-68`), and orderbook failure can fall back to summary prices (`market_finder.py:178-187,235-244`).
   - A dead or repeatedly reused quote is stamped new and passes; missing quotes fail open. A focused stale-quote probe printed `stale_quote_after_trader_style_update_is_fresh=True`.
   - **Smallest fix:** Propagate a real successful book/venue timestamp with each market. Check it before recording a new observation, reject missing or stale timestamps, and update only on a genuinely new quote. Add a controlled-clock cycle test proving an old quote remains blocked on later cycles.

6. **Exceptions bypass latency-span cleanup** (`trader.py:2025-2027`)
   - The span is manually entered and exited with no `with` or `finally`. Discovery can raise at `trader.py:2091`, and `_run_loop()` catches outside `cycle()` (`trader.py:3991,4024-4026`). `LatencySpan` already provides context-manager cleanup (`latency.py:22-28`).
   - An exceptional cycle records no span. Forcing discovery to raise produced `exception_path_recorded_spans=0`. All 12 explicit normal returns currently do have an immediately preceding exit; the defect is exceptional exits and future return-path fragility.
   - **Smallest fix:** Make `cycle()` a thin wrapper around the current implementation using `with _latency.span(...):`, and delete all manual enter/exit calls. Test that an exception after entry records exactly one span.

## Should fix

7. **`tick_to_submit` samples measure housekeeping and no-order cycles** (`trader.py:2025-2027,2855-3102,3365`)
   - The span includes control, discovery, settlement, exits, DB work, webhooks, and LLM evaluation. It also records risk, cash, no-market, no-signal, and session-limit returns where no submit occurred. `LatencyRecorder.budget_breaches()` consumes this stage for the 500 ms/1 s budgets (`latency.py:18-20,98-102`).
   - Slow settlement with no order creates a false submit-latency breach; fast no-signal cycles bias percentiles downward. These samples cannot validate T16-64.
   - **Smallest fix:** Record the whole-cycle wrapper as `cycle`. Record `tick_to_submit` only from a real tick/book receipt to each order submission, with no sample on no-order paths. Test zero submit samples for a no-signal cycle and one for a one-order cycle.

8. **An expired session cooldown still blocks the next eligible cycle** (`trader.py:3098-3102`)
   - `consecutive_loss_exceeded()` returns true from the streak (`session_limits.py:67-75`). Only afterward does `cooldown_remaining()` clear an expired cooldown and return zero (`session_limits.py:77-90`); the caller still returns unconditionally. The focused probe printed `expired_cooldown_branch_entered=True remaining=0.0 streak_after=0`.
   - The first otherwise-eligible cycle after expiry is rejected even though the cooldown state has just been cleared.
   - **Smallest fix:** After arming/checking the streak, read `remaining` and return only when `remaining > 0`. Add a trader-level expiry regression, not only a `SessionLimits` unit test.

## Test and verification evidence

- Relevant unit tests exercise helpers, not this wiring: `tests/test_kill_switch.py:16-99`, `tests/test_session_limits.py:6-111`, `tests/test_quote_freshness.py:17-78`, and `tests/test_latency.py:32-322`. `tests/test_h8_h9_trader_wiring.py:199-219` checks only older DB latency rows. No test references the new trader globals or the dry-once lock bypass.
- Focused existing tests: 56 passed in 2.07 s (`test_kill_switch`, `test_session_limits`, `test_quote_freshness`, `test_latency`, `test_h8_h9_trader_wiring`, `test_b15_instance_lock`, and `test_instance_lock`). These green tests do not cover the defects above.
- Focused probes also printed `stale_quote_after_trader_style_update_is_fresh=True`; exception and cooldown probe results are recorded above.
- No full suite, live run, order, credential change, source/test/config edit, commit, push, or trading-process change was performed. The only project-tree file written by this reviewer is this report.
- Required final source and ASCII verification results follow below.

