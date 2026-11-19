# Review Packet: Commit 304d8f8 — Kalshi NO-side Floor Fix

**Date:** 2026-11-19  
**Commit:** `304d8f8330279b7f8a75fc399ba90dd79ff12783`  
**Author:** scienceguy  
**Branch:** main (pre-history-rewrite; SHA valid in rewritten history)  
**Files Changed:** `gates.py`, `tests/test_gates.py`  

---

## Summary

**Bug:** Gate 3 (measured-q floor) applied `GATES_MEASURED_Q = 0.9928` (the realized win rate of a YES favourite priced 95–99c) to **both sides** using the raw entry price. Since Kalshi prices NO contracts directly (`no_bid`/`no_ask` are independent fields), a NO fill at 97c was incorrectly floored with the YES-side 99.28% win rate — even though a NO contract at 97c settles NO only ~3% of the time. Conversely, a NO fill at 3c (YES-equivalent 97c) was NOT floored because its raw price (3c) was outside the band.

**Impact:** Near-certain losers (NO @ 97c) were scored as near-certain winners; near-certain winners (NO @ 3c) were not boosted. This inverted the gate's intent on the NO side where a lost contract costs the most (short YES exposure).

**Fix:** Convert to YES-equivalent price first (`yes_equiv = entry_price_cents if side == "yes" else 100 - entry_price_cents`), test against band, floor `q` with measured value, and record `yes_equiv_price_cents` in metrics for auditability.

---

## Changes

### `gates.py` (lines ~387–405)

```python
# Before: used raw entry_price_cents for both sides
if entry_price_cents is not None:
    if (config.GATES_MEASURED_BAND_MIN_CENTS
            <= entry_price_cents <= config.GATES_MEASURED_BAND_MAX_CENTS):
        q = max(q, config.GATES_MEASURED_Q)
        metrics["q_measured_floor"] = True

# After: convert to YES-equivalent, record it, then floor
if entry_price_cents is not None:
    yes_equiv = (entry_price_cents if side == "yes"
                 else 100 - entry_price_cents)
    metrics["yes_equiv_price_cents"] = yes_equiv
    if (config.GATES_MEASURED_BAND_MIN_CENTS
            <= yes_equiv <= config.GATES_MEASURED_BAND_MAX_CENTS):
        q = max(q, config.GATES_MEASURED_Q)
        metrics["q_measured_floor"] = True
```

### `tests/test_gates.py` — New class `TestMeasuredFloorSide`

Three pinning tests ensure the logic cannot regress:

| Test | Side | Raw Price | YES-Equiv | Expected Floor |
|------|------|-----------|-----------|----------------|
| `test_yes_in_band_records_floor_flag` | YES | 97 | 97 | ✅ YES (band 95–99) |
| `test_no_at_97c_is_not_floored` | NO | 97 | 3 | ❌ NO (outside band) |
| `test_no_at_3c_maps_to_yes_97` | NO | 3 | 97 | ✅ YES (band 95–99) |

---

## Verification

```bash
# Run the specific test class
python -m pytest tests/test_gates.py::TestMeasuredFloorSide -v

# Full gate test suite
python -m pytest tests/test_gates.py -q
```

All tests pass (1312 passed, 10 skipped as of HEAD `1666ad7`).

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Regression of raw-price test on NO side | Low | High (inverted gate on NO) | Three explicit tests pinning YES-equivalent logic |
| Config drift (band moved, GATES_MEASURED_Q stale) | Medium | Medium | Measured Q is a constant; band in config; no dynamic fetch |
| Side parameter missing/empty | Low | Medium | `signals.py` validates `side != ""`; gate receives valid side |

---

## Audit Trail

- Introduced in commit `304d8f8` (Mon Nov 9 2026)
- Survived history rewrite on 2026-11-01 (new HEAD `ac0b91b`)
- Present in current `main` at HEAD `1666ad7`
- No subsequent modifications to this logic

---

## Reviewer Checklist

- [ ] Logic: YES-equivalent conversion is correct for both sides
- [ ] Band test uses `yes_equiv`, not raw `entry_price_cents`
- [ ] `yes_equiv_price_cents` recorded in metrics for audit trail
- [ ] Tests cover all three critical cases (YES in band, NO@97c, NO@3c)
- [ ] No other gate depends on raw price vs measured floor
- [ ] `GATES_MEASURED_Q` value (0.9928) matches measured win rate docstring

---

## Related Context

- `GATES_MEASURED_Q = 0.9928` — documented as "realized win rate of a YES favourite priced in [95, 99]c"
- Band: `GATES_MEASURED_BAND_MIN_CENTS=95`, `GATES_MEASURED_BAND_MAX_CENTS=99`
- Kalshi NO pricing: independent `no_bid`/`no_ask` fields (not complements of YES)
- `signals.py` sets NO entry as `no_ask - 1` (one tick below ask)