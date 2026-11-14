# Quantitative Audit: DegenHedge Signal Model vs Live Black-Scholes Diffusion

**Project:** kalshi_deepbot · **Dataset:** DegenHedge/kalshi-btc-15m (release v2026.10.1, `data/hf/`) · **Live code:** `model.py`, `crossvenue_edge.py`, `gates.py`, `signals.py` · **Backtest:** `legacy/parallel_backtest_hf.py` (1,890 combos, `hf_backtest_results.json`)

---

## 1. DegenHedge signal generation methodology (DATA_DICTIONARY.md / METHODOLOGY.md / README.md)

### What is disclosed

| Item | Specification |
|------|---------------|
| **Universe** | Every settled KXBTC15M window, 2026-01-10 05:15 UTC → 2026-10-04 23:45 UTC (25,370 windows). Signals logged 2026-03-24 → 2026-10-04 (11,541 graded signals). |
| **Strike** | Kalshi `floor_strike` = 60-second CF Benchmarks BRTI average *before the window opens* (`windows.official_strike`, range $58,039.90–$97,730.18). Strike type `greater_or_equal`: an exact tie resolves **Yes**. |
| **Settlement value** | Kalshi `expiration_value` = 60-second BRTI average *before the close* (`official_settlement_value`; blank on 365 windows, mostly Jan 2026). |
| **Signal time** | DegenHedge logger polls Kalshi's order book every 10 s; one observation per window at ~170 s before the close (median `seconds_to_close_at_log`). |
| **Entry ask** | `entry_ask_cents` = one minus the best opposing bid, rounded to the whole cent = **breakeven win rate before fees**. |
| **model_prob** | "The model's probability for `side`, 4 decimals" — written by the logger at signal time. |
| **Grade / won** | `won = 1` iff `side == outcome`; outcome = Kalshi official `result` when available, else the logger's own spot-feed fallback (167 signals, `graded_from=own_spot_feed_fallback`). |
| **Fee schedule** | Kalshi taker fee `ceil(0.07 × C × P × (1 − P))` dollars rounded up to the cent (P in dollars, C contracts), effective 2026-02-05. |

### What is NOT disclosed (critical finding)

**The exact formula for `model_prob` is not published.** DATA_DICTIONARY.md defines only its meaning ("The model's probability for `side`, 4 decimals"); METHODOLOGY.md says the logger writes "the model probability"; README.md lists "internal model features" under *"Not included, on purpose"*, and raw exchange spot prices were removed in v2026.10.1. **`model_prob` cannot be exactly reproduced from this release.** It is an undisclosed internal model.

### Headline numbers (build_summary.json / README)

- 11,541 signals · 10,527 wins · **win rate 91.21%** (Wilson 95%: 90.68–91.72%)
- Average entry **91.74¢** (median 96¢)
- Breakeven before fees at a 91.74¢ entry = 91.74% → the record sits **0.53 points BELOW breakeven before fees**, and further below after the Kalshi taker fee.
- **README explicit: "Nothing in this dataset shows an edge, and it must not be described as one."**

---

## 2. Comparison with our live implementation

### 2a. DegenHedge `model_prob` (undisclosed internal model)

- Formula: unknown / not disclosed.
- Underlying spot reference: not published (removed); only the derived `spot_close_disagreed` flag is kept.
- Strike: official Kalshi `floor_strike` (60s BRTI pre-open).
- Time-to-close: signal logged ~170 s before close; `seconds_to_close` recorded.
- Volatility assumption: undisclosed.
- Drift / risk-free rate: undisclosed.
- Output use: logged as a probability; the published record shows it is **not profitable** (below breakeven before fees).

### 2b. Our `model.py` — `gbm_probability_yes_wins(spot_price, strike, minutes_left, ...)`

**Formula (Black–Scholes d2):**
```
P(S_T >= K) = N(d2)
d2 = ( ln(S/K) + (r - 0.5*sigma^2) * T ) / ( sigma * sqrt(T) )
T = minutes_left / 525600   (years; 525,600 min/yr)
```
- `spot_price`: live composite spot (Binance/OKX via `binance_feed`) — **not** the Kalshi strike reference.
- `strike`: `market.strike` (Kalshi target).
- `minutes_left`: minutes to settlement from the market object.
- `volatility_annualized`: default **0.80** (config `MODEL_VOLATILITY`); dynamic option `MODEL_VOL_DYNAMIC` = realized per-minute vol × √525600, clamped [0.20, 5.00].
- `risk_free_rate`: default **0.05**.
- Output clamped to [0.001, 0.999].
- `model_probability_from_market` dispatches `gbm` / `momentum` / `locked_share` / `raw_mid`; in the final ≤1.5 min it forces `locked_share` (BRTI 60s average locking in, r^1.5 uncertainty collapse).

