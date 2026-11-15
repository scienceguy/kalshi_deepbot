# Kalshi Trading Strategy Research Report

**Project:** kalshi_superbot  
**Date:** Sun Nov 01 2026  
**Scope:** 15-minute binary markets, fee structure, microstructure, risk management, backtesting, API rate limits, parameter optimization

---

## Executive Summary

This report synthesizes research from 15+ sources spanning official Kalshi documentation, third-party analytics (Turbine, Bot for Kalshi, PredictionMarketPicks/The 7 Oracles), academic micro-structure papers, and community discussion. The central, repeated finding is that **Kalshi's 15-minute crypto markets are efficiently priced**: measured gross edges are small (sub-cent per contract) and the taker fee frequently consumes or exceeds them. Strategies that win require either (a) passive maker execution avoiding the taker fee, (b) entry bands tightly tuned to the fee curve, or (c) an information edge on the settlement index that exceeds the fee drag. No strategy reviewed demonstrates a reliably positive edge after fees across a large, independent out-of-sample test.

---

## 1. 15-Minute Binary Market Trading Strategies

### 1.1 Buying the side that is ahead (favorite strategy)

**Key insight:** In KXBTC15M, the side priced above 50¢ is genuinely, if slightly, underpriced. Measured at the midpoint, the leading side won 0.3–1.8 percentage points more often than its price implied across price bands from 50¢ to 99¢ (about one point on average). At the **ask** (what a taker actually pays), the edge shrinks to about **0.67¢ per contract** before fees — but the taker fee averages about **1.55¢** (whole-cent rounding) or **1.09¢** (unrounded), leaving a net of roughly **−0.42¢ to −0.88¢ per contract**. Across 6,298 settled windows (June 9–Aug 15, 2026), no entry time showed a reliable profit after fees.

- Best pre-fee result: entry at 8 minutes left, net +0.02¢ (unrounded fee), t = +0.04 — not distinguishable from zero.
- Worst pre-fee result: entry at 14 minutes left, net −1.31¢ (unrounded).
- A short 7-day sample (662 windows) showed +1.89¢ at the 10-minute entry; at full sample it was −0.54¢. Small samples mislead.
- A live account running this strategy made $10.42 across 40 windows in one August night — variance, not edge.

**Source:** https://www.botforkalshi.com/blog/kalshi-15-minute-bitcoin-markets (third-party analysis, Bot for Kalshi, Oct 20 2026)

### 1.2 The 90–99¢ "sure thing" near the close

**Key insight:** Buying 80–99¢ favorites near the close wins most of the time, but the price is fair, not a bargain. At 95–99¢, the leader won 96.75% of the time at an average price of 96.74¢ — essentially fair pricing. Before fees, the 90–99¢ band earned +0.26¢ per contract (within noise, SE 0.33¢); after any fee it is negative. One losing window costs ~92¢, erasing gains from ~13 wins. At 95–99¢, one loss erases ~30 wins.

- The win rate matched price almost exactly across bands: 80–84¢ won 79.0% vs. implied 82.1¢; 90–94¢ won 92.5% vs. 92.4¢; 95–99¢ won 97.1% vs. 97.0¢.
- All late-window cells were within 1.9 standard errors of fair pricing.

**Source:** https://www.botforkalshi.com/blog/kalshi-15-minute-bitcoin-markets (third-party analysis, Bot for Kalshi, Oct 20 2026)

### 1.3 Resting limit orders (maker / favorite-maker)

**Key insight:** Because makers pay no fee on KXBTC15M, resting limit orders at the bid turn the buying-the-leader result positive — about **+1.49¢ per contract** as a ceiling. However, this assumes every resting order fills. Real resting orders do not fill at random:

- Paper test (July 19 – Aug 3, 2026): resting on the favorite at up to 57¢ across nine 15-min crypto series. On NEAR/DOGE/SOL/ZEC (815 contracts), fills averaged 49.5¢ and won only 45.3% vs. 49.5% implied — Paper lost ~5.87¢/contract.
- 207 live contracts lost ~5.79¢/contract.
- Root cause: **adverse selection** — a resting order on the favorite fills only when someone sells down to it, which tends to happen as that side stops being the favorite. The fills delivered coin flips that had just turned against them.

**Bottom line:** The +1.49¢ ceiling is not an expected result. Resting orders change *which* trades you get, not just whether you pay a fee.

**Source:** https://www.botforkalshi.com/blog/kalshi-15-minute-bitcoin-markets (third-party analysis, Bot for Kalshi, Oct 20 2026)

### 1.4 The 12:30 Rule — debunked

**Key insight:** A popular rule says to stop buying 80¢+ favorites after the 12:30 mark in Kalshi's 15-minute bitcoin windows, claiming liquidity fades and "gamma explodes." Tested on 1,258 windows (Oct 13–27, 2026) with order-book captures every 5 seconds:

| Stretch | Entries | Won | Paid | Net equal-weight | Net contract-weighted |
|---|---|---|---|---|---|
| Before 12:30 | 2,563 | 90.3% | 90.4¢ | −0.76¢ | −0.49¢ |
| 12:30–14:00 | 1,373 | 93.7% | 92.8¢ | +0.43¢ | −0.31¢ |
| Final 60 seconds | 538 | 97.1% | 94.1¢ | **+2.67¢** | **+1.90¢** |

