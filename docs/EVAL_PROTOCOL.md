# Pre-Registered Evaluation Protocol for Kalshi/Polymarket SuperBot

**Version:** 1.1
**Date:** 2026-11-23
**Status:** T17-08 UNBLINDING BLOCKED UNTIL THIS VERSION AND THE SECTION 3 MANIFEST ARE FROZEN
**Amendment reason:** Resolve docs/T17_PROTOCOL_REVIEW.md before any T17-08 NO-only result is opened.

---

## 1. Objective

This protocol fixes the forecast evaluation and the pre-registered T17-08 NO-only edge study for Kalshi 15-minute binary markets and Polymarket US Up/Down 15-minute markets. NO means Kalshi NO or Polymarket US Down. The primary strategy is taker entry plus the calibrated realized-vol-scaled gate, with every filled position held to settlement.

The $3,250,000 balance is an aspirational forecast OUTPUT only. It is never a promotion threshold, sizing input, tuning objective, reason to select a region, or reason to relax a gate. P(target) is reported even when below 1%.

---

## 2. Metric Set (Fixed)

### T17-08 primary endpoint

For each fully crossed NO-price, minutes-left, asset, and venue region, the primary endpoint is realized net-of-fee EV per filled contract for taker NO entries passing the frozen realized-vol-scaled gate. The promotion statistic is the one-sided `1 - 0.05/M` market-cluster bootstrap lower confidence bound defined in section 7. It must be above zero.

Report YES-only (Polymarket US Up) beside NO-only with the same observation and accounting rules as a descriptive comparison. YES results cannot authorize YES trading, select a NO region, or change the NO-only gate. Maker is a separately labelled column and is `NOT ESTIMABLE` unless section 5's real-fill requirement is met.

### Hard risk and forecast metrics

| Metric | Fixed definition | Limit or use |
|--------|------------------|--------------|
| **Realized maximum drawdown** | Peak-to-trough loss on the exact held-out NO-only configuration/region | <= 30% |
| **Daily loss** | Venue-specific realized daily-loss accounting in section 11 | Zero breaches of section 11 caps |
| **P(Drawdown > 30%)** | Forecast probability of peak-to-trough loss exceeding 30% | < 20% for every base family in section 9 |
| **P(Ruin)** | Forecast probability of balance < $100 | < 5% for every base family in section 9 |
| **Median Day-14 Balance** | Median terminal bankroll after 14 trading days | Report only |
| **P(Balance >= $3.25M)** | Probability of reaching the aspirational target | Report only; never a gate |
| **Daily Growth Required** | Growth needed from current real balance to target | Report only; never sizing or tuning |
| **Coverage (p5/p50/p95)** | Realized balance within predicted bands | 90%/50%/10% honesty targets |

No averaging over model families and no selecting the most optimistic family is permitted for the probability limits. A required family that is not estimable blocks promotion rather than being silently dropped.

### Secondary metrics

- Net EV per trade and per filled contract after actual fees and real-depth fills.
- Settled win rate, average hold time, and fill/partial-fill/unfilled rates.
- Sharpe ratio of daily returns; this is secondary and never substitutes for the primary EV bound.
- Realized maximum drawdown and daily-loss breaches.
- Calibration error (Brier score and reliability diagram).
- Counts of eligible, excluded, missing-data, and unfilled markets and orders.

---

## 3. Windows, Separation, and Pre-Unblinding Manifest (Fixed)

| Window | Relative period | Exclusive purpose |
|--------|-----------------|-------------------|
| **Train** | Days 1-60 | Model fitting and hyperparameter selection |
| **Validation** | Days 61-70 | Early stopping and model selection |
| **Calibration** | Days 71-74 | Realized-vol gate and probability/fill calibration only |
| **Final Test (Held-Out)** | Days 75-88 | One final evaluation; never fitting, calibration, selection, or tuning |
| **Live Paper** | Days 89+ | Real-time dry evaluation after the frozen final evaluation |

