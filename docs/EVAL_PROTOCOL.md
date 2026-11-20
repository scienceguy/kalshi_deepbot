# Pre-Registered Evaluation Protocol for Kalshi/Polymarket SuperBot

**Version:** 1.0  
**Date:** 2026-11-20  
**Status:** COMMITTED BEFORE FIRST FORECAST RUN  
**Hash:** `git rev-parse HEAD` at commit time

---

## 1. Objective

This protocol defines the evaluation framework for the SuperBot trading system across Kalshi 15-minute binary markets and Polymarket US up/down 15-minute markets. It MUST be committed BEFORE any forecast run to prevent data snooping and overfitting.

---

## 2. Metric Set (Fixed)

### Primary Metrics (per venue, per model family)
| Metric | Definition | Target |
|--------|------------|--------|
| **Median Day-14 Balance** | Median terminal bankroll after 14 trading days | > $3,250,000 |
| **P(Balance ≥ $3.25M)** | Probability of reaching aspirational target | > 1% |
| **P(Drawdown > 30%)** | Probability of peak-to-trough exceeding 30% | < 20% |
| **P(Ruin)** | Probability of balance < $100 | < 5% |
| **Daily Growth Required** | CAGR needed from current real balance to target | Reported |
| **Coverage (p5/p50/p95)** | Realized balance within predicted bands | 90%/50%/10% |

### Secondary Metrics
- **Net EV per trade** (after fees, fill model)
- **Win rate** (settled)
- **Average hold time** (minutes)
- **Fill rate** (maker vs taker)
- **Sharpe ratio** (daily returns)
- **Max drawdown** (realized)
- **Calibration error** (Brier score, reliability diagram)

---

## 3. Walk-Forward Windows (Fixed)

| Window | Period | Purpose |
|--------|--------|---------|
| **Train** | Days 1-60 | Model fitting, hyperparameter selection |
| **Validation** | Days 61-74 | Early stopping, model selection |
| **Test (Held-Out)** | Days 75-88 | Final evaluation, NEVER used for tuning |
| **Live Paper** | Days 89+ | Real-time paper trading |

**Embargo:** 1 market length (15 minutes) between windows to prevent leakage.

**Cluster Unit:** Market (not quote). All quotes from same market kept together.

---

## 4. Fee Models (Fixed)

### Kalshi
- **Taker fee:** `0.07 × C × p × (1-p)` where C=contracts, p=price
- **Maker fee:** Free on standard series (KXBTC15M, KXETH15M, etc.)
- **Maker fee series:** `quadratic_with_maker_fees` per series_fee_info
- **Settlement fee:** $0

### Polymarket US
- **Tier 1 (30-day vol < $10k):** Theta = 0.095
- **Fee formula:** `Theta × C × p × (1-p)`
- **Maker fee:** Free
- **Settlement fee:** $0

**Source:** Verified from live API responses (T15-01).

---

## 5. Fill Models (Fixed)

### Taker Model (Conservative Default)
- **Fill probability:** 1.0 (immediate at ask)
- **Slippage:** 0 (cross spread)
- **Latency:** Measured RTT + 10ms signing

### Maker Model (Conservative)
- **Fill probability:** `measured_fill_rate` from historical resting orders
  - Default: 0.35 (35%) if no history
  - Updated daily from `scripts/q_floor_holdout.py` holdout
- **Queue position:** Assumed random within top 3
- **Adverse selection:** Win rate on fills = `measured_q_floor` (0.9928 holdout) - 0.02
- **Cancellation:** Unfilled orders cancelled at `ENTRY_WINDOW_END_MIN`

### Hybrid Model
- **Entry:** Maker (resting limit at ask-1c or bid+1c)
- **Exit:** Taker (cross at settlement or stop)
- **Fill prob:** `measured_fill_rate × (1 - adverse_selection_factor)`

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

## 7. Strategy Variants & Multiple Testing Correction (Fixed)

### Count of Variants Tried
| Category | Variants |
|----------|----------|
| GBM parameters (vol, drift, window) | 12 |
| Entry bands (min/max cents, window) | 20 |
| Sizing (Kelly fraction, caps) | 8 |
| Gate thresholds (ATR, EV, cushion) | 15 |
| Model types (GBM, momentum, locked-share) | 3 |
| Cross-venue arb params | 10 |
| **Total** | **68** |

