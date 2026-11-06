# Polymarket-Anchored Fair Value for Kalshi — Design

Date: 2026-11-05
Status: draft for approval

## Intent

Decide, with measurement, whether Kalshi's price on a market disagrees with a
better-informed reference price by more than the cost of trading. The reference
is Polymarket's price on the same event. Kalshi is the only venue we execute on;
Polymarket is read-only.

The question is not "can we trade more". It is "is there any deviation left after
fees and spread". A design that cannot answer "no, there is nothing" is not a
design, it is a wish.

## Why the premise is under suspicion

Three independent results cut against the current bot's thesis:

1. Kalshi 15-minute crypto is near-efficient. The 0–1h calibration slope is
   ~0.99, the leader is underpriced by ~0.67c at the ask, and the taker fee is
   ~1.55c. Measured net is **−0.42 to −0.88c per contract**.
2. A resting favourite filled at 49.5c and won **45.3%** — **−5.87c/contract**
   paper, **−5.79c** live. Resting maker orders fill when the favourite stops
   being the favourite.
3. Our own 15,420-window sweep found only one parameter band that traded at all:
   **43.4% wins**, EV $0.0037/trade, 92.5% max drawdown. That is noise with fees
   on top.

So the burden of proof sits on this design, not on the sceptic. Milestone 0
exists to kill the project cheaply if the deviation is not there.

## Goals

- Measure the Kalshi-vs-Polymarket price deviation on genuinely equivalent
  markets, in cents, after fees and spread, before building any trading logic.
- If deviation exists, trade it with maker entries on Kalshi and hold until
  convergence or settlement.
- Add trend/flow features and a learned model only if they beat the raw anchor
  out-of-sample.
- All backtests start at $10 with fractional position sizing, so results are
  comparable to the account we actually have.

## Non-goals

- Market making as a service. We are not quoting both sides for liquidity
  rewards; that needs $10k-scale inventory and two-sided obligations.
- Arbitrage execution across both venues. We cannot hold a Polymarket position.
- Latency games. We will be slow; anything that needs sub-second reaction is out
  of scope by construction.
- Beating the 15-minute crypto fee wall. If Milestone 0 says there is no
  deviation, 15M crypto is retired rather than tuned further.

## The three approaches, built in order

**A. Hard anchor (baseline, no ML).**
Polymarket mid is the fair value F. Trade Kalshi when
`|Kalshi mid − F| > fees + spread + buffer`. Hold to convergence or settlement.
This is the measuring stick. If A cannot be made profitable in backtest, B and C
have nothing to add.

**B. Anchor plus flow.**
Same F, plus Polymarket order-flow imbalance and recent drift as a direction
filter. Rule-based, a handful of thresholds, no training. Tests whether flow
carries information the price does not.

**C. Learned anchor.**
A model over (Polymarket price, flow imbalance, drift, spot features,
time-to-close, category, spread) predicts P(Kalshi YES). Trade when the model's
probability deviates from Kalshi's price beyond costs. The anchor becomes one
feature among several instead of the answer.

**Acceptance rule: C ships only if it beats A out-of-sample on the same
walk-forward folds, by more than its own variance.** Otherwise A ships and C is
recorded as a negative result. This rule is written down before any model is fit
because it is the only thing that stops us fitting noise and calling it alpha.

## Milestone 0 — the measurement that can end the project

Before any trading code, a research script that:

1. Pulls Polymarket markets and Kalshi markets over the same window.
2. Matches them under the equivalence rules below.
3. For each matched pair and each minute, records `Kalshi mid − Polymarket mid`
   in cents, plus both spreads and both fee loads.
4. Reports the distribution of that deviation: mean, standard deviation,
   percentiles, how often it exceeds costs, and how long it persists.

**Kill criteria:** if the cost-adjusted deviation is not positive at a
statistically distinguishable level after matching, the project stops and the
finding is filed. No model is trained on a non-existent edge.

## Market matching — the part that decides whether any of this is real

This is the largest single risk. A study of ~100,000 "equivalent" cross-venue
events found roughly 44% were **not** equivalent (off-by-one strikes, different
tie handling, different settlement sources). A mismatch does not lose an edge; it
loses the whole position.

Rules, all mandatory:

- Match on **exact strike or threshold**, never on approximate wording or a
  title-similarity score.
- Both markets must resolve from the **same underlying reference** at the **same
  timestamp**. If one uses a 15-minute TWAP and the other a spot print, they do
  not match.
- Tie and edge-case handling must be compared explicitly. "Exactly at the strike"
  resolving YES on one venue and NO on the other is a disqualifying difference.
- Keep the raw rules text for every matched pair in the dataset so a human can
  audit any match.
