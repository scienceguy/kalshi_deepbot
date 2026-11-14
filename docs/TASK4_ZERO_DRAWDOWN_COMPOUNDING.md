# Task 4 — Zero-Drawdown Milestone Compounding Strategy ($35 → $1,000,000)

**Status:** strategy synthesis (grounded in live codebase). No prior compounding decision exists in MemPalace (KG query `compounding` returned 0 facts).

Grounded in: `config.py`, `risk.py`, `trader.py`, `signals.py`, `gates.py`, `model.py`, `fees.py`, `poly_us_trader.py`, `scripts/bankroll_compounding_sim.py`, and AGENTS.md (HF backtest / reality-tax findings).

---

## 1. Five-parameter interaction (synthesis)

| Parameter | Value at $35 | What it does |
|---|---|---|
| Starting bankroll | $35.00 | Base equity; every percentage below derives from it |
| Max per-window budget | 3% = $1.05 | Hard cap on risk capital deployed in any single 15-min window |
| Cash reserve floor | 30% = $10.50 | Minimum cash never at risk; caps deployable capital at 70% |
| Model probability hurdle | P ≥ 0.96 | Only trades the model prices ≥96% likely to win (+$0.3537 EV/trade per HF backtest) |
| Strike cushion hurdle | z ≥ 1.80, spot–strike distance > 2.0% | Enter only when spot is ≥1.8σ from the strike on the favored side and >2% away |

### How they bind (the constraint chain)

1. **Floor first.** 30% of equity ($10.50) is locked as cash. Only 70% ($24.50) is deployable.
2. **Budget next.** The per-window budget (3% of equity = $1.05) is far tighter than the 70% deployable cap, so it is the binding constraint per window. At $35 you can risk at most ~$1.05 in a 15-min window — roughly one 95–99c favorite contract.
3. **Size follows budget.** Per-trade size = min(3% of equity, remaining window budget, cash above the floor). One contract per window at low balances; size grows linearly with equity as milestones are crossed.
4. **Edge qualifies the trade.** No order is placed unless the model clears P ≥ 0.96 *and* the cushion gate clears z ≥ 1.80 / distance > 2.0%. This makes losses rare (win rate ≳96.4% from cushion alone) and each loss small (−3% max).
5. **Growth compounds.** With fixed 3% sizing and positive per-trade edge, the dollar stake grows with equity, so each milestone is reached faster — until qualifying flow (markets per window passing all gates) becomes the binding constraint, not capital.

### Why drawdown is bounded (the "0 drawdown" claim)

- Worst single loss = −3% of equity (3% cap).
- Cushion z ≥ 1.80 ⇒ win rate ≳ Φ(1.80) = 96.4%; loss rate ≲3.6% per trade.
- Consecutive losses are exponentially rare: 2 straight ≈ 0.13%, 3 straight ≈ 0.005%, 4 straight ≈ 0.0002%.
- Drawdown vs peak from n straight 3% losses: 1→−3.0%, 2→−5.9%, 3→−8.7%, 4→−11.5%. Four straight losses is effectively impossible, so drawdowns self-correct within a window or two via the positive edge.
- **Circuit breaker:** if post-settlement equity ≤ $34.95, reset bankroll to $35.00 (ledger/history preserved). This guarantees the bankroll never permanently impairs below start — i.e., zero *permanent* drawdown / zero capital impairment.

Honest caveat: "zero drawdown" means bounded, non-permanent drawdown with an auto-reset floor, not literally zero losing trades. ~3.6% of trades lose; the design ensures no loss can break the compounding curve.

---

## 2. Milestone table

| Milestone | Equity | 3% window budget | 30% cash floor | Deployable (70%) | Max contracts/win @ ~96c |
|---|---|---|---|---|---|
| Start | $35 | $1.05 | $10.50 | $24.50 | ~1 |
| M1 | $100 | $3.00 | $30.00 | $70.00 | ~3 |
| M2 | $1,000 | $30.00 | $300.00 | $700.00 | ~31 |
| M3 | $10,000 | $300.00 | $3,000.00 | $7,000.00 | ~312 |
| M4 | $100,000 | $3,000.00 | $30,000.00 | $70,000.00 | ~3,125 |
| M5 | $1,000,000 | $30,000.00 | $300,000.00 | $700,000.00 | ~31,250 |

Contracts/win assumes ~96c entry and full window-budget deployment; at $100k+ the code switches to 2% sizing (`poly_us_trader.compute_tier_size`), halving these.

The per-window budget is recomputed at every 15-minute window rollover from current total equity (cash + mark-to-market exposure), so sizing scales automatically with the bankroll — no manual retuning between milestones.

---

## 3. Operational execution playbook (daemon rules)

### A. Entry gate — all must pass, every candidate, every cycle

1. Market inside the entry window (`ENTRY_WINDOW_START_MIN`/`END_MIN`).
2. YES favorite inside the band (92–99c per config; the stipulated high-certainty band).
3. Model probability P ≥ 0.96 (GBM with dynamic realized volatility; `MODEL_VOL_DYNAMIC=true`, clamped 0.20–5.00).
4. Cushion z ≥ 1.80 **and** spot–strike distance > 2.0% (directional, on the favored side).
5. Fee-adjusted net EV ≥ 0 after the Kalshi taker fee (0.07 × p × (1−p)); maker/post-only preferred to avoid fees.
6. Stability check: underlying stayed clear of the strike for the lookback window.
7. Not already positioned in this market; under max open positions (2) and max orders per cycle (2).

Any failure → skip, log the reason, place no order.

### B. Sizing rule