The calibration window is disjoint from the final test. Maker fill rates, realized-vol parameters, thresholds, model choices, bins, sizing, and risk settings may never use final-test features, outcomes, fills, or PnL. Final-test unblinding occurs once. Any later change requires a new protocol version and a new untouched holdout; rerunning the changed analysis on the unblinded days is not confirmatory evidence.

**Embargo:** One full market length (15 minutes) between windows. A market is kept whole by market ID. A market touching an embargo is excluded from both adjacent windows, and all its observations stay in one cluster.

**Exact calendar registration:** Before final-test unblinding, the immutable run manifest must replace the relative day labels with exact inclusive UTC start and exclusive UTC end timestamps for every window. Day 1 and day 88 are fixed from the frozen production dataset boundary, not chosen after viewing edge or PnL.

The manifest is part of this pre-registration and must contain, before unblinding:

1. Full protocol commit ID and SHA-256 of this file, recorded after commit without backdating; exact code and config commit hashes and the frozen strategy/gate/sizing config hash.
2. Exact UTC window boundaries, production API/feed/source identifiers, raw-file paths or object IDs, and an actual SHA-256 for every input file and the ordered manifest as a whole.
3. Row counts by dataset and window; distinct market and UTC settlement-day counts; all excluded markets and reasons; book/print/spot gaps and missing-data counts.
4. The frozen asset list from production catalog metadata, all strategy variants and segment cells, the exact integer M, bootstrap seed and repetitions, and all gate/calibration parameter values.
5. Actual production fee-tier/series metadata and the verified numeric daily-loss caps and day-boundary rules.

No placeholder hash or missing manifest field is acceptable. If any field is absent, the study is not registered, final-test outcomes must remain blinded, and promotion is blocked. The study report must reproduce the manifest hashes and counts without changing them.

---

## 4. Production Fee Accounting (Fixed)

The backtest must call the production fee implementation at the manifest's code hash, using each actual partial-fill quantity and executable price. A formula copied into a study script is not sufficient. Fee tier, series `fee_type`, series multiplier, account status, and any explicit multiplier must come from production metadata captured at decision/fill time. Missing, stale, unknown, or unverified fee metadata means no fill and blocks promotion for that venue; no default tier is treated as verified.

### Kalshi

- Use `fees.py:taker_fee_dollars` and `buy_cash_out`, not mid-price or an unrounded quadratic approximation.
- The raw taker fee is `FEE_RATE * explicit_multiplier * series_fee_multiplier * C * p * (1-p)`, with the implementation's upward rounding to $0.000001. With no series override, the code's standard multiplier is 1.0, but this study still requires the real production series metadata.
- Apply the actual combined multiplier through the production function's multiplier argument when calling `buy_cash_out`; do not drop the series multiplier. `buy_cash_out` implements the non-direct cent-grid cash rounding. Direct-member $0.0001 balance precision and accumulated rounding rebates require the matching production semantics and real account status; this helper alone is not direct-member evidence. Missing applicable cash/rounding/rebate semantics means no fill. Settlement is not a trade and has no trade fee in the current implementation.
- Maker execution is disabled. For completeness only, standard series are maker-free and a production `quadratic_with_maker_fees` series uses the code's maker coefficient and series multiplier with upward $0.000001 rounding. This never licenses a maker fill assumption.

### Polymarket US

- Use `poly_us_trader.py:_taker_fee` in the production `us_tier` model: actual trailing-30-day taker-tier theta times `C * p * (1-p)`, rounded to the cent with round-half-even.
- Record and apply the actual tier, including non-Tier-1 and new-account schedules. The current code identifies Tier 1 theta 0.095 and new-account days 1-7 theta 0.0695; neither may be assumed without production account/tier evidence. The legacy international rate is not the T17-08 US fee model.
- Makers are free and settlement has no trade fee under the current implementation, but maker remains disabled and not estimable without section 5 evidence.