### 2c. `crossvenue_edge.py`

- Same GBM (`gbm_probability_yes_wins`) but vol = `binance_feed.composite_sigma × √525600`.
- Reference probability `ref_prob = P(S_T >= K)` vs Polymarket outcome price:
  `edge_cents = (ref_prob - poly_price) × 100`.
- Strike cushion: `cushion_z = |ln(S/K)| / (sigma_per_sqrt_min × √minutes)`, `sigma_per_sqrt_min = vol / √525600` — matches `gates.cushion_z`.

### 2d. Side-by-side

| Dimension | DegenHedge `model_prob` | Our GBM (`model.py`) | Our `crossvenue_edge` |
|---|---|---|---|
| Formula | undisclosed | Black–Scholes d2, N(d2) | same GBM |
| Underlying ref | undisclosed (removed) | live Binance/OKX spot | live composite spot |
| Strike | Kalshi `floor_strike` (60s BRTI pre-open) | `market.strike` | `assetPriceTerms.strike` |
| TTC | ~170 s at signal (median) | `minutes_left` from market | `end_dt - now` |
| Vol | undisclosed | 0.80 fixed, or realized/min annualized | realized/min annualized |
| Drift | undisclosed | r = 0.05 in d2 | none (pure d2 denominator for cushion_z) |
| Edge test | none published (record below breakeven) | `edge_cents = q×100 − ask − fee` | `(ref_prob − poly_price)×100` |
| Profitability | **below breakeven before fees** | thin, vol-dependent (+1.03c dynamic vs −0.01c fixed) | deviation vs Polymarket |

### Bottom line of the comparison

The two models are **different model classes**: DegenHedge's is an undisclosed internal signal whose published record is below breakeven; ours is a transparent GBM from spot-vs-strike with an explicit fee-adjusted edge test. They should **not** be treated as interchangeable, and `model_prob` from the dataset cannot be dropped into `gbm_probability_yes_wins` as a drop-in replacement.

---

## 3. Aligning paper-trading probability thresholds with the 1,890-combo backtest

### 3a. The 1,890-combo backtest (`legacy/parallel_backtest_hf.py`, `hf_backtest_results.json`)

Grid: `entry_ask_min × entry_ask_max × model_prob_min ∈ {0.60,0.70,0.80,0.85,0.90,0.92,0.94,0.96,0.98} × side ∈ {both,UP,DOWN}` = 1,890 combos over 11,541 signals. Each combo buys at `entry_ask_cents`, sells at 1.00 (win) / 0.00 (loss), taker fees on both legs via `taker_fee_dollars` (6dp).

**Best EV:** `model_prob_min=0.96`, `entry_ask ∈ [1,65]`, `side=UP` → **+0.3537c/trade**, but only **n=2 trades** (avg entry 63¢, both won, total +0.71¢). `model_prob_min=0.96` is tied at the top across 11 combos; `0.98` peaks at +0.3243c (n=1, DOWN).

**This is not statistically meaningful:** only two signals in the entire record pass `model_prob ≥ 0.96 AND ask ≤ 65¢`. A two-trade "optimum" is cherry-picked noise on a dataset whose own README says it shows no edge. **Do not import a flat `model_prob >= 0.96` cutoff into paper trading on this evidence.**

### 3b. Why a flat `model_prob >= 0.96` is miscalibrated

Live code does **not** use a flat probability cutoff. `signals.py` computes:

```
edge_cents(q, ask, fee) = q*100 - ask_cents - fee_cents
trade iff edge >= MODEL_MIN_EDGE_CENTS        (default 0.0)
```

which is algebraically the fee-adjusted breakeven test:

```
net_ev(q,p,fee) = q(1-p) - (1-q)p - fee = q - p - fee       (dollars/contract)
breakeven: q = p + fee(p)                                    p = ask in dollars
fee(p) = 0.07 * p * (1 - p)  (raw, 6dp), then rounded UP to the next cent
```

The raw fee peaks at p=0.5 and falls toward the tails; across the 95–99¢ favourite band it is always < 1¢, so the **effective cent-rounded fee is 1¢ per contract** at every ask in the band. Breakeven win probability `q = p + 0.01`:

