# Ponytail Review — Nemotron Ultra (nvidia/nvidia/nemotron-3-ultra-550b-a55b)
**Date:** 2026-11-14
**Provider:** NVIDIA API Direct
**Scope:** Review of recent settlement race fixes, SignalEngine OOP wrapper, and MTM exposure tracking in `kalshi_deepbot`.

---

## What this change does:
Introduces settlement-race fixes in mark-to-market exposure (cleanly distinguishing open unexpired positions from closed/settled positions to avoid double-counting with cash payout), adds a tested `SignalEngine` class wrapper for callers expecting an OOP interface, and raises log capacity to 1GB per file with 3 backups. In `risk.py`, adds conditional clearance for false drawdown halts caused by transient settlement spikes.

---

### Findings addressed:
1. **`signals.py` `SignalEngine.evaluate` parameter alignment**:
   - The initial wrapper accepted `max_contracts=0`, whereas `signals.evaluate(market, cash, max_trade_override)` does not take `max_contracts`. Caught by integration test and aligned immediately in `b0e3055`.
2. **`trader.py` `_mark_to_market_exposure`**:
   - Accurately checks whether market is closed/settled before deciding whether to retain cost basis exposure.
3. **`risk.py` false drawdown prevention**:
   - Prevents artificial drawdown halts when cash exceeds start balance with 0 open positions.

---

## Verification:
- 17 unit tests in `tests/test_signals.py` passing.
- 71 unit tests in `tests/test_trader.py` passing.
- 79 unit tests in `tests/test_risk.py` passing.
- Live exchange balance compounded from $1.33 to $1.50 to $1.55 on Kalshi exchange shard 2.