---

## 5. Real Fills and NO-Only Lifecycle (Fixed)

### Common production-data rule

Only production APIs, immutable recorded production history, and our own real fills are allowed. Sandbox/demo data, invented prices or ticks, synthetic fallback prices, assumed slippage, interpolated gaps, invented fill odds, and hardcoded win-rate haircuts are forbidden. Forecast resampling of real logs is permitted only for forward distributions and never creates a historical fill.

At each decision, use synchronized recorded production books, BBO/depth, public trade prints, spot inputs, and measured per-decision latency. Missing, gap-affected, stale, crossed, or one-sided required data means **NO FILL**. Never forward-fill a book or print. Record every rejected and unfilled order, its reason, requested quantity, and available real depth; exclusions are reported rather than silently dropped.

### Taker (primary)

Reconstruct the real book at measured order-arrival time and consume recorded ask depth for a NO/Down buy, walking price levels up to the frozen order limit. Filled quantity cannot exceed real displayed depth or the frozen depth cap. Quantity beyond available depth is partial or unfilled and is recorded; fees and PnL use only actual filled quantity and volume-weighted real prices. If the arrival-time book or required latency observation is absent, there is no fill. A top-of-book fill at full requested size with zero slippage is never assumed.

### Maker (disabled; normally not estimable)

Maker execution is hard-disabled. A maker estimate requires human-approved minimum-size real maker fills plus the corresponding production books and public prints. A print must trade through the resting price after the recorded queue ahead; queue position cannot be random or assumed. Books/prints alone may audit that rule but cannot substitute for the required real-fill rate. Without that evidence, report maker as `NOT ESTIMABLE`; do not emit a numeric maker EV, fill rate, or promotion bound and do not count maker as a fallback strategy.

### Position lifecycle

Only NO/Down entries are eligible for promotion. Every filled position settles; there is no stop, stop-out, early sale, hybrid exit, or mark-to-market exit substituted for settlement. Only an unfilled entry order may be cancelled. YES/Up is descriptive comparison only.

---

## 6. Latency Model (Fixed)

| Stage | Budget (p95) | Budget (p99) |
|-------|--------------|--------------|
| Feed receive | 30 ms | 50 ms |
| Features + GBM | 10 ms | 20 ms |
| Ensemble lookup | 5 ms | 10 ms |
| Risk + sizing | 2 ms | 5 ms |
| Order build + sign | 10 ms | 20 ms |
| Network submit | RTT measured | RTT measured |
| **Total tick-to-submit** | **< 500 ms** | **< 1,000 ms** |

**Measurement:** `latency_measurements` table, per-decision timestamps.
**Alert:** n8n when p95 breaks budget for 5 consecutive minutes.

---

## 7. NO-Only Grid and Multiplicity (Fixed Before Unblinding)

### Observation time and fixed cells

Decision time is the timestamp at which the frozen strategy evaluates an entry, not a later bar, fill, or settlement timestamp. `p` is the executable NO/Down price in dollars and `t` is exact minutes from that decision timestamp to the market's production close/expiry timestamp. Invalid prices, negative time, and decisions more than 15 minutes from expiry are excluded and counted.

NO-price bins are fixed at these edges, with left-closed/right-open intervals except the last:

`[0.00,0.10) [0.10,0.20) [0.20,0.30) [0.30,0.40) [0.40,0.50)`
`[0.50,0.60) [0.60,0.70) [0.70,0.80) [0.80,0.90) [0.90,1.00]`

Minutes-left bins are fixed as:

`[0,1) [1,2) [2,5) [5,10) [10,15]`

Report every fully crossed NO-price by minutes-left by underlying-asset by venue cell for the primary taker plus calibrated realized-vol-scaled gate. Venue is separately `KALSHI` or `POLYMARKET_US`; asset is the canonical production-catalog underlying symbol. The asset list is frozen from catalog metadata in the manifest, never selected using PnL. Cells are never merged, split, pooled across assets/venues, or redefined after unblinding. Empty and insufficient cells are reported as such, not omitted.