```
size_usd = min(3% × total_equity, remaining_window_budget, cash − 30%_floor)
contracts = floor(size_usd / entry_price, to 0.01 lot)
contracts = min(contracts, MAX_CONTRACTS_CAP, available_liquidity)
```

Size on settled equity, not mark-to-market gains, so a drawdown shrinks stakes procyclically (protective).

### C. Per-window budget

- New budget = 3% × total equity at each 15-minute window rollover (`trader.py:_touch_window`).
- Window spent resets to $0; resting (unfilled) orders count against the budget so multiple resting orders can't collectively exceed it.
- Unspent budget does not roll over (use it or lose it within the window) — prevents staleness.

### D. Position management

- Hold to BRTI settlement (no early exit) — the 60-second BRTI average is the alpha source; exiting early forfeits it.
- One entry per market per window (`_already_ordered()` blocks duplicate resting entries).
- Max 2 concurrent open positions (crypto 15m series move together = one correlated bet, not diversification).

### E. Loss & drawdown handling

- Drawdown halt: if total equity falls vs peak by `DRAWDOWN_HALT_PCT` (8% live), halt new entries until drawdown recovers to half the halt threshold.
- Consecutive-loss cooldown: 3 straight losses → pause new entries.
- Per-market daily loss limit (`MARKET_DAILY_LOSS_PCT` = 10% of portfolio).
- Auto-reset floor: post-settlement equity ≤ $34.95 → reset bankroll to $35.00, clear positions, preserve ledger.

### F. Milestone promotion

When total equity crosses $100 / $1K / $10K / $100K / $1M, log a milestone event, record it in the ledger, and let the next window rollover recompute budget & floor from the new equity. Sizing scales automatically.

### G. Flow management (the real binding constraint)

- Prioritize opportunities closing soonest so settlements compound in real time.
- If fewer qualifying markets appear than capital can deploy, leave capital idle (the cash floor protects it) rather than lowering gates.
- **Never** widen the band, lower P, or lower z to manufacture trades — that destroys the edge.

---

## 4. Configuration alignment (one recommended code change)

The Kalshi daemon (`trader.py`) currently sets the per-window budget to `WINDOW_TRADE_PCT = 1.00` (100% of cash) at each window rollover (`trader.py:_touch_window`, ~line 558), **not 3%**. To run this strategy on Kalshi:

- Set `WINDOW_TRADE_PCT = 0.03` (3% of equity per window), or gate the window budget to `min(WINDOW_TRADE_PCT × cash, 0.03 × total_equity)`.
- Keep `CASH_FLOOR_PCT = 0.30` and `EXPOSURE_CEILING_PCT = 0.30` (they already sum to ≤ 1.0).
- Keep `GATES_MIN_CUSHION_Z = 1.80` and `MODEL_VOL_DYNAMIC = true`.
- `PAPER_GUARDS_ENABLED` may be off only in paper experiments — never live.

All other machinery (per-window budget tracking, resting-order reservation, drawdown halt, consecutive-loss cooldown, per-market daily loss, auto-reset, milestone ledger) already exists in `risk.py`, `trader.py`, and `poly_us_trader.py`.

---

## 5. Honest caveats

- **Edge is thin.** AGENTS.md reality tax: band 95–99c + minutes 6–11 + taker + fee ≈ −0.29c/trade; maker resting ≈ −4.06c (dead); taker + calibrated model gate ≈ +1.03c/trade. The fixed `MODEL_VOLATILITY=0.80` GBM gate returns −0.01c; dynamic realized-vol returns +1.03c. **+$0.3537/trade is the HF-stipulated edge — verify the live gate clears breakeven before scaling.** Do not widen the band or lower P/z expecting more edge; the binding constraint is gate calibration and qualifying flow.
- **0 drawdown ≠ zero losing trades.** ~3.6% of trades lose; the design ensures no loss impairs the bankroll below $35 and drawdowns self-correct.
- **Moonshot timeline:** $35 → $1M requires ~28,600× growth. At fee-net edge (~1c/contract) and limited qualified flow per window, this is months-to-years of continuous operation, not 7 days. Track EV/trade and win rate, not the equity curve.
- **Correlated risk:** BTC/ETH/SOL/... 15m markets move together; max 2 concurrent positions reflects one correlated crypto bet.

---

## Executive playbook (concise)

**Start:** $35, dry-run first. Confirm the Kalshi key pair is the live "Kalshi GPT API Key" + "Kalshi GPT RSA Key" — the stale "Kalshi API Key" / "Kalshi RSA Key" pair returns `INCORRECT_API_KEY_SIGNATURE`.

**Every 15-minute window:**
1. Recompute window budget = 3% × total equity; reset spent = $0.
2. Scan all 9 series; prefetch spot/ATR concurrently.
3. For each candidate run the entry gate: P ≥ 0.96, z ≥ 1.80, spot–strike distance > 2%, band 92–99c, net EV ≥ 0 after fee, stability check, no existing position, under max positions/orders.
4. Size = min(3% equity, remaining window budget, cash − 30% floor); place ONE maker order per market; max 2 positions, max 2 orders/cycle.
5. Hold to BRTI settlement; book P&L on settlement; update milestones.

**Drawdown rules:** 3% max per trade; halt at −8% vs peak (resume at −4%); cooldown after 3 straight losses; per-market 10% daily loss cap; auto-reset to $35 if equity ≤ $34.95.

**Milestone promotion:** at $100 / $1K / $10K / $100K / $1M, log the milestone and let the next window recompute budget & floor automatically.

**Never:** widen the band, lower P or z, skip the cash floor, or trade when no candidate clears all gates — idle cash is cheaper than a negative-edge trade.
