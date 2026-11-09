# Predictive Models for NET POSITIVE EV on Kalshi 15-Minute Binary Crypto Markets

**Project:** Kalshi SuperBot — research report (no source code modified)
**Date:** 2026-11-08
**Scope:** KXBTC15M, KXETH15M and related 15-minute binary crypto markets
**Goal:** Replace the current NEGATIVE-EV "final-minute taker" strategy (buy 80–99c favorite at ask in last minute) with a predictive model that trades only when model-estimated true probability > ask price + fee + minimum edge.

---

## 0. Executive Summary

Backtesting on Kalshi 15-minute BTC windows shows all "buy the late favorite at ask" bands lose money after fees. The core reason: the Kalshi taker fee `0.07 × C × P × (1−P)` (~0.79c at 87c) exceeds the raw market edge (~0.67c). The market price is fair (bands win within ~1 point of their price), so there is no lazy mispricing to scoop up. To turn NET EV positive, the bot must:

1. **Predict true probability better than the market price** using one or more of the models below.
2. **Subtract the exact taker fee** (including cent-rounding) from any edge estimate.
3. **Trade only when model_prob > ask + fee + min_edge** (e.g., 1–2c minimum edge).
4. **Size with fractional Kelly**, not flat size.

Six model families are covered: GBM/Black-Scholes, locked-share settlement-average, cross-exchange (Binance/Polymarket), momentum, plus edge-after-fee math and Kelly sizing. A dispatch/ensemble framework decides which model to trust under which market conditions.

---

## 1. Model 1 — GBM / Black-Scholes binary prediction model

### 1.1 Theory

A Kalshi 15-minute binary contract pays $1 if spot S_T >= strike K at expiry T. This is a cash-or-nothing binary call. Under Geometric Brownian Motion:

```
ln(S_T / S_0) ~ Normal( (r - 0.5·sigma^2) · T,  sigma^2 · T )
```

The risk-neutral probability of finishing in the money is N(d2):

```
d1 = ( ln(S_0 / K) + (r + 0.5·sigma^2)·T ) / (sigma·sqrt(T))
d2 = d1 - sigma·sqrt(T)  = ( ln(S_0/K) + (r - 0.5·sigma^2)·T ) / (sigma·sqrt(T))
P(S_T >= K) = Phi(d2)   [standard normal CDF]
```

For a driftless (r = 0) short-horizon crypto contract:

```
P(UP) = Phi( (ln(S_0/K) - 0.5·sigma_h^2) / sigma_h )
where sigma_h = sigma_per_second · sqrt(seconds_remaining)
```

N(d2) is the risk-neutral probability that the option expires ITM; it is the fair value of a cash-or-nothing binary call paying $1. This matches the kalshibot GBM implementation and the Black-Scholes-on-Polymarket article, both of which use N(d2) as the model-implied probability and compare it to the market price.

### 1.2 Parameter choices for crypto

| Parameter | Recommended value | Notes |
|---|---|---|
| sigma (annualized vol) | 0.80 (80%) | Crypto typical; kalshibot uses realized vol from trailing 1-sec Binance samples scaled to horizon |
| r (risk-free rate) | 0.00 | Negligible at 15-min horizon; kalshibot fixes drift at zero |
| T (time to expiry) | seconds_remaining / 900 | Fraction of 15-minute window |
| S_0 | current Binance spot (BTC/USDT) | WebSocket ~200ms latency |
| K | Kalshi floor_strike / target price | Locked at market open |

### 1.3 Volatility estimation (kalshibot method)

From trailing log-returns of 1-second Binance price samples over the contract's duration window:

```
r_i = ln(P_i / P_{i-1})
variance = mean((r_i - mean(r))^2)
vol_per_sample = sqrt(variance)
volatility = vol_per_sample * sqrt(samples_in_window)
```