The only primary configuration is taker NO/Down entry with the realized-vol-scaled gate calibrated and frozen using only train/validation/calibration data. An ungated taker result and YES-only result are descriptive, not alternative promotion candidates. Any additional attempted configuration, gate, parameter set, side used for selection, execution method, or repeated final-test look must be registered and included in M before its bound is examined.

### Frozen exact comparison count

The manifest must enumerate every attempted configuration, including every earlier variant whose result is used, as exact parameter key/value pairs with its code/config hash; category totals without those values are not a valid registry. For each registered hypothesis, count every one of the `10 * 5 * A * 2` fully crossed cells, where `A` is the frozen production-catalog asset count and 2 is the venue count. Then `M` is the exact sum of those cell counts over all attempted configurations, sides, execution methods, model/gate/sizing variants, and final-test looks whose results or bounds are examined. For the single primary NO configuration and one look, this contributes exactly `100 * A` hypotheses; YES-only, ungated, and every other comparison are added separately.

The manifest freezes the complete registry, its hash, and the resulting exact integer M before unblinding. M is neither a hardcoded legacy total nor a lower bound, and it is never a count of only nonempty/favorable cells. Registered empty, insufficient, failed, missing-data, and not-estimable cells remain in M. Every reported bound uses that same M. Any later look or variant requires a new frozen registry with all earlier and new hypotheses, a larger exact M, a new untouched holdout, and a new protocol version before its result is examined.

### Market-cluster primary inference

For each region, aggregate all filled observations and actual net PnL by market first. No quote is an independent observation. Use a nonparametric market-cluster bootstrap with **seed 17008** and **10,000 repetitions**: resample whole distinct market IDs with replacement, keeping every observation from a selected market together. For each replicate, compute total net-of-fee PnL divided by total filled contracts across the resampled market aggregates. Unfilled orders contribute no fictitious fill or PnL and are reported separately.

Let `alpha = 0.05/M`. The promotion lower bound is the empirical `alpha` quantile of the 10,000 replicate net-EV values, a one-sided `1 - 0.05/M` lower confidence bound. The point estimate uses the same total-net-PnL/total-filled-contract definition on the observed markets. Report M, alpha, seed, repetitions, point estimate, lower bound, distinct settled markets, distinct UTC settlement days, and filled/unfilled counts for every cell. Sharpe and any deflated-Sharpe calculation are secondary only.

---

## 8. Production Data Sources and Coverage (Fixed)

| Dataset | Required production source | Required coverage |
|---------|----------------------------|-------------------|
| Kalshi order book and BBO | Production WS `orderbook_snapshot`, `orderbook_delta`, and ticker | Complete frozen 88-day windows plus settlement labels |
| Polymarket US order book and BBO | Production WS `orderbook` and `bbo` | Complete frozen 88-day windows plus settlement labels |
| Venue public trade prints | Production venue trade/print feed or history | Complete frozen 88-day windows |
| Binance/Coinbase spot trades | Production WS trades; production REST klines only for auditable history | Complete frozen 88-day windows |
| BRTI-style proxy inputs | Computed only from the recorded real spot inputs | Complete frozen 88-day windows |
| Settlement outcomes/reference prices | Production Kalshi and Polymarket US settlement/history endpoints | Permanent after capture |
| Fees, tiers, series rules, and decisions | Production metadata/API records and local decision log | Complete frozen 88-day windows |

All raw inputs are archived immutably in compressed Parquet/CSV with their hashes in the manifest; local SQLite tables may include `orderbook_snapshots`, `bbo_snapshots`, `spot_ticks`, and `settlement_data`, but a table name is not provenance. Retention shorter than the full study window is insufficient. A pre-run row-count/gap audit must demonstrate coverage for every declared window; a 3-day book archive or 60-day spot archive cannot support an 88-day study.