- There is **no 12:30 cliff**. Buying every favorite blind at the ask is a fee-sized drag at every clock mark (5:00 through 14:45), all within 1.6 standard errors of zero.
- The final minute was the **best** stretch for favorites: takers netted +2.67¢ per contract after fees.
- **Why:** KXBTC15M settles on the average of 60 one-second CF Benchmarks prints in the final minute. With 30 seconds left, half the prints are already locked in; remaining uncertainty scales with r^1.5, not √r. At 30 seconds left it is ~71% smaller than a last-tick settle would be. A trader pricing as "spot vs. strike, volatility × √time" overstates late risk — that trader sells the favorite too cheap.
- Liquidity: top-of-book size thins ~81% from minute 13 to the last full minute, but spreads do **not** widen (median spread drops from 1¢ to 0.1¢ as prices reach the tails). Spreads over 3¢ stay under 1% even in the final minute.

**Source:** https://predictionmarketspicks.com/articles/kalshi-bitcoin-15-minute-last-60-seconds (third-party analysis, PredictionMarketPicks/The 7 Oracles, Oct 27 2026)

### 1.5 Turbine's five Kalshi strategy configurations

From Turbine's research library (5 strategy families, explicit rules):

| Strategy | Market | Core Rule | Test Evidence | Failure Case |
|---|---|---|---|---|
| Price-threshold entry | KXBTC15M | Buy YES at ask ≤0.35 with >8 min left; profit & close-time exits | Research: buy at 0.50, sell at 0.70 | No exit bid; rule never triggers |
| Mean reversion | KXBTC15M | Buy YES at low extreme (0.25), NO at high (0.75); exit near 0.50 | Library backtest lost money; early-window reversal mixed | Price jumps past exit band; no built-in stop |
| Panic fade | KXBTC15M | Buy YES after sharp drop; exit on recovery | All 100 variants lost in later test | Drop continues; sampling too slow |
| Observation momentum | Any frequent-trade market | Buy in direction of move past threshold; hold to settlement | No library entry yet | No new trades; stale signal repeats |
| Market making (spread capture) | KXBTC15M | Post-only quotes both sides when spread wide enough | Library: Layered Maker Spread Floor | One side fills; price moves against inventory |

**Key cautions from Turbine:**
- 5,732 completed backtests: 2,154 positive P&L, 2,496 negative, 1,082 zero, 972 no-trades.
- Panic fade: April study (93 of 96 variants profitable, best +18.32%) — later test of all 100 variants lost (−$27.23 to −$5.39). **Leaderboard results do not survive.**
- Momentum continuation: 100/100 variants positive (+$1,131 to +$3,540) — but on one selected family, one historical window.
- Five-minute/one-hour trend confirmation: 0/100 variants positive; fees consumed 146% of winner's gross P&L.
- Price ceiling sweep: lower ceiling ($0.65) beat higher ceiling ($0.85) on both P&L (+$36.28 vs. +$17.18) and drawdown (−$28.99 vs. −$36.85) despite lower win rate (50.4% vs. 55.5%). **Higher win rate ≠ better outcome.**
- MACD thresholds: strictest config won 76.9% with smallest drawdown (−$3.08) but earned least (+$18.08); modest threshold earned most (+$75.38).

**Source:** https://www.turbinefi.com/blog/5-prediction-market-strategies-you-can-automate-today and https://www.turbinefi.com/blog/5732-backtests-kalshi-bitcoin-15-minute-bots-2026 (third-party analysis, Turbine, Sep–Oct 2026)

### 1.6 NO-side edge / fading the leader

**Key insight:** Buying the trailing side gives up the leader's small underpricing and still pays a fee symmetric around 50¢. In the Bot for Kalshi archive, buying the trailing side at its ask lost about **1.6¢ per contract before fees**, then paid about **1.1¢ more in fees**. NO-side (fade) strategies are structurally worse on this series.

**Source:** https://www.botforkalshi.com/blog/kalshi-15-minute-bitcoin-markets (third-party analysis, Bot for Kalshi, Oct 20 2026)

### 1.7 Safe compounder approaches

**Key insight:** No reviewed strategy demonstrates a safe, reliably positive compounding edge after fees. The closest candidates:

- **Market making / spread capture** (post-only resting orders): avoids taker fee but carries inventory risk — one side fills and price moves against exposure. Turbine notes Kalshi replay is supported but queue position and live execution are limitations.
- **Final-minute favorite lane** (PredictionMarketPicks): pre-registered paper strategy buying the market favorite in the final 60 seconds only when the locked share of the 60-print average makes it underpriced after fee, once per window, never fading. Pass line: 300 graded paper fills, ≥+1.0¢/contract net, positive 90% lower bound, both halves positive. Live would run at $3 max/position with a $25 daily loss stop. Backtest on 14 days: 161 windows fired, won 98.1%, netted +3.81¢/contract after fees (SE 1.10). **Still in paper testing as of Oct 27 2026; not yet live.**

**Source:** https://predictionmarketspicks.com/articles/kalshi-bitcoin-15-minute-last-60-seconds (third-party analysis)

---

## 2. Fee Structure and Profitability Impact

### 2.1 Core formula (official)

**Taker fee:** `raw = M_taker × 0.07 × C × P × (1 − P)`
**Maker fee:** `raw = M_maker × 0.0175 × C × P × (1 − P)`

Where:
- C = contract count
- P = execution price in dollars (50¢ = 0.50)
- M = series- and role-specific multiplier (default taker M=1, maker M=0)

