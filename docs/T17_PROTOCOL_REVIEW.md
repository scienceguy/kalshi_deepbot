# T17-50 Protocol Review

## Scope and verdict

Reviewed `docs/EVAL_PROTOCOL.md` against `AGENT_TASKSv17.md` T17-50, T17-08, R8, and the v17 execution directives at HEAD `e692c94`. The protocol worktree file matches HEAD (SHA-256 `3752e73827dee9161a14f85aa02bdc0bd592dd86cdcac6efab83907a2c1b4157`). Fee implementations were read only to check protocol fee semantics. No NO-only/model study result was opened or run, and no orders, live calls, balances, secrets, processes, or git state were touched.

**Overall: FAIL.** T17-50 remains **PARTIAL**. The document has a useful forecast skeleton, but it does not pre-register the required NO-only study, R8-compliant fills, multiplicity-adjusted promotion inference, or all hard risk/sample gates. It is not ready to authorize T17-08 promotion or live trading.

## Findings

### 1. Fixed metrics, windows, and latency model: PASS (document completeness only)

- The metric set is fixed at `docs/EVAL_PROTOCOL.md:16-35`; train/validation/test windows and a final never-tuned test are at `docs/EVAL_PROTOCOL.md:39-50`.
- Latency stages, p95/p99 budgets, and measurement source are at `docs/EVAL_PROTOCOL.md:94-107`.
- This PASS concerns whether the protocol states these items, not whether production evidence meets them. Holdout contamination and data-sufficiency problems are below.

### 2. Net-of-fee EV clustered by market: PARTIAL

- Net EV after fees/fills is a secondary metric (`docs/EVAL_PROTOCOL.md:28-35`), market is the cluster unit (`docs/EVAL_PROTOCOL.md:48-50`), and promotion requires a positive clustered lower bound (`docs/EVAL_PROTOCOL.md:176-186`). This matches the core rule at `AGENT_TASKSv17.md:639-644` and `AGENT_TASKSv17.md:817-824`.
- Missing: confidence level, one-/two-sided convention, cluster estimator/bootstrap, seed/repetitions, minimum cluster count, and treatment of multiple observations from one market. "Clustered lower bound" is therefore not reproducible. It is also not explicitly NO-only or multiplicity-adjusted.
- Concrete failure: two analysts can both claim the line-180 rule passed while using different quote-level and market-level intervals.

### 3. NO-price/minutes/asset/venue segmentation and study policy: FAIL

- T17-08 requires NO-only net EV by NO price band, minutes to expiry, underlying, venue, and taker/maker, using the realized-vol-scaled gate, with YES-only beside it (`AGENT_TASKSv17.md:639-643`). The strategy directive also limits trading to positive-lower-bound regions (`AGENT_TASKSv17.md:39-45`).
- The protocol only says "per venue, per model family" (`docs/EVAL_PROTOCOL.md:18-26`) and has no fixed NO-price or minutes bins, underlying segmentation, NO-only/YES-only comparison, or named realized-vol gate. Its general model list (`docs/EVAL_PROTOCOL.md:150-162`) does not fill that gap.
- Bin edges and observation-time definitions are not fixed before unblinding. A later study could choose favorable cells after seeing results.

### 4. Held-out real production data: PARTIAL

- A held-out final period is explicit (`docs/EVAL_PROTOCOL.md:41-50`) and the sources are described as live APIs/feeds (`docs/EVAL_PROTOCOL.md:134-146`).
- However, the protocol never adopts R8's production-only, no-synthetic, no-interpolation, missing-data-means-no-fill rule (`AGENT_TASKSv17.md:131-139`). It has no immutable dataset manifest with calendar boundaries, production provenance, row counts, and an actual data hash; the hash fields are shell placeholders (`docs/EVAL_PROTOCOL.md:220-221`).
- Retention is only "> 3 days" for venue books/BBO and "> 60 days" for spot (`docs/EVAL_PROTOCOL.md:136-144`), while the stated windows span 88 days. The pre-run "> 60 days" check (`docs/EVAL_PROTOCOL.md:192-196`) does not establish complete book coverage for days 1-88.
- Maker fill probability is "updated daily" from a holdout (`docs/EVAL_PROTOCOL.md:79-85`). If this is the final holdout, it violates "NEVER used for tuning" at line 45. Calibration and final evaluation holdouts are not separated.

### 5. Real fee and fill rules: FAIL

- Fees are only partially fixed. The formulas at `docs/EVAL_PROTOCOL.md:54-68` omit Kalshi fee multipliers and cash/fee rounding implemented at `fees.py:3-12`, `fees.py:94-96`, and `fees.py:130-142`. They also omit Polymarket's half-even cent rounding and non-Tier-1/new-account cases implemented at `poly_us_trader.py:425-451`. v17 itself says the real Polymarket tier still needs confirmation (`AGENT_TASKSv17.md:481-482`).
- Taker fill probability 1.0 at ask with zero slippage (`docs/EVAL_PROTOCOL.md:74-77`) has no real-depth, partial-fill, book-age, size, or missing-data condition. R8 requires a taker to consume recorded real depth (`AGENT_TASKSv17.md:131-136`).
- Maker defaults of 0.35, random queue position in the top 3, and a hardcoded win-rate haircut (`docs/EVAL_PROTOCOL.md:79-85`) are invented fill/queue assumptions unless derived from the required recorded prints. R8 forbids invented fill odds and requires prints to trade through the price after queue ahead (`AGENT_TASKSv17.md:133-137`).
- The hybrid stop/exit path (`docs/EVAL_PROTOCOL.md:87-90`) conflicts with NO-only hold-to-settlement, where only unfilled entries may be cancelled (`AGENT_TASKSv17.md:39-40`). Maker execution must remain disabled until real-fill evidence (`AGENT_TASKSv17.md:703-707`).