| Ask (¢) | Raw fee $ | Effective fee (cent-rounded) | Breakeven q | Flat 0.96 verdict |
|---------|-----------|------------------------------|-------------|-------------------|
| 95 | $0.0033 | 1¢ | 0.96 | exactly breakeven, **zero edge, no buffer** |
| 96 | $0.0027 | 1¢ | 0.97 | **negative EV** (0.96 < 0.97) |
| 97 | $0.0021 | 1¢ | 0.98 | **negative EV** |
| 98 | $0.0014 | 1¢ | 0.99 | **negative EV** |
| 99 | $0.0007 | 1¢ | 1.00 | **impossible** — even q=1.0 loses to the 1¢ fee |

A flat `model_prob >= 0.96` is therefore **too lenient at high asks** (admits guaranteed-negative-EV trades at 96–99¢) and **too restrictive at low asks** (forgoes positive-edge trades between breakeven and 96% at, e.g., 92¢ where breakeven q ≈ 0.93). It is ask-independent by construction and cannot be correct across a 95–99¢ band.

### 3c. Correct alignment for paper engines

Replace any flat `model_prob >= X` gate with the ask-dependent fee-adjusted test already implemented in `signals.py` / `gates.py`:

```
q_min(ask_cents) = ask/100 + 0.07*(ask/100)*(1 - ask/100) + min_edge/100
trade iff model_prob >= q_min(ask_cents)
```

With `MODEL_MIN_EDGE_CENTS` set to a positive buffer (config guidance: ≥0.5¢, e.g. "set min edge ≥ 0.5c to require model_prob > ask+fee+0.5c"). Default 0.0 admits zero-edge trades; raise it for paper trading.

Additional calibration points from the measured record (config.py / AGENTS.md reality tax):

- **Band:** 95–99¢, minutes 6–11 (246 distinct markets, 557 quote rows). Taker + model gate = **+1.03c/trade** (win 98.6%, n=727).
- **Volatility:** fixed 0.80 → **−0.01c/trade** in the band; realized per-minute vol → **+1.03c/trade**. Use `MODEL_VOL_DYNAMIC=true`. The fixed 0.80 is **not calibrated** and is the difference between negative and positive EV.
- **Avoid:** 15–55¢ band (significantly negative EV on the larger sample) and `minutes_left ≤ 6` (pin risk; realized win rate < 98%).
- **Maker vs taker:** maker resting at ask−1 = −4.06c (fill rate 36.5%, adverse selection) — dead; taker at ask + model gate = +1.03c. Paper engines modeling fills must price as taker, not maker.
- `gates.py` already enforces `GATES_MIN_CUSHION_Z=1.80`, `GATES_PIN_RISK_MINUTES=6.0`, and an opt-in measured-win-rate floor (`GATES_MEASURED_Q=0.9928`, band 95–99¢, default off) — paper engines should enable the same guards rather than inventing a flat probability floor.

---

## 4. Recommendations

1. **Do not reproduce DegenHedge's `model_prob`.** Its formula is undisclosed and removed from the release; the published record is below breakeven before fees. Treat it as an external benchmark only.
2. **Do not import flat `model_prob >= 0.96`** from the 1,890-combo backtest; it rests on 2 trades and is ask-independent. The backtest's own `model_prob_min` dimension is a filter on a disclosed column, not a validated edge.
3. **Align paper engines to the fee-adjusted breakeven test** (`q >= ask + fee(ask) + min_edge`), which `signals.py`/`gates.py` already implement. Raise `MODEL_MIN_EDGE_CENTS` above 0.0 (e.g. 0.5¢) for a buffer.
4. **Calibrate volatility before trusting the gate:** fixed 0.80 loses (−0.01c); realized per-minute vol wins (+1.03c). Enable `MODEL_VOL_DYNAMIC` and re-validate against live outcomes before widening any band or gate.
5. **Trade the 95–99¢ band at minutes 6–11 only,** with taker pricing, `cushion_z >= 1.80`, and pin-risk floor at 6 min; avoid the 15–55¢ band entirely.
6. **Paper engines must model the cent-rounded fee** (minimum 1¢/contract at these prices), which makes buying at 99¢ unbreak-even even with a perfect model — the strongest argument against any flat probability cutoff.

---

*Sources: data/hf/{README,DATA_DICTIONARY,METHODOLOGY}.md, data/hf/build_summary.json, data/hf/validate_dataset.py; model.py; crossvenue_edge.py; gates.py; signals.py; config.py; fees.py; legacy/parallel_backtest_hf.py; hf_backtest_results.json.*