The 0.07 coefficient is **not** a flat 7% charge. At 50¢, 100 contracts cost $50 before fees; the $1.75 taker fee is **3.5% of stake** but **1.75% of maximum payout**. Always state the denominator when comparing fee percentages.

**Fee peaks at 50¢** because `P × (1 − P)` peaks at 0.50. The coin-flip contracts that feel most tradeable are the most expensive to trade:

| Contract price | Purchase cost | Taker fee (100 contracts) | Fee / purchase cost |
|---|---|---|---|
| 5¢ | $5.00 | $0.33 | 6.65% |
| 25¢ | $25.00 | $1.31 | 5.25% |
| 50¢ | $50.00 | $1.75 | 3.50% |
| 75¢ | $75.00 | $1.31 | 1.75% |
| 90¢ | $90.00 | $0.63 | 0.70% |
| 95¢ | $95.00 | $0.33 | 0.35% |

A lower dollar fee does **not** make a contract better value. A 95¢ contract can lose its entire purchase cost; a 5¢ contract's small dollar fee is a large percentage of its stake.

### 2.2 Rounding and precision

- Kalshi rounds fee plus position cost to a **centicent ($0.0001)**.
- Whole-cent display tables round up: one 50¢ contract at taker M=1 → raw $0.0175 → total $0.5175 → fee remains 1.75¢, but whole-cent display shows 2¢.
- Actual fill accounting: six-decimal trade fees, balance alignment at $0.0001 for direct members / $0.01 for broker-cleared members, with a running accumulator for rounding overpayment across an order's fills.
- **Do not round each contract separately** or assume a calculator reproduces every partial fill or FCM charge.

### 2.3 Maker vs. taker distinction

- A **taker fill** matches an order already on the book. A limit order priced at or through the ask is still a taker fill.
- A **maker fill** comes from an order that first rests and is matched later. An unfilled resting order incurs no execution fee; canceling it creates no trading fee.
- Some series charge maker fees when resting orders fill; KXBTC15M's maker multiplier is 0 (makers pay nothing).
- For 100 contracts at 50¢: taker = $1.75; maker M=1 = $0.44; maker M=0 = $0.
- A limit order alone is **not** evidence of a maker fill. Inspect the role of each fill.

### 2.4 Break-even example

Buying 20 contracts at 60¢, taker M=1, $0.0001 precision: total cost $12.3360 (including $0.3360 entry fee).
- Winning settlement: $20 payout − $12.3360 = **$7.6640 net profit**
- Losing settlement: $0 − $12.3360 = **$12.3360 loss**
- **Break-even probability: 61.68%** ($12.3360 ÷ $20) — a cost threshold, not an estimate of event likelihood.

### 2.5 Settlement, deposits, withdrawals

- No settlement fee for simple yes/no outcomes. Holding to close costs nothing extra; fee paid at trade time.
- No Kalshi ACH deposit/withdrawal fee. Card deposits max 2%. Crypto transfers may carry third-party processor fees.

### 2.6 Profitability implication

**Fees can consume most of a visible price move.** Buying 100 contracts at 50¢ and selling all at 55¢ produces $5.00 gross price profit. Taker fees on both trades: $1.7500 + $1.7325 = **$3.4825**, leaving **$1.5175 net** — ~70% of the gross move gone to fees.

**Sources:**
- Official Kalshi fee schedule (PDF): https://kalshi.com/docs/kalshi-fee-schedule.pdf (effective July 7, 2026) — **official**
- Official Kalshi help center fees: https://help.kalshi.com/en/articles/13823805-fees — **official**
- Bot for Kalshi fees explained: https://www.botforkalshi.com/blog/kalshi-fees-explained (third-party, Sep 16 2026)
- SailGP/Kalshi fees review: https://sailgp.com/prediction-markets/kalshi/fees (third-party, Oct 16 2026)
- CFTC filing of fee schedule: https://www.cftc.gov/sites/default/files/filings/orgrules/22/09/rule091222kexdcm003.pdf — **official/regulatory**

---

## 3. Market Microstructure: Order Book Dynamics, Bid-Ask Spreads

### 3.1 KXBTC15M liquidity profile

- **Volume:** median ~2.9 million contracts per window; 10% trade <1.7M, 10% >3.9M. One window (Oct 20, 12:00 close) printed 43,600 separate trades.
- **Trade frequency:** median 16.5 trades/second in June 2026 sample.
- **Depth:** best bid and best ask each showed ~2,000 contracts; a one-contract order nearly always finds a counterparty at the best price.
- **Spread:** average quoted spread ~1.1¢ near the open, ~0.6¢ in the last minutes (finer 0.1¢ steps above 90¢).
- **Price steps:** 1¢ from 10¢ to 90¢, 0.1¢ below 10¢ and above 90¢.
- **Price traveled:** median 50¢ within a window; a traded price level typically lasted ~1 second. Second-by-second moves showed **no momentum to follow and no reversal to fade**.
- A new resting order at best price waits behind ~2,000 contracts.

### 3.2 Liquidity by minutes left (KXBTC15M)