Fallback: 0.15% per 15-minute period when <10 samples. kalshibot caps probabilities at [0.01, 0.99] and bounds Z at [-8, 8] SD for numerical stability.

### 1.4 Implementation guidance

- Compute `P(UP)` per window; `P(DOWN) = 1 - P(UP)`.
- Compare to Kalshi YES ask and NO ask respectively.
- Signal: `modelEdge_YES = (P(UP) - Kalshi_YES_ask) × 100` (cents); fire when > MIN_DIVERGENCE (kalshibot default 8%).
- Only trade within the first ~10 minutes of the 15-minute window; no entries within 30s of settlement (kalshibot time-based guards).
- Normal CDF via Abramowitz & Stegun (1964) rational approximation or `scipy.stats.norm.cdf` / `math.erf`.

### 1.5 Limitation

GBM assumes constant vol and log-normal returns; it underestimates tail risk during macro shocks. It is a baseline, not an oracle. The SiddhaBasu kalshi-bot pairs GBM with an XGBoost classifier cross-checked against it — GBM alone is not sufficient for edge.

**Sources:** brandononchain/kalshibot (Probability Model section); SiddhaBasu/kalshi-bot (How the BTC Model Works — GBM baseline); Ayrat Murtazin, "Black-Scholes on Polymarket" (dev.to, N(d2) as binary-call probability).

---

## 2. Model 2 — Locked-share / settlement average model

### 2.1 Why it matters