- **Unverifiable means rejected.** No match is better than a guessed match.
- Category prior: crypto price ladders are the easiest to verify mechanically;
  politics and sports wording is the most likely to hide a difference.

Expect the match rate to be low. A low match rate with high confidence beats a
high match rate with 44% poison.

## Data

- **Polymarket prices:** `clob.polymarket.com/prices-history?market=<token>&interval=<i>&fidelity=<m>`
  (verified: ~136 points/day at 10-minute fidelity). Market metadata including
  `clobTokenIds`, `outcomes`, `outcomePrices`, `conditionId`, `volume`,
  `liquidity` from `gamma-api.polymarket.com/markets`.
- **Kalshi prices:** existing `kalshi_client`, plus per-minute bid/ask from the
  HF dataset `papylove/kalshi-15m-data` (15,420 windows already used in sweeps).
  For non-crypto series, collect per-minute quotes ourselves going forward.
- **Spot reference data:** Binance klines (including `data.binance.vision`
  dumps) and Coinbase, to derive fair value independently of both venues.
- **Resolution rules text:** store verbatim per matched pair. This is a required
  field, not a nice-to-have.

A dataset recon is running in parallel on deeper/paid sources (KalshiEye tier
contents, Dune, cross-venue replication packages). Its findings may extend the
data section; they do not change the milestone order.

## Execution (Kalshi only)

Reuse what already exists and works:

- Maker entry via `post_only` limit orders; fractional contracts to Kalshi's 0.01
  lot; no whole-contract flooring.
- Maker exits and settlement are fee-free on the series we trade; only taker legs
  pay. Exits are therefore taker IOC at the bid — a `post_only` sell at the bid
  would cross and be cancelled, making the exit a no-op (this is already fixed in
  `trader.py`).
- `clear_resting_orders()` before closing; Kalshi blocks a close while any order
  rests on the market.
- Essentially unchanged: `risk.py` sizing and drawdown halt, `paper_mode.py`
  ledger seeded at $10, `resilience.py` backoff, `supabase_client.py` logging,
  the 7-layer LLM path with `LLM_EVAL_ENFORCE=false`.

What A adds is one thing: an external fair value and a deviation threshold. The
entry rule becomes `Kalshi price < F − cost − buffer` for the YES side (and the
mirror for NO), replacing the skew/trend gates.

## Sizing and drawdown

- Backtests and dry runs start at $10, per the requirement. Position size as a
  percentage of balance with fractional contracts, so $10 is representable.
- Live sizing stays at `PER_TRADE_PCT=0.05`, `MAX_OPEN_POSITIONS=2`,
  `DRAWDOWN_HALT_PCT=0.08`. These are already conservative and are not loosened
  by this design.
- A position is only opened when the expected edge exceeds costs by the buffer.
  No edge, no trade — a cycle that does nothing is a correct cycle.

## Validation protocol

- **Walk-forward only.** Train on an earlier window, test on the next, roll
  forward. No random shuffles, no test-set peeking, no re-fitting after seeing
  the test result.
- Report per fold: number of trades, win rate, average entry price, **gross edge
  (win% − average entry price)**, net EV per trade, max drawdown, and the
  bootstrap confidence interval on net EV.
- A result is believed only if gross edge is positive and net EV's confidence
  interval excludes zero across folds.
- **Leakage checks** before trusting any feature: settlement values must not be
  present in feature rows; macro data must be as-published, not revised;
  dead markets must not be silently dropped (survivorship).
- Dry run for a meaningful sample before any live cent. The LLM verdict is
  logged, not enforced, because the evidence does not support it as a filter.

## Constraints

- Termux/Android: no sklearn (no wheel, source build fails, no package). numpy
  2.5.4, scipy 1.18.1, pandas 3.0.6, torch 2.15.0 are installed. Logistic
  regression via `scipy.optimize`; torch only if a linear model is insufficient.
  **No new ML dependency.**
- Single shared working tree with concurrent agents: pull before push, no
  destructive git commands, small commits.
- Polymarket is read-only. No Polymarket account, no Polymarket orders, ever.

## What is left to the builder

- Exact cost buffer value, deviation threshold grid, and hold/exit horizon.
- How much history the Polymarket endpoints actually return at finer fidelity,
  and whether that is enough to train C on cross-venue pairs (the running recon
  covers this).
- Whether trend/flow features come from Polymarket trades or from spot order
  flow; B's feature set is deliberately unspecified.
- Model form for C: logistic first, and only escalate if the lift justifies it.
- Whether the 15–55c favourite-maker band is retired outright or kept as a
  fallback when no anchor deviation exists.