| Minutes left | Median spread | Average spread | Spread >3¢ | Median thinner side | Avg 5-sec spot move |
|---|---|---|---|---|---|
| 13 | 1.0¢ | 1.01¢ | 0.1% | 3,068 | 0.72 bp |
| 10 | 1.0¢ | 0.94¢ | 0.0% | 2,753 | 0.69 bp |
| 5 | 1.0¢ | 0.70¢ | 0.1% | 1,465 | 0.63 bp |
| 3 | 1.0¢ | 0.58¢ | 0.1% | 1,074 | 0.63 bp |
| 2 | 0.1¢ | 0.52¢ | 0.0% | 901 | 0.62 bp |
| 1 | 0.1¢ | 0.48¢ | 0.1% | 586 | 0.59 bp |
| 0 (final minute) | 0.1¢ | 0.56¢ | 0.8% | 561 | 0.63 bp |

Depth at top thins ~81% from minute 13 to the last full minute. Spreads do **not** widen. Spot is not jumpier late (0.59–0.63 bp/5s in last three minutes vs. 0.69–0.76 early).

### 3.3 Academic microstructure research

- **GWU (Bürgi et al., 2026):** "Makers or Takers: The Economics of the Kalshi Prediction Market" — models matching between makers and takers and illustrates how it endogenously produces observed bid-ask spreads. Cited by 59.
- **SSRN:** "A Microstructure Perspective on Prediction Markets" — examines Kalshi orderbook dynamics using NFL regular season trade-level data.
- **Metamask.io:** "5-cent spreads in prediction markets: liquidity, costs" — notes Kalshi processed ~$24.99B; 5-cent bid-ask spread affects liquidity and trading costs.
- **YMCA Whittier:** "The Liquidity-Accuracy Trade-Off" — contracts with spreads of 5–10 points and <$10,000 combined bids/asks are thin.
- **GitHub (gordonkoehn/prediction-market-microstructure):** analyzes bid-ask spreads and trading volume; Kalshi Direct API + crypto exchanges.
- **CryptoStruct:** Kalshi historical data download — tick-by-trades, L2 order-book depth, 1-min OHLCV/VWAP and bid-ask spreads for 134,643 Kalshi instruments.

### 3.4 Settlement microstructure (KXBTC15M)

- Settles on CF Benchmarks Bitcoin Real-Time Index (BRTI), not a single exchange.
- In the last minute before expiration, Kalshi collects 60 index values (one per second) and averages them, rounded to two decimals.
- Median settlement: 3.8 seconds after close; 90% within 6.8 seconds. One window took ~2 hours (contract terms allow latest expiration a week out with review).
- Median distance between target and final value: 0.068% (~$56 at $82,500 BTC). Only 17 of 1,000 windows moved 0.5% or more.
- "At least" wording: ties go to YES (rare at two-decimal precision; none in 1,000 windows).
- YES won 502 of 1,000 October windows; NO won 498 — a coin flip at the open.

**Sources:**
- Bot for Kalshi 15-min BTC article: https://www.botforkalshi.com/blog/kalshi-15-minute-bitcoin-markets (third-party, Oct 20 2026)
- PredictionMarketPicks 12:30 rule article: https://predictionmarketspicks.com/articles/kalshi-bitcoin-15-minute-last-60-seconds (third-party, Oct 27 2026)
- GWU microstructure paper: https://www2.gwu.edu/~forcpgm/2026-001.pdf (academic, 2026)
- SSRN microstructure: https://papers.ssrn.com/sol3/Delivery.cfm/6325658.pdf (academic)
- Metamask spread article: https://metamask.io/news/5-cent-spread-prediction-markets (third-party)
- CryptoStruct historical data: https://cryptostruct.com/exchanges/kalshi (third-party data vendor)
- Kalshi crypto settlement explainer: https://help.kalshi.com/en/articles/13823838-crypto-markets (official)

---

## 4. Risk Management: Position Sizing, Drawdown Limits, Daily Loss Limits

### 4.1 Kelly Criterion for binary contracts

**Formula:** `f* = (p × b − q) / b`
- f* = fraction of bankroll to bet
- p = estimated probability of winning
- q = 1 − p
- b = net payout ratio = (payout / cost) − 1

**Example:** 65% win probability, YES at 50¢ → b = ($1/$0.50) − 1 = 1.0 → f* = (0.65 × 1.0 − 0.35) / 1.0 = **0.30** (30% of bankroll on one trade).

### 4.2 Why full Kelly is too aggressive

Full Kelly maximizes long-run log growth only if assumptions hold: (1) probability estimate exactly correct (never true), (2) trader can handle massive drawdowns (full Kelly produces 50%+ drawdowns regularly), (3) infinite number of trades (need to survive short run). Overestimating edge by even a small amount turns Kelly into a recipe for ruin.

### 4.3 Half-Kelly: practical standard

- Produces **75% of the growth rate** of full Kelly
- Substantially less variance and smaller drawdowns
- More robust to errors in probability estimates
- Fractional Kelly reduces stake and theoretical growth but does **not** guarantee survival or repair a misestimated probability.

### 4.4 Practical caps (Bot for Kalshi recommendation)

- Never more than **5% of bankroll** on a single trade
- Never more than **15% of bankroll** in a single market category
- Never more than **30% of bankroll** at risk simultaneously

### 4.5 Kelly doesn't apply when

- Bets are correlated (e.g., multiple props from same game) — Kelly overestimates optimal size; need portfolio-level Kelly or further adjustment.
- Edge estimate is just order-book noise — sizing precisely is sizing nothing; confirm edge is real first.

### 4.6 Position sizing from Turbine backtests