### 6. Multiple-testing control: PARTIAL

- A variant count and Bonferroni ideas exist (`docs/EVAL_PROTOCOL.md:111-130`), satisfying the topic but not an enforceable primary-EV rule.
- The 68-count registry has no listed parameter values and does not say it includes every NO-price/minutes/asset/venue cell, repeated look, or future variant. Selecting the best uncounted cell can produce a false positive.
- `deflated_sharpe = sharpe * sqrt(1 - (log(68) / N))` (`docs/EVAL_PROTOCOL.md:124-130`) leaves N undefined and is not a market-clustered EV lower bound. Making Bonferroni an "Alternative" does not require the promotion EV bound at lines 178-181 to be adjusted.

### 7. Hard drawdown, daily-loss, and P(ruin) promotion limits: PARTIAL

- Realized maximum drawdown <= 30% is a promotion condition (`docs/EVAL_PROTOCOL.md:178-185`): this part is present.
- P(drawdown > 30%) < 20% and P(ruin) < 5%, with ruin defined as balance < $100, appear only as forecast targets (`docs/EVAL_PROTOCOL.md:18-26`), not promotion gates. The protocol does not say which model families must pass or prevent choosing the most optimistic family.
- No daily-loss limit or zero-breach condition appears in the promotion criteria. v17 explicitly requires drawdown, daily-loss, and ruin limits (`AGENT_TASKSv17.md:17-19`) and documents venue daily-loss rules that must be resolved and verified (`AGENT_TASKSv17.md:277-290`). A forecast target is not a hard promotion stop.

### 8. Minimum 300 settled markets and 14 days for promotion: FAIL

- The directive requires held-out real-data NO-only net EV with market-clustered lower bound above zero over at least 300 settled markets and 14 days before human approval (`AGENT_TASKSv17.md:63-68`).
- None of the promotion conditions at `docs/EVAL_PROTOCOL.md:176-186` requires either count. A 14-day bankroll forecast horizon and a generic sample-size warning (`docs/EVAL_PROTOCOL.md:150-162`) are not 14 days of settled-market evidence.

### 9. Negative NO-only stop rule: FAIL

- If no NO region clears the after-fee clustered lower bound, v17 requires stopping strategy-feature work and reporting, not widening bands or loosening gates (`AGENT_TASKSv17.md:33-38`, `AGENT_TASKSv17.md:644-648`).
- The protocol's post-run rule only says to state when P(target) < 1% (`docs/EVAL_PROTOCOL.md:207-210`). It contains no equivalent negative-NO-only stop.

### 10. Pre-registration evidence: PARTIAL

- The file claims "COMMITTED BEFORE FIRST FORECAST RUN" (`docs/EVAL_PROTOCOL.md:3-6`), but its commit/hash fields are unexpanded placeholders (`docs/EVAL_PROTOCOL.md:6`, `docs/EVAL_PROTOCOL.md:218-221`, `docs/EVAL_PROTOCOL.md:233-235`). Git shows the document last changed in `337da5d75c976535b66cfde48c7f522a9eb13d2a`; this review did not establish the chronology of every forecast run.
- Any correction after a forecast run must follow the versioning, clean-rerun, and human-approval rule at `docs/EVAL_PROTOCOL.md:225-231`; the placeholders must not be backdated.

## Minimal patch plan

1. **Version and data manifest.** Add a new protocol version and reason before T17-08. Record actual code/data hashes, exact calendar windows, production sources, row/market/day counts, and missing-data rules. Ban synthetic/sandbox/interpolated inputs. Keep final test data untouched by fill calibration and tuning.
2. **Pre-register the NO-only grid.** Fix decision-time NO-price and minutes-left bin edges, underlying and venue dimensions, the realized-vol-scaled gate, hold-to-settlement, and YES-only comparison. Taker is primary; maker is "not estimable" without R8-valid prints/real fills and remains disabled for execution.
3. **Use real execution accounting.** Require recorded book depth and public prints, actual partial/unfilled quantities, and no fill on missing data. Remove 0.35/random-top-3/haircut defaults. Use the production fee code semantics, including tier, multiplier, and rounding captured at decision/fill time.
4. **Make inference reproducible and multiplicity-safe.** Register every attempted configuration and segment cell as M comparisons. Resample whole markets, report market counts, and require the one-sided `1 - 0.05/M` market-clustered net-EV lower bound above zero for promotion. Keep Sharpe secondary.
5. **Add hard promotion gates.** For the exact held-out NO-only configuration/region require >= 300 settled markets spanning >= 14 distinct days, adjusted lower bound > 0, realized max drawdown <= 30%, zero breaches of explicitly resolved numeric per-venue daily-loss caps/day boundaries, and pre-registered P(drawdown > 30%) < 20% and P(ruin) < 5% across the required model-family aggregation. Then require human approval; never relax these gates to reach the balance target.
6. **Add the stop rule.** If no NO-only region clears the adjusted after-fee market-clustered bound, stop strategy-feature work and report. Do not widen bins, loosen gates, enable maker, or tune on the final holdout.

## Verification

- `LC_ALL=C grep -n '[^ -~]' docs/T17_PROTOCOL_REVIEW.md`
  - Result: PENDING
- `.venv/bin/python scripts/verify_source.py --all`
  - Result: PENDING

Not checked: NO-only/model results, forecast profitability, historical forecast-run chronology, production API/fee probes, and live behavior. These were outside this review-only scope.