No synthetic, demo, sandbox, interpolated, or unexplained third-party observations are permitted. A third-party historical dataset is eligible only when its production provenance and hash are frozen and a documented random sample is cross-checked against venue production history. Missing data remains missing and produces no fill.

---

## 9. Forecast Model Families (Fixed)

Six base model families produce 14-day bankroll forecasts from the same real, settled trade log:

1. **Trade-level bootstrap:** Resample real trades with replacement, apply the frozen live sizing policy, and compound.
2. **Daily block bootstrap:** Resample real daily PnL blocks to preserve autocorrelation and compound.
3. **GBM bankroll model:** Fit mean and volatility to real daily log returns and sample parameter uncertainty.
4. **Regime-switching (HMM):** Use 2-3 volatility regimes driving edge and frequency.
5. **Fat-tail/jump model:** Use Student-t or compound Poisson for gap losses.
6. **ML quantile (LightGBM):** Estimate daily PnL quantiles from day-start features.

Stress variants of all six use fees +25%, realized-fill sensitivity -25% by seeded thinning of actual real fills only, and edge -50%; they are forward sensitivity outputs, never rewritten historical fills, are reported separately, and cannot improve a base family's risk result. Every base family must satisfy both probability limits in section 2 for the exact candidate configuration/region, per venue and for the combined portfolio when both venues are used. Failure or non-estimability of any required family blocks promotion. Forecasts may model real logs forward but may never create historical fills or use the balance target for sizing or tuning.

**Output per venue per model:** median, p5, p25, p75, and p95 of day-14 balance; P(balance >= $3.25M); P(DD > 30%); P(ruin); daily growth required from current real balance; sample-size warning; and manifest/seed/code hashes. Do not log raw balances or secrets.

---

## 10. Forecast Honesty Tracking (Fixed)

- Log predicted p5/p50/p95 daily.
- Compare with realized next-day balance.
- Report coverage: percent of days realized in [p5, p95] (target 90%) and [p25, p75] (target 50%).
- If coverage < nominal - 5 percentage points for any model family, flag that family. Band changes require calibration/validation data only and a new untouched final holdout before promotion.
- Archive reports without raw balances or secrets.

---

## 11. Hard Promotion Gates (Fixed)

Every gate applies to the exact same frozen held-out NO-only taker configuration and fully crossed region. Combining regions, models, or venues to satisfy a gate is forbidden. All of the following are necessary; none is sufficient alone:

1. The section 7 one-sided `1 - 0.05/M` market-clustered net-of-fee EV lower bound is strictly above zero.
2. At least **300 distinct settled markets**, each contributing at least one filled NO/Down position held to settlement, span at least **14 distinct UTC settlement days** in the final test. Report both counts for the exact region; quote or trade counts do not substitute.
3. Realized maximum peak-to-trough drawdown is <= 30%.
4. There are zero daily-loss-limit breaches. Days run from 00:00:00 UTC to the next 00:00:00 UTC, and venue losses are never netted together:
   - **Kalshi:** New entries halt when cumulative daily net PnL is <= `-0.05 * day-start balance`; the cap is 5% of day-start balance.
   - **Polymarket US:** New entries halt when cumulative realized losing PnL, summing absolute losses without offsetting wins, is >= `max($5, 0.05 * current equity)`; the cap is the larger of $5 or 5% of current equity.
   A settled loss that reaches or crosses a cap is a breach even if the halt prevents later entries. Before unblinding, production rules/code and these numeric caps, equity definitions, and day boundaries must be verified and recorded in the manifest. If production uses an additional or different day boundary, both production and UTC evaluations must show zero breaches; an unresolved or stricter production rule blocks promotion and is never relaxed.
5. P(drawdown > 30%) < 20% and P(ruin) < 5% for every base forecast family as required by sections 2 and 9. Ruin is balance < $100. The most optimistic family cannot be selected.
6. Dry/live parity holds for the frozen config hash. Subsequent real dry/paper results agree with backtest EV within +/-10%; this comparison never tunes on the final test.
7. No martingale or target-driven behavior: size never increases because of a loss or the $3.25M target. Sizing uses fractional Kelly on the lower edge bound, real-depth caps, and one correlated-exposure budget.
8. T17-43 caps and all other independent v17 promotion requirements pass, and recorded human approval follows the evidence. No agent enables live trading, raises size, or relaxes a cap without that approval.

Failure of any gate means dry/paper only. Empty or insufficient cells are not promoted. Maker remains disabled until its separate real-fill evidence and positive adjusted bound exist under a new pre-registration.

---

## 12. Execution Protocol

### Pre-Unblinding Checklist

- [ ] Version 1.1 is committed before T17-08 results are opened; full commit/file hashes are recorded without backdating.
- [ ] The immutable section 3 manifest has exact UTC windows, production provenance, actual data hashes, row/market/day counts, and missing-data audits for the full windows.
- [ ] Train, validation, calibration, and final-test separation is enforced; final test has never informed calibration, fills, model choice, sizing, bins, or thresholds.
- [ ] Production fee code, actual tier/series metadata, and rounding semantics are verified and frozen.
- [ ] Real book depth, public prints, partial/unfilled handling, and missing-data-no-fill behavior are verified; maker is disabled.
- [ ] The NO-price/minutes/asset/venue grid, realized-vol gate, exact M, bootstrap seed 17008, and 10,000 repetitions are frozen.
- [ ] Latency budgets are measured and under threshold.
- [ ] Numeric drawdown, daily-loss, ruin, sizing, exposure, and dry/live parity gates are frozen.

### Determinism and outputs

The backtest, NO-only analysis, and forecast commands and their complete argument lists must be recorded in the manifest/report. The same frozen inputs, code, config, and seeds must produce identical outputs. The market-cluster bootstrap seed is 17008; the existing forecast seed 7 remains fixed for forecast-only simulation and does not replace the cluster seed.

Archive the study and forecast reports with manifest hashes, all attempted cells (including empty and failed cells), coverage, and risk results. State plainly when P(target) < 1%; never alter sizing or tuning in response.

### Mandatory negative-NO-only stop

If no NO-only region clears the section 7 adjusted after-fee market-clustered lower bound above zero, record exactly: **"no NO-only strategy clears the bar."** Stop strategy-feature work and report to the human. Do not widen or merge bins, loosen the realized-vol/risk gates, reduce M, enable maker, tune on the final holdout, or use the forecast target to justify another attempt. A negative result is a valid deliverable. Any genuinely new hypothesis requires new production data, a new untouched holdout, a versioned pre-registration, and human direction.

---

## 13. Version History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-11-20 | SuperBot Agent | Initial forecast protocol; historical run chronology not re-certified here |
| 1.1 | 2026-11-23 | `ubuntu:opencode:kalshi_deepbot` | Pre-register T17-08 real-data NO-only grid, fills, fees, inference, risk/sample gates, and stop rule before unblinding |

The parent review/commit evidence must record the actual full protocol commit ID and this file's SHA-256 before any T17-08 result is opened. The immutable run manifest separately records actual input-data hashes; shell placeholder text is not evidence.

---

## 14. Sign-Off and Amendment Rule

This version becomes the T17-08 pre-registration only when committed before any T17-08 result is opened and before final-test unblinding. Any amendment after forecast or study unblinding requires:

1. A new version number and explicit reason.
2. A newly frozen manifest and complete rerun from raw production data.
3. A new untouched final holdout; the unblinded holdout cannot restore confirmatory status.
4. Human approval.

No commit, hash, date, or sign-off may be backdated. This protocol does not itself authorize live orders.