- Smaller entries (5 vs 10 contracts) with wider bands recorded less drawdown (−$91.40 vs −$160.69) but also less P&L (+$2,252 vs +$2,781).
- Trade counts changed substantially with size without confirmed explanation — effective entry boundary needs verification.
- Stop-loss should be no more than fractional Kelly amount; total trade size not more than full Kelly.

### 4.7 Drawdown and loss-limit principles (general trading, adapted to Kalshi)

- Risk 1–2% of account equity per trade (classic rule)
- A 50% drawdown requires a 100% gain to recover
- Daily loss limits and max drawdown caps are essential backstops
- Separate trade review from trade sizing — never size up to recover a loss (martingale behavior)
- Account-level daily loss cap as backstop for discipline slips
- Kill switch, hard position limits, and alerting on anomalous behavior for automated bots

### 4.8 Bot-for-Kalshi risk guardrails (from 10 mistakes article)

- Per-bot stop-loss and take-profit levels
- Account-level daily loss cap and position limits
- All visible before arming a bot
- A stop requests an order; it does **not** guarantee a price, fill, or loss ceiling

**Sources:**
- Bot for Kalshi Kelly criterion: https://www.botforkalshi.com/blog/kalshi-kelly-criterion-position-sizing (third-party, May 22 2026)
- Bot for Kalshi 10 mistakes: https://www.botforkalshi.com/blog/kalshi-trading-mistakes (third-party, Sep 14 2026)
- Turbine 5,732 backtests: https://www.turbinefi.com/blog/5732-backtests-kalshi-bitcoin-15-minute-bots-2026 (third-party, Sep 29 2026)
- General risk management (non-Kalshi-specific): tradealgo.com, tradezella.com, quantvps.com, crosstrade.io (third-party educational)

---

## 5. Backtesting Methodology for Kalshi Binary Markets

### 5.1 Data sources

| Source | What it offers | Coverage |
|---|---|---|
| Kalshi official historical data API | Current and recent data via live endpoints; historical tier for older data | Event contracts |
| Kalshi GET /account/endpoint_costs | Authoritative non-default endpoint costs | API |
| Allium.so | Kalshi historical data guide — market and settlement records | Research |
| CryptoStruct | Tick-by-tick trades, L2 order-book depth, 1-min OHLCV/VWAP, bid-ask spreads for 134,643 Kalshi instruments | Data vendor |
| LycheeData | Complete Kalshi market history, trades, prices, outcomes, categories, orderbook behavior | Data vendor |
| arXiv PredictionMarketBench | SWE-bench-style framework targeting Kalshi data | Academic |
| Kalshi docs: Historical Data | Live vs. historical tier partitioning | Official docs |

### 5.2 Common backtesting pitfalls

1. **Look-ahead bias:** using information not available at entry time.
2. **Overfitting:** selecting best historical result from a large grid; collapses out-of-sample. Turbine: "A parameter grid that never changes the trades is not evidence that a strategy is robust."
3. **Fee omission:** modeling gross edge and discovering fees afterward — the #1 mistake that turns a working strategy into a losing account.
4. **Same-candle fills:** older Turbine model (1,000-strategy study) used same-candle fills without fees or liquidity caps — not comparable to later studies.
5. **Execution model inconsistency:** artifacts mix official API data, archived order books, and Turbine-derived execution events. Maker studies use different assumptions from directional taker strategies.
6. **Partial/mixed data coverage:** many reports requested 30 days but recorded partial coverage; publication date ≠ data window end.
7. **No holdout test:** some reports scramble external-feed timing while keeping market prices anchored — asks whether feed timing matters, does not validate on untouched future window.
8. **Selection bias / p-hacking:** Turbine's BTC maker sweep had 26/100 positive variants but permutation test failed; best result not distinguishable from lucky selection.
9. **Short price history:** Kalshi's price history is short relative to traditional markets; a strategy testing well over a couple of years may be fitted to one macro regime.
10. **Extreme-price fills / small samples:** late-conviction study had 40/100 positive variants but p=0.248 and extreme-price fills flagged — green leaderboard alone didn't settle the question.

### 5.3 Turbine backtesting workflow

1. Define market, signal, entry band, exit, position limit
2. Backtest supported rules with testing guide
3. Inspect paper run
4. Include modeled fees and fills in every comparison
5. Report requested and actual data window, active trades, modeled costs, drawdown, failures
6. Test changes on another window where available

### 5.4 What backtests do NOT establish

- Live fill quality or a deployable edge
- A holdout-tested winner (most lack one)
- Thousands of independent strategies (count includes related parameter cells, repeated validations, inactive configs)
- Performance on Bitcoin 15-min contracts for ETH-leading studies (two BTC-velocity-to-ETH studies: one positive on small samples, one lost all 100 variants)

**Sources:**
- Turbine 5,732 backtests: https://www.turbinefi.com/blog/5732-backtests-kalshi-bitcoin-15-minute-bots-2026 (third-party, Sep 29 2026)
- Turbine 5 strategies: https://www.turbinefi.com/blog/5-prediction-market-strategies-you-can-automate-today (third-party, Mar/Oct 2026)
- Kalshi docs: Historical Data: https://docs.kalshi.com/getting_started/historical_data (official)
- Allium Kalshi historical data guide: https://allium.so/blog/kalshi-historical-data-a-practical-guide/ (third-party)
- CryptoStruct: https://cryptostruct.com/exchanges/kalshi (third-party)
- LycheeData: https://lycheedata.com/kalshi-historical-data (third-party)
- PredictionMarketBench: https://arxiv.org/html/2602.00133v1 (academic)
- Backtesting pitfalls (general): https://www.linkedin.com/posts/rmp99_3-backtesting-pitfalls-that-ruin-your-trading-activity-7439369922931134464-7MHV (third-party)