KXBTC15M does NOT settle on the last tick. It settles on the **simple average of 60 one-second CF Benchmarks BRTI prints** in the final minute of the window, compared to the strike locked at the open (which equals the prior window's final value). YES wins if the average >= strike ("at least" → ties go to YES).

As seconds tick by inside the final minute, more of the 60 prints are already fixed and cannot change. Remaining uncertainty therefore collapses faster than sqrt(time):

- **Inside the final minute:** with r seconds left, remaining uncertainty scales with **r^1.5**, not sqrt(r). At 30s left it is **~71% smaller** than a last-tick settle would be.
- **Before the final minute:** at 12:30 the uncertainty is ~14% smaller than last-tick.

This is the single most important structural insight: a trader pricing the contract as "spot vs. strike, vol × sqrt(time)" **overstates how much can still go wrong**, especially in the last minute, and therefore **underprices the favorite**. "Gamma explodes" describes a last-tick option; this contract is not one.

### 2.2 Modeling the partly-known average

Let the 60 prints be X_1 ... X_60 (one per second in the final minute). The settlement value is A = (1/60) · sum(X_i). After k seconds have elapsed, prints X_1...X_k are known constants; X_{k+1}...X_60 are still random.

Assuming each future print increment is an independent random-walk step with per-second vol sigma_s:

- Known contribution: (1/60) · sum_{i<=k} X_i (fixed)
- Remaining random sum: (1/60) · sum_{i=k+1}^{60} X_i
- Remaining variance: (1/60^2) · (60 - k) · sigma_s^2
- Remaining SD: sigma_s · sqrt(60 - k) / 60

So the remaining uncertainty scales with sqrt(60-k)/60 — which, parameterized by seconds-left r = 60-k, behaves like sqrt(r)/60 for the sum but the *relative* effect on the binary probability is captured by the r^1.5 finding (the PMP analysis models the distribution of the partly-known average).

### 2.3 Model implementation

1. Track the running CF Benchmarks BRTI average as prints arrive in the final minute.
2. Compute `locked_share = k / 60` (fraction of average already fixed).
3. Compute `remaining_avg_range` from remaining 60-k prints using per-second vol.
4. Derive `P(settlement >= strike | locked_share, current_partial_avg, remaining_vol)` via normal approximation:
   - `partial_avg = (1/60) · sum_{i<=k} X_i`
   - `remaining_mean = partial_avg` (driftless: future prints expected at current level)
   - `remaining_sd = sigma_s · sqrt(60-k) / 60`
   - `P = Phi( (partial_avg - strike) / remaining_sd )`  [approximately; more exactly, model current price vs. strike]
5. Trade when this conditional P > ask + fee + min_edge.

### 2.4 Empirical evidence (PMP, Oct 13–27 2026, 1,258 windows)

| Stretch | Entries | Won | Paid | Net equal-weight | Net contract-weighted |
|---|---|---|---|---|---|
| Before 12:30 | 2,563 | 90.3% | 90.4¢ | −0.76¢ | −0.49¢ |
| 12:30–14:00 | 1,373 | 93.7% | 92.8¢ | +0.43¢ | −0.31¢ |
| **Final 60s** | 538 | **97.1%** | 94.1¢ | **+2.67¢** | **+1.90¢** |

Final minute by band: 80–89¢: +5.47¢; 90–94¢: +3.70¢; 95–99¢: +1.22¢. The final minute is the best stretch for favorites *when you pick your spots* (blind buying every final-minute favorite = +0.67¢, not significant). The edge belongs to knowing how much of the average is fixed.

### 2.5 Limitation

Requires access to the CF Benchmarks BRTI feed (or close proxy) second-by-second in the final minute. Binance spot is ~2.5bps from the oracle and moves one-for-one within a window (Dai et al. arxiv:2606.31675, Appendix D), so Binance can serve as a proxy with basis risk.

**Sources:** PredictionMarketsPicks "The Last 60 Seconds of a Kalshi Bitcoin 15-Minute Market" (Oct 27, 2026); PredictionMarketsPicks "Kalshi 15-Minute Bots: The Math Every Bot Has to Beat"; Bot for Kalshi "Kalshi 15-Minute Bitcoin" (Oct 20, 2026); Dai, Jia, Yu "Settlement Manipulation in Prediction Markets" (arxiv:2606.31675, Appendix D: Binance spot tracks Chainlink resolution price).

---

## 3. Model 3 — Cross-exchange signal models

### 3.1 Binance spot vs. Kalshi strike (directional / latency arb)

Kalshibot's core strategy exploits the latency between Binance spot BTC (~200ms WebSocket) and Kalshi contract repricing (3–7s lag). When BTC moves on Binance, the probability of the contract outcome changes immediately but Kalshi's book takes seconds to re-equilibrate.

```
modelEdge_YES  = (P(UP)  - Kalshi_YES_ask)  * 100   [cents]
modelEdge_NO   = (P(DOWN) - Kalshi_NO_ask)   * 100
fire when modelEdge > MIN_DIVERGENCE  (kalshibot default 8%)
```

P(UP) comes from the GBM model (Section 1) driven by Binance spot. Entry conditions: Binance price live within 5s; divergence >= threshold; time since market open < TRADING_WINDOW (10 min); time to settlement > 30s; sufficient balance.

**Empirical result:** PMP tested a fair-value model (GBM-driven) trading whenever it disagreed with market midpoint by >3 points across 2,710 windows / 1,571 trades: −0.30¢ before fees, **−1.36¢ after fees**. The market's own price was the better forecast (Brier 0.1269 vs model 0.1326). So GBM/binance alone is NOT enough — the bar is beating an already-fair price by more than fee + spread.

### 3.2 Polymarket as a leading indicator

Polymarket lists equivalent 15-minute (and 5-minute) crypto up/down markets. Its CLOB is more liquid and its price formation is independent. Cross-exchange signal:

```
polyFair_UP   = (Polymarket_UP_bid + Polymarket_UP_ask) / 2
polyFair_DOWN = (Polymarket_DOWN_bid + Polymarket_DOWN_ask) / 2
polyEdge_YES  = (polyFair_UP  - Kalshi_YES_ask)  * 100
polyEdge_NO   = (polyFair_DOWN - Kalshi_NO_ask)  * 100
fire when polyEdge > MIN_EDGE  (kalshibot default 5%)
```

Rationale: Polymarket's higher liquidity and broader participant base give more efficient price discovery; when Polymarket mid exceeds Kalshi ask, Kalshi is undervalued relative to cross-market consensus.

**Critical caveat (settlement basis risk):** Polymarket.com (international) settles on a **Chainlink** 60-second average; Kalshi settles on **CF Benchmarks BRTI**. PMP tested 663 identical windows (Sept 21–27 2026): the two venues settled opposite ways **4 times**, all on windows decided by $5.10 or less — exactly where arbitrage is supposed to be safe. A push against one side can flip both venues differently, so both legs of an "arb" can lose. Polymarket US settles on the same index as Kalshi but publishes no volume, so fill depth is unmeasurable. Use Polymarket only as a *signal*, not as a guaranteed arb leg, unless you confirm settlement-source parity.

### 3.3 GBM-derived implied probability (from kalshibot GitHub)

kalshibot derives implied probability from GBM (Section 1) and clamps to [0.01, 0.99]. It also runs a **dual-side arbitrage**: if Kalshi YES_ask + NO_ask < $0.98, buy both sides for a guaranteed ~2% return (one side always settles at $1). kalshibot disabled this in production because the two legs are submitted separately and are not atomic — a partial fill leaves unhedged exposure.

### 3.4 Implementation guidance

- Primary signal: GBM-implied P(UP) from Binance spot vs. Kalshi ask (divergence threshold).
- Confirming signal: Polymarket mid vs. Kalshi ask (edge threshold), only when settlement source matches.
- Dual-side arb: only if you can atomically fill both legs; otherwise skip.
- Always price the fee into the signal at the fill price (Section 5).

**Sources:** brandononchain/kalshibot (Trading Strategies 1–3); PredictionMarketsPicks "Kalshi 15-Minute Bots"; PredictionMarketsPicks "Kalshi vs. Polymarket 15-Minute Bitcoin Markets: Same Window, Different Settlement Number".

---

## 4. Model 4 — Momentum models

### 4.1 Theory

Recent price direction predicts settlement in the final minutes: if the favorite's ask is rising, settlement probability converges toward 1. This is a trend-following / momentum signal over the last 1–5 minutes of the window.

Simple momentum features:
- `btc_move_since_quote = (S_now - S_quote) / S_quote` (is the book current?)
- short-horizon momentum (last 30–60s returns)
- quote age / staleness
- order-book imbalance (bid size vs ask size at top)
- RSI / MACD / Bollinger / VWAP deviation
- path efficiency (how directional the move was)

### 4.2 Evidence

- SiddhaBasu kalshi-bot uses 22 causal features including momentum, realized vol, quote staleness, order imbalance, trailing settlement bias, hour-of-day — fed to an XGBoost classifier with Platt-scaled probability calibration.
- Bot for Kalshi (June–Aug 2026 data): "second-by-second moves showed no momentum to follow and no reversal to fade" for buying-the-leader — so raw momentum alone is weak; it must be combined with a probability model and edge threshold.
- PMP 12:30-rule test: spot is not jumpier late (0.59–0.63bps per 5s in last 3 min vs 0.69–0.76 early), so momentum doesn't accelerate near the close — but the *settlement-average lock-in* (Model 2) makes late favorites more certain than spot-vs-strike pricing implies.

### 4.3 Implementation guidance

- Momentum is a **feature**, not a standalone strategy. Feed short-horizon momentum (+ sign of recent move) into the probability estimate or as a gate.
- Gate: only trade in direction of recent momentum (if favorite's ask rising over last 60s, lean YES; if falling, lean NO or skip).
- Combine with Model 2 (locked share): momentum + locked-share certainty compounds edge in the final minute.
- Use causal features only (`.shift(1)`, no lookahead) — SiddhaBasu enforces feature-parity between live and offline to catch train/serve skew.

### 4.4 Limitation

Momentum is noisy at 15-min horizon; GBM deliberately fixes drift at zero because "any momentum estimate is mostly noise." Use momentum as a tie-breaker/confirming signal, not the primary edge.

**Sources:** SiddhaBasu/kalshi-bot (How the BTC Model Works — XGBoost features); Bot for Kalshi "Kalshi 15-Minute Bitcoin"; PredictionMarketsPicks "The Last 60 Seconds" (spot not jumpier late).

---

## 5. Edge calculation after fees (Kalshi fee model)

### 5.1 Kalshi taker fee formula

Taker fee per contract = `0.07 × C × P × (1 − P)` where C = contracts, P = price in dollars. Fee is charged on the fill, win or lose. Rounded up to a cent (Kalshi aligns balances to $0.01 for broker-cleared members; direct members to $0.0001 with running credit). PMP's study rounded every fill's fee up to a whole cent, overstating cost for direct members; they report both.

Fee per contract (cents) at key prices:

| Price | Taker fee / contract | Upside if wins | Fee as % of upside |
|---|---|---|---|
| 50¢ | 1.75¢ | 50¢ | 3.5% |
| 70¢ | 1.47¢ | 30¢ | 4.9% |
| 80¢ | 1.12¢ | 20¢ | 5.6% |
| 90¢ | 0.63¢ | 10¢ | 6.3% |
| 95¢ | 0.33¢ | 5¢ | 6.6% |
| 99¢ | 0.07¢ | 1¢ | 7.0% |

Fee is largest at 50¢ and shrinks toward extremes — but so does upside. At 95¢ the fee (~0.33c) is ~7% of the 5c you can win.

### 5.2 Break-even probability

For a buy at price P (decimal) with taker fee f (decimal per contract):

```
net_payoff_if_win  = 1 - P - f
net_payoff_if_lose = -P - f
EV = p·(1 - P - f) + (1-p)·(-P - f)
   = p - P - f
```

Break-even (EV = 0):

```
p_break_even = P + f
```

Only trade when `model_prob > p_break_even + min_edge`, where min_edge is e.g. 0.01–0.02 (1–2c).

### 5.3 Full edge formula with fee rounding

```
P_market = ask_price   [decimal, e.g. 0.87]
f_raw    = 0.07 × P_market × (1 - P_market)   [fee per contract, dollars]
f        = round_up_to_cent(f_raw)             [for broker-cleared; else raw]
break_even = P_market + f
edge_cents = (model_prob - break_even) × 100
TRADE if edge_cents >= MIN_EDGE_CENTS   (e.g., 1–2)
```

**Numerical example (PMP's numbers):** buying 87c favorite, fee = 0.07×0.87×0.13 = 0.007917 → ~0.79c (rounds up to 1c for broker-cleared). Break-even ≈ 87.79c (or 88c rounded). Raw market edge ≈ 0.67c < fee → negative EV. This is exactly why the current strategy loses.

### 5.4 Rounding note

Kalshi computes each fill's fee to a millionth of a dollar, then aligns to $0.0001 (direct) or $0.01 (broker) with a running credit for overpaid rounding across an order's fills. For conservative backtesting, round each fill's fee **up to a whole cent** (PMP's method) — this overstates cost and is safe. For live trading, use the raw formula.

**Sources:** Kalshi Fee Schedule (kalshi.com/docs); Allium "Kalshi Fees: Why the Cost Peaks Near 50 Cents"; Bot for Kalshi "Kalshi Fees Explained"; Bot for Kalshi "Kalshi 15-Minute Bitcoin" (fee table); whirligigbear "Maker/Taker Math on Kalshi"; GWU paper (Bürgi 2026) fee formula f(P) = γP(1−P), γ=0.07.

---

## 6. Position sizing — Kelly criterion for binary options

### 6.1 Kelly formula

For a binary bet with probability p of winning, payout odds b (net return per $1 risked if win), loss probability q = 1−p:

```
f* = (b·p - q) / b          [full Kelly fraction of bankroll]
```

For a Kalshi binary contract bought at price P (decimal), if it wins you get $1 (net gain = 1−P), if loses you lose P. The odds b = (1−P)/P (you win (1−P) dollars per P dollars risked, i.e. b = (1−P)/P).

Substituting:
```
b = (1 - P) / P
f* = (b·p - (1-p)) / b
```

Simplified for binary payout:
```
f* = p - (1-p)/b = p - P·(1-p)/(1-P)
```

Fractional Kelly: `position_fraction = f* × KELLY_FRACTION` (kalshibot default 0.25 = quarter-Kelly; SiddhaBasu uses 0.15).

### 6.2 Recommended sizing

| Parameter | Recommended | Rationale |
|---|---|---|
| Kelly fraction | 0.15–0.25 | Full Kelly assumes perfect prob estimates; quarter-Kelly cuts variance 75% while keeping ~50% of growth |
| Per-trade cap | 5–15% of bankroll | Survive variance; kalshibot caps at 25% per trade, $25 max |
| Max concurrent positions | 10 | kalshibot default |
| Exposure ceiling | 50% of balance | kalshibot portfolio control |
| Drawdown circuit breaker | −10% → halve size; −20% → pause 15min | kalshibot risk controls |
| Per-market/day loss limit | record_market_loss / market_loss_exceeded | kalshi_superbot risk.py (already implemented) |

### 6.3 When edge is zero or negative

Kelly f* ≤ 0 → **do not trade** (size = 0). This is the key integration point: if model_prob ≤ break_even + min_edge, edge is non-positive, Kelly gives zero or negative size, and the bot sits out. This automatically enforces the "only trade with positive edge after fees" rule.

**Sources:** brandononchain/kalshibot (Position Sizing — Kelly); SiddhaBasu/kalshi-bot (Fractional Kelly 0.15); PMP Kelly calculator tool.

---

## 7. Combining models — ensemble or dispatch

No single model has a reliable standalone edge (PMP: GBM/binance model −1.36¢ after fees; late-favorite −0.22¢; market price is fair to ~1pt). The edge comes from **dispatch**: trust the model whose assumptions match the current market regime, and require all models to agree on direction before trading.

### 7.1 Dispatch table by market regime

| Regime (detectable condition) | Primary model | Secondary/confirming | Action |
|---|---|---|---|
| First 10 min of window, Binance live, spot vs strike diverged | GBM/Model 1 | Polymarket mid (Model 3) | Trade if GBM edge > min_edge AND Polymarket agrees on direction |
| Final minute (last 60s), BRTI prints arriving | Locked-share/Model 2 | Momentum (Model 4) | Trade if locked-share conditional P > ask + fee + min_edge; momentum same direction |
| Polymarket mid materially ≠ Kalshi ask, settlement source matches | Cross-exchange/Model 3 | GBM | Trade if poly edge > min_edge |
| Ask rising, favorite consolidating, >80c | Momentum/Model 4 | Locked-share | Trade only if momentum aligns with a positive-edge model |
| No model shows positive edge after fees | — | — | **No trade** (Kelly size = 0) |

### 7.2 Ensemble voting (optional, more complex)

If you prefer an ensemble over dispatch:
- Each model outputs a probability `p_i` and a confidence `c_i`.
- Ensemble probability = weighted average `sum(c_i · p_i) / sum(c_i)`, weights tuned on held-out data.
- Trade only if ensemble_prob > ask + fee + min_edge AND at least 2 of 3 models agree on direction.

**Recommendation:** Start with dispatch (simpler, auditable), not ensemble. Dispatch maps cleanly to observable regimes (time-to-close, feed availability) and avoids overfitting weights on limited history.

### 7.3 Decision flow (per cycle)

```
1. Fetch Kalshi ask/ask for the open window; compute break_even = ask + fee.
2. If time_to_close > 60s: compute GBM p(up) from Binance spot (Model 1).
   Else (final minute): compute locked-share conditional p (Model 2).
3. Optionally get Polymarket mid; check it agrees on direction (Model 3).
4. Optionally check momentum sign (Model 4) as confirming gate.
5. model_prob = primary model output; if any confirming model disagrees → skip.
6. edge_cents = (model_prob - break_even) × 100.
7. If edge_cents < MIN_EDGE_CENTS → skip (no trade).
8. Kelly size = fractional_kelly(model_prob, ask, bankroll); cap at per-trade max.
9. Place limit order at ask (or better) if size > 0.
```

### 7.4 Gating rules (from source material)

- Trade only within first ~10 min of 15-min window; no entries within 30s of settlement (kalshibot).
- Require Binance price live within 5s; else skip directional trading (kalshibot).
- Polymarket signal only when settlement source matches Kalshi (CF Benchmarks) — avoid Chainlink-vs-BRTI basis risk (PMP).
- Momentum never trades alone; it confirms or vetoes (Bot for Kalshi: raw momentum has no standalone edge).
- Fee priced into every signal at the fill price; count it twice if exiting early (PMP).
- Size via fractional Kelly; zero size when edge ≤ 0 (Kelly f* ≤ 0).

---

## 8. Recommended parameter values

| Parameter | Value | Source / rationale |
|---|---|---|
| MIN_EDGE_CENTS | 1.0–2.0 | Section 5; trade only after fees |
| MIN_DIVERGENCE (GBM) | 8% | kalshibot default |
| MIN_EDGE (Polymarket) | 5% | kalshibot default |
| Kelly fraction | 0.15–0.25 | kalshibot 0.25; SiddhaBasu 0.15 |
| Per-trade cap | 5–15% of bankroll (max $25) | kalshibot; variance survival |
| Trading window | first 10 min of 15-min window | kalshibot |
| No-entry zone | last 30s before settlement | kalshibot |
| Volatility | 80% annualized (crypto) | Section 1.2; realized vol from 1-sec samples |
| Fallback vol | 0.15% per 15-min | kalshibot when <10 samples |
| Probability clamp | [0.01, 0.99] | kalshibot; avoids extreme Kelly |
| Z bound | [-8, 8] SD | kalshibot numerical stability |
| Fee rate γ | 0.07 | Kalshi standard schedule; validate per market |
| Risk-free rate r | 0.00 | negligible at 15-min |
| Markets | KXBTC15M, KXETH15M | Kalshi 15-min crypto series |

---

## 9. Citations

1. **brandononchain/kalshibot** — Autonomous multi-strategy Kalshi bot; GBM implied-probability model, Polymarket cross-exchange signal, dual-side arb, Kelly sizing, adaptive scan rate. `https://github.com/brandononchain/kalshibot`
2. **SiddhaBasu/kalshi-bot** — KXBTC15M bot with GBM baseline + XGBoost classifier, walk-forward validation, Platt calibration, fractional Kelly 0.15, 22 causal features. `https://github.com/SiddhaBasu/kalshi-bot`
3. **Ayrat Murtazin**, "Black-Scholes on Polymarket: Finding Mispriced Binary Events with Python" — N(d2) as risk-neutral probability for binary call; IV extraction; edge vs Polymarket price. dev.to, Apr 2026. `https://dev.to/ayratmurtazin/black-scholes-on-polymarket-finding-mispriced-binary-events-with-python-2m50`
4. **PredictionMarketsPicks / The 7 Oracles**, "The Last 60 Seconds of a Kalshi Bitcoin 15-Minute Market: We Tested the 12:30 Rule on Two Weeks of Tape" — 60-print settlement average, r^1.5 uncertainty, final-minute edge +1.90¢ contract-weighted, no 12:30 cliff. Oct 27, 2026. `https://predictionmarketspicks.com/articles/kalshi-bitcoin-15-minute-last-60-seconds`
5. **PredictionMarketsPicks**, "Kalshi 15-Minute Bots: The Math Every Bot Has to Beat, Tested on 2,710 Bitcoin Windows" — late-favorite −0.22¢, GBM model −1.36¢ after fees, market price fair (Brier 0.1269), fee table. Sept 28 / updated Nov 8, 2026. `https://predictionmarketspicks.com/articles/kalshi-15-minute-bots-fee-math`
6. **Bot for Kalshi**, "Kalshi 15-Minute Bitcoin: How KXBTC15M Works & Strategy Data" — settlement on CF Benchmarks BRTI 60-second average, fee table (0.07×C×P×(1−P)), leader underpriced ~0.67¢ consumed by fee, resting-order adverse selection. Oct 20, 2026. `https://www.botforkalshi.com/blog/kalshi-15-minute-bitcoin-markets`
7. **Bot for Kalshi**, "Kalshi Fees Explained: Formula, Maker & Taker (2026)" — 0.07 × contracts × price × (1−price), series multiplier. `https://www.botforkalshi.com/blog/kalshi-fees-explained`
8. **Kalshi Fee Schedule** — `https://kalshi.com/docs/kalshi-fee-schedule.pdf`; `https://kalshi.com/fee-schedule`
9. **Allium**, "Kalshi Fees: Why the Cost Peaks Near 50 Cents" — fee formula 0.07 × C × P × (1−P). `https://allium.so/blog/kalshi-fees-why-the-cost-peaks-near-50-cents/`
10. **whirligigbear** (Andrew Courtney), "Maker/Taker Math on Kalshi" — net price after fee examples. `https://whirligigbear.substack.com/p/makertaker-math-on-kalshi`
11. **C. Bürgi**, "Makers or Takers: The Economics of the Kalshi Prediction Market" — fee formula f(P) = γP(1−P), γ=0.07; favorite–longshot bias. GWU / CESifo, 2026. `https://www2.gwu.edu/~forcpgm/2026-001.pdf`; `https://www.karlwhelan.com/Papers/Kalshi.pdf`
12. **D. Dai, R. Jia, S. Yu**, "Settlement Manipulation in Prediction Markets" — 15-minute horizon attenuates manipulation vs 5-minute; Binance spot tracks Chainlink resolution price (~2.5bps). arxiv:2606.31675, 2026. `https://arxiv.org/abs/2606.31675`
13. **PMXT** — unified API for Polymarket, Kalshi, and more ("CCXT for prediction markets"). `https://pmxt.dev/`
14. **TurbineFi** — "5 Kalshi Trading Strategies: Rules & Backtests" — strategy configurations, failure cases, testing limits. `https://www.turbinefi.com/blog/5-prediction-market-strategies-you-can-automate-today`

---

## 10. Integration notes for Kalshi SuperBot (no code changes in this report)

- The current "final-minute taker" (buy 80–99c favorite at ask in last minute) is NEGATIVE EV because taker fee (~0.79c at 87c) > raw edge (~0.67c). Do not simply size it larger.
- Replace with the dispatch framework in Section 7: primary model = GBM (first 10 min) or locked-share (final minute), confirming = Polymarket + momentum, gate = model_prob > ask + fee + min_edge, size = fractional Kelly.
- The `risk.py` module already has `record_market_loss` / `market_loss_exceeded` and drawdown halt — reuse these; add fee-aware edge gating before any order.
- Before live deployment, backtest the dispatch model on historical KXBTC15M/KXETH15M windows using PMP's method (round each fill fee up to a cent) and require positive net EV on held-out data.

---

**End of report.** This is research only; no source code was modified. All parameter values are starting points to be calibrated on held-out backtest data before live trading.
<tool_call>
<function=edit>