### Multiple Testing Haircut
**Method:** Bonferroni-adjusted Sharpe (deflated Sharpe)
- Adjusted α = 0.05 / 68 = 0.000735
- Corresponding z-threshold: 3.16
- Report: `deflated_sharpe = sharpe * sqrt(1 - (log(68) / N))`

**Alternative:** Bonferroni on p-values for EV > 0.

---

## 8. Data Sources (Fixed)

| Dataset | Source | Period | Retention |
|---------|--------|--------|-----------|
| Kalshi orderbook snapshots | WS `orderbook_snapshot` + `orderbook_delta` | Live | > 3 days (Parquet) |
| Kalshi BBO | WS ticker | Live | > 3 days (Parquet) |
| Polymarket US orderbook | WS `orderbook` | Live | > 3 days (Parquet) |
| Polymarket US BBO | WS `bbo` | Live | > 3 days (Parquet) |
| Spot prices (Binance/Coinbase) | WS trades + REST klines | Live | > 60 days (Parquet) |
| BRTI proxy (60s median-of-mids) | Computed from spot | Live | > 60 days (Parquet) |
| Settlement prices | Kalshi API + Polymarket settlement endpoint | Live | Permanent |

**T15-13:** All captured via `local_db.py` tables `orderbook_snapshots`, `bbo_snapshots`, `settlement_data`.

---

## 9. Forecast Model Families (Fixed)

Seven independent model families producing 14-day bankroll forecasts:

1. **Trade-level bootstrap** — Resample trades with replacement, apply live sizing policy, compound.
2. **Daily block bootstrap** — Resample daily P&L blocks (preserves autocorrelation), compound.
3. **GBM bankroll model** — Fit μ, σ to daily log returns; sample parameter uncertainty.
4. **Regime-switching (HMM)** — 2-3 volatility regimes driving edge & frequency.
5. **Fat-tail/jump model** — Student-t or compound Poisson for gap losses.
6. **ML quantile (LightGBM)** — Daily P&L quantiles from day-start features.
7. **Stress variants of 1-6** — Fees +25%, fill rate -25%, edge -50%.

**Output per venue per model:** median, p5, p25, p75, p95 of day-14 balance, P(balance ≥ $3.25M), P(DD > 30%), P(ruin), daily growth required from CURRENT REAL balance, sample-size warning, balances/seed/code hash.

---

## 10. Forecast Honesty Tracking (Fixed)

- Log predicted p5/p50/p95 daily.
- Compare with realized next-day balance.
- Report coverage: % of days realized ∈ [p5, p95] (target 90%), ∈ [p25, p75] (target 50%).
- If coverage < nominal - 5pp for any model family: widen bands, flag family.
- Archive to DB and MemPalace.

---

## 11. Promotion Criteria (T15-16, Fixed)

A model, band, or gate change is promoted to live ONLY if on the **held-out period**:

1. **Net-of-fee EV > 0** with clustered lower bound > 0 (cluster by market)
2. **Drawdown ≤ 30%** (max peak-to-trough)
3. **Paper results agree** within ±10% of backtest EV
4. **No martingale behavior** (size never increases after loss)
5. **Human approval recorded** in MemPalace with evidence

Otherwise: stays paper-only (as README already states).

---

## 12. Execution Protocol

### Pre-Run Checklist
- [ ] This protocol committed to git (hash recorded)
- [ ] Data pipeline validated (T15-13 tables populated, > 60 days)
- [ ] Fee models match live API responses
- [ ] Fill models calibrated on holdout (not in-sample)
- [ ] Latency budgets measured and under threshold
- [ ] Multiple testing count finalized
- [ ] MemPalace logging operational

### Run Command
```bash
python forecast14d.py --venue both --seed 7
```
**Must be deterministic:** Same seed → identical results.

### Post-Run
- Archive results to DB (`forecast_results` table) and MemPalace.
- Publish coverage report.
- State plainly if P(target) < 1%.

---

## 13. Version History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-11-20 | SuperBot Agent | Initial protocol committed before any forecast run |

**Protocol hash at commit:** `$(git rev-parse HEAD)`
**Data hash at commit:** `$(sha256sum local_data/*.parquet 2>/dev/null | sha256sum)`

---

## 14. Sign-Off

This protocol is **immutable after first forecast run**. Any changes require:
1. New version number
2. Explicit reason for change
3. Re-run from scratch on held-out data
4. Human approval

**Committed by:** `ubuntu:opencode:kalshi_deepbot`  
**Date:** 2026-11-20  
**Git commit:** `$(git log -1 --format="%H %s")`