---

## 6. Kalshi API Rate Limits and REST Best Practices

### 6.1 Token-based limits (official docs)

Every authenticated request costs **tokens**. Tier sets **budget** = tokens per second refill rate. Sustained rate = budget ÷ cost. Most requests cost default **10 tokens**.

### 6.2 Read and Write buckets (independent)

| Bucket | Covers |
|---|---|
| Read | GET endpoints and anything not routed to Write |
| Write | Order placement, amends, cancels, order groups, RFQ quote flow, block trade proposal accepts |

Split is by operation type, not protocol. REST and FIX drain the same buckets.

**GET /portfolio/balance** is metered in its own bucket (not Read). Basic/Advanced: once per second; higher tiers: 5 per second. Bucket holds one second of budget (no burst). Balance only changes on fills, settlements, transfers — react to those (e.g., user fills websocket channel) rather than polling.

### 6.3 Exchange sharding (Write budgets)

- Single REST order targeting `exchange_index >= 1` bills that shard's Write bucket. Shard 0 bills unscoped Write bucket. Auto-routed (`exchange_index: -1` or omitted) bills **every** shard's Write bucket.
- FIX: `ExDestination` (tag 100) ≥1 bills that shard; 0/-1/omitted bills unscoped.
- RFQ and quote writes bill shard 1 Write bucket.
- **Batch REST** creates/cancels bill total per-order cost to unscoped Write bucket regardless of exchange_index. Batch of 25 orders = 25 × 10 = 250 tokens; batch of 25 cancels = 25 × 2 = 50 tokens. Whole batch must fit in bucket at once or entire batch rejected.

### 6.4 Bucket capacity and bursting

- Basic/Advanced Read buckets and Write buckets above Basic: hold up to **3 seconds of budget** (3× burst).
- Above-Advanced Predictions Read, Perps Read, Basic Write: hold 1 second (no idle banking beyond that).
- Example: Premier Predictions Write bucket refills at 1,200 tokens/sec, capacity 3,600 → sustains 120 orders/sec at default 10-token cost.

### 6.5 Rate-limit response

- `429 Too Many Requests` with body `{"error": "too many requests"}`
- No `Retry-After` or `X-RateLimit-*` headers; no penalty/cooldown. Bucket keeps refilling. Apply exponential backoff on 429.
- At 1,200 tokens/sec refill, empty bucket replenishes 10 tokens for an order in ~8.3 ms.

### 6.6 Tier budgets (Predictions)

| Tier | Read budget | Write budget |
|---|---|---|
| Basic | 200 | 100 |
| Advanced | 300 | 300 |
| Expert | 600 | 600 |
| Premier | 1,200 | 1,200 |
| Paragon | 2,400 | 2,400 |
| Prime | 4,800 | 4,800 |
| Prestige | 12,000 | 9,600 |

Write bucket capacity = 3× per-second budget above Basic tier.

### 6.7 Tier qualification

- Basic: complete account signup
- Advanced: call Upgrade Account API Usage Level endpoint
- Expert–Prestige: earned from volume or assigned manually
- Volume share = trailing 30-day volume ÷ (previous month exchange volume × 2)
- Grants last 30 days; daily review renews while qualifying. Earn vs. Keep thresholds:

| Tier | Earn | Keep |
|---|---|---|
| Expert | 0.075% | 0.05% |
| Premier | 0.125% | 0.10% |
| Paragon | 0.25% | 0.20% |
| Prime | 0.50% | 0.40% |
| Prestige | 1.00% | 0.80% |

### 6.8 WebSocket connection limits

| Tier | Max concurrent connections |
|---|---|
| Basic–Expert | 200 |
| Premier | 500 |
| Paragon | 750 |
| Prime–Prestige | 1,000 |

Predictions and Perps WS connections share per-user limit. New handshakes return 429 at limit.

### 6.9 REST best practices

- Use REST for setup, snapshots, order placement, cancellation, fills, account state.
- Use WebSockets when strategy depends on live data.
- Optimize REST usage with bulk requests and local caching.
- Don't poll balance; react to fills/settlements via websocket.
- Batch operations don't save tokens — each item billed separately.
- Verify non-default endpoint costs via `GET /account/endpoint_costs`.
- Fetch tier/limits via `GET /account/limits`.

**Sources:**
- Official Kalshi rate limits docs: https://docs.kalshi.com/getting_started/rate_limits (official)
- Batch Create Orders V2: https://docs.kalshi.com/api-reference/orders/batch-create-orders-v2 (official)
- Bot for Kalshi API guide: https://www.botforkalshi.com/blog/kalshi-api-tutorial (third-party)
- Kairos Kalshi API guide: https://kairos.trade/compare/kalshi-api (third-party)
- QuantVPS Kalshi order book API: https://www.quantvps.com/blog/kalshi-order-book-api-endpoints-explained (third-party)
- PredictAndProfit rate limits: https://predictandprofit.io/blog/kalshi-api-rate-limits (third-party)

---

## 7. Parameter Optimization: Entry Bands (75–95¢), Kelly Sizing, Stop Loss

### 7.1 Entry band optimization (Turbine price ceiling sweep)

Four configurations, only entry ceiling changed (Coinbase VWAP momentum):

| Entry ceiling | Simulated P&L | Trades | Win rate | Max drawdown |
|---|---|---|---|---|
| $0.65 | +$36.28 | 3,448 | 50.4% | −$28.99 |
| $0.70 | +$21.58 | 3,952 | 51.1% | −$32.41 |
| $0.75 (baseline) | +$23.79 | 4,407 | 52.3% | −$30.19 |
| $0.85 | +$17.18 | 5,200 | 55.5% | −$36.85 |

**Finding:** Highest ceiling produced most trades and highest win rate but **lowest P&L and largest drawdown**. $0.65 ceiling finished ahead on both P&L and drawdown despite winning barely half its trades. Buying a more likely winner at a higher price leaves less room to earn enough on each win. **Not established as universal** — four comparisons, no permutation test.

### 7.2 MACD threshold optimization

| 1m histogram / 5m histogram | Simulated P&L | Trades | Win rate | Max drawdown |
|---|---|---|---|---|
| 0 / 0 | +$54.61 | 1,697 | 52.8% | −$24.33 |
| 2.5 / 0 | +$75.38 | 1,138 | 57.8% | −$11.51 |
| 5 / 0 | +$69.07 | 774 | 60.7% | −$8.52 |
| 20 / 10 | +$18.08 | 78 | 76.9% | −$3.08 |

**Finding:** Strictest config won 76.9% with smallest drawdown but earned least. Highest P&L came from modest 1m threshold with no 5m threshold. Tradeoff: frequency, net P&L, drawdown alongside accuracy. Higher win rate ≠ better outcome.

### 7.3 Entry size × price band

| Entry size / price band | Simulated P&L | Trades | Win rate | Max drawdown |
|---|---|---|---|---|
| 10 contracts / $0.45–$0.55 | +$2,781.79 | 2,703 | 59.9% | −$160.69 |
| 5 contracts / $0.45–$0.55 | +$2,252.87 | 3,341 | 60.7% | −$91.40 |
| 10 contracts / $0.35–$0.65 | +$4,516.86 | 6,218 | 55.0% | −$312.99 |
| 5 contracts / $0.35–$0.65 | +$4,298.37 | 7,666 | 59.7% | −$148.70 |

**Finding:** Wider bands earned more than narrow bands in both size categories. 5-contract wide-band recorded slightly less P&L than 10-contract with less than half the drawdown. Two issues unresolved: size change also changed trade counts without confirmed explanation; base risk ceiling and wider rule-level band differ, so effective entry boundary needs verification.

### 7.4 Exit type changes entry-signal leaderboard

- With profit/stop exits: early entry led at +$1,166.40; late-momentum candidate lost $39.52 despite 85.0% win rate (different size/spread gate).
- With near-settlement exits: acceleration momentum led at +$1,894.39; VWAP family earned +$1,105.50 but recorded −$1,751.92 drawdown. Positive final P&L hid a rougher path.

**Finding:** Entry signal cannot be evaluated separately from exit and risk rules.

### 7.5 Kelly criterion application to Kalshi

Steps:
1. Estimate probability (the hard part — use models/analysis/domain expertise; overconfidence kills)
2. Calculate Kelly fraction: `f* = (p × b − q) / b`
3. Apply fractional Kelly (0.5 = half-Kelly, 0.25 = quarter-Kelly)
4. Apply practical caps: 5% per trade, 15% per category, 30% total at risk

**Stop-loss guidance:** Stop-loss should be no more than fractional Kelly amount; total trade size not more than full Kelly. A stop-loss capping loss at 5% of position allows larger position than unprotected trade.

### 7.6 The 75–95¢ entry band question

No reviewed source specifically optimizes a 75–95¢ band as a standalone parameter. Relevant findings:

- Buying leaders at 90–99¢: net negative after fees (−0.74¢ whole-cent, −0.19¢ unrounded).
- Buying leaders at 95–99¢: −1.00¢ whole-cent, −0.22¢ unrounded.
- Final-minute favorites (PredictionMarketPicks): +1.22¢ at 95–99¢ band in final 60 seconds (the one band/stretch showing positive net after fees).
- Price ceiling sweep: lower ceilings ($0.65–$0.75) outperformed $0.85 — suggesting entries below 75¢ may be preferable to 75–95¢ for momentum strategies.

**Sources:**
- Turbine 5,732 backtests: https://www.turbinefi.com/blog/5732-backtests-kalshi-bitcoin-15-minute-bots-2026 (third-party, Sep 29 2026)
- Bot for Kalshi Kelly criterion: https://www.botforkalshi.com/blog/kalshi-kelly-criterion-position-sizing (third-party, May 22 2026)
- Bot for Kalshi 15-min BTC: https://www.botforkalshi.com/blog/kalshi-15-minute-bitcoin-markets (third-party, Oct 20 2026)
- PredictionMarketPicks 12:30 rule: https://predictionmarketspicks.com/articles/kalshi-bitcoin-15-minute-last-60-seconds (third-party, Oct 27 2026)

---

## Cross-Cutting Conclusions

1. **Fees dominate thin edges.** The KXBTC15M leader's measured edge (~0.67¢/contract at ask) is smaller than the taker fee (~1.09–1.55¢). Any strategy must either avoid the taker fee (maker/post-only) or have an edge exceeding ~1¢/contract.

2. **No strategy has a proven out-of-sample edge.** Turbine's 5,732 backtests show ~37.6% of variants profitable, but this is a description of the research collection, not a forecast. April's panic-fade leader (93/96 profitable) lost in a later test (100/100 lost).

3. **Higher win rate ≠ better outcome.** The $0.85 price ceiling had the highest win rate (55.5%) but lowest P&L and largest drawdown. MACD's strictest config won 76.9% but earned least.

4. **The final minute is special.** KXBTC15M's 60-print settlement average means uncertainty collapses ~71% by 30 seconds left. The 12:30 rule is debunked; the final minute was the best stretch for favorites (+2.67¢/contract after fees).

5. **Adverse selection kills passive orders.** Resting limit orders avoid fees but fill on the wrong trades — Bot for Kalshi's paper/live tests lost ~5.8¢/contract resting on favorites.

6. **Kelly must be fractional and capped.** Full Kelly produces 50%+ drawdowns. Half-Kelly gives 75% of growth with much less variance. Apply hard caps (5%/trade, 15%/category, 30% total).

7. **API design implications:** Use token buckets wisely (3-second burst above Basic), don't poll balance (use websocket fills), batch operations don't save tokens, exponential backoff on 429, prefer WebSockets for live data.

---

## Recommended Next Steps for kalshi_superbot

1. **Model fees precisely** in the backtest engine using the unrounded formula with centicent precision, not whole-cent estimates.
2. **Test maker/post-only execution** on KXBTC15M given its maker multiplier of 0, but model adverse selection and fill probability, not 100% fill assumption.
3. **Focus on final-minute favorite lane** — the only setup showing positive net after fees in reviewed research (+1.22¢ at 95–99¢ final minute), but require pre-registered pass line before live deployment.
4. **Implement Kelly with fractional sizing (half or quarter) and hard caps** (5%/trade, 15%/category, 30% total).
5. **Add daily loss stop and kill switch** for any automated strategy.
6. **Use WebSockets for live market data**, REST for setup/snapshot, respect token bucket limits with exponential backoff.
7. **Backtest on multiple windows**, not one 30-day cohort; require out-of-sample validation before live deployment.

---

## Source Classification Summary

| # | Source | URL | Type |
|---|---|---|---|
| 1 | Kalshi Fee Schedule (PDF) | https://kalshi.com/docs/kalshi-fee-schedule.pdf | Official |
| 2 | Kalshi Help Center: Fees | https://help.kalshi.com/en/articles/13823805-fees | Official |
| 3 | Kalshi Docs: Rate Limits | https://docs.kalshi.com/getting_started/rate_limits | Official |
| 4 | Kalshi Docs: Historical Data | https://docs.kalshi.com/getting_started/historical_data | Official |
| 5 | Kalshi Crypto Settlement | https://help.kalshi.com/en/articles/13823838-crypto-markets | Official |
| 6 | Kalshi Limit Orders | https://help.kalshi.com/en/articles/13823811-limit-orders | Official |
| 7 | CFTC Fee Schedule Filing | https://www.cftc.gov/sites/default/files/filings/orgrules/22/09/rule091222kexdcm003.pdf | Official/Regulatory |
| 8 | Bot for Kalshi: 15-min BTC | https://www.botforkalshi.com/blog/kalshi-15-minute-bitcoin-markets | Third-party |
| 9 | Bot for Kalshi: Fees Explained | https://www.botforkalshi.com/blog/kalshi-fees-explained | Third-party |
| 10 | Bot for Kalshi: Kelly Criterion | https://www.botforkalshi.com/blog/kalshi-kelly-criterion-position-sizing | Third-party |
| 11 | Bot for Kalshi: 10 Mistakes | https://www.botforkalshi.com/blog/kalshi-trading-mistakes | Third-party |
| 12 | Turbine: 5 Strategies | https://www.turbinefi.com/blog/5-prediction-market-strategies-you-can-automate-today | Third-party |
| 13 | Turbine: 5,732 Backtests | https://www.turbinefi.com/blog/5732-backtests-kalshi-bitcoin-15-minute-bots-2026 | Third-party |
| 14 | PredictionMarketPicks: 12:30 Rule | https://predictionmarketspicks.com/articles/kalshi-bitcoin-15-minute-last-60-seconds | Third-party |
| 15 | SailGP: Kalshi Fees | https://sailgp.com/prediction-markets/kalshi/fees | Third-party |
| 16 | GWU Microstructure Paper | https://www2.gwu.edu/~forcpgm/2026-001.pdf | Academic |
| 17 | SSRN Microstructure | https://papers.ssrn.com/sol3/Delivery.cfm/6325658.pdf | Academic |
| 18 | Allium Kalshi Data Guide | https://allium.so/blog/kalshi-historical-data-a-practical-guide/ | Third-party |
| 19 | CryptoStruct Kalshi Data | https://cryptostruct.com/exchanges/kalshi | Third-party (data vendor) |
| 20 | PredictionMarketBench | https://arxiv.org/html/2602.00133v1 | Academic |

---

*End of report. All figures are historical measurements or simulations, not forecasts or trade recommendations. Trading prediction-market contracts involves substantial risk, including loss of the entire amount invested.*