# Task 3 — Automated Capital Milestone Compounding Ladder ($35 -> $1,000,000)

**Applies to:** `poly_us_trader.py` (Polymarket US paper trader).  
**Model:** favourite-maker, hold-to-settlement, 92c-99c band, ~98% win rate, $1.00 payout/contract.  
**Source of trade-count numbers:** `scripts/bankroll_compounding_sim.py` Monte Carlo (8,000 runs, 3% fixed-fraction, q~0.955, p=0.98) — see `scripts/bankroll_compounding_results.json`.

---

## 1. Milestone progression rules (modeled)

The per-window budget is **3% of total equity** (cash + mark-to-market positions), re-computed every cycle and every new 15-minute window. Contracts = `floor(budget / entry_price)` at ~$0.95 entry. No per-tier caps.

| Tier | Bankroll range | Per-window budget (3%) | Contracts @ $0.95 | Cash floor (30%) | Deployable (70%) |
|------|----------------|------------------------|-------------------|------------------|------------------|
| Start | $35 -> $100 | $1.05 | 1 | $10.50 | $24.50 |
| Milestone 1 | $100 -> $1,000 | $3.00 | 3 | $30.00 | $70.00 |
| Milestone 2 | $1,000 -> $10,000 | $30.00 | ~31 | $300.00 | $700.00 |
| Milestone 3 | $10,000 -> $100,000 | $300.00 | ~315 | $3,000.00 | $7,000.00 |
| Milestone 4 | $100,000 -> $1,000,000 | $3,000.00 | ~3,157 | $30,000.00 | $70,000.00 |
| (top) | $1,000,000 | $30,000.00 | ~31,578 | $300,000.00 | $700,000.00 |

The contract counts in the task (1, 3, 30) are the rounded `floor(0.03 * equity / 0.95)` at each milestone; the dollar budget is the governing rule.

**Trade counts to each milestone** (Monte Carlo median, 3% sizing, ~98% win rate):
- $100: 1,794 trades (p-hit 98%)
- $1,000: 4,909 trades
- $10,000: 7,854 trades
- $100,000: 10,766 trades
- $1,000,000: 13,687 trades (p10 12,868 / p90 14,582)

At an idealized 22 concurrent rungs per 15-min window (88 trades/hour), $1M median ≈ 155 hours (~6.5 days). That assumes 22 qualifying 92-99c favorites every 15 min continuously — the **binding constraint is flow (sourcing qualifying markets), not sizing**. At low balances per-trade PnL is cents, so the early tiers are slow in wall-clock terms even if the math compounds.

---

## 2. How the 30% cash floor and 3% window budget re-anchor

Both are **functions of total equity**, recomputed each cycle (`poly_us_trader.py:422-428` floor, `:680` window budget) and each new 15-minute window:

- `window_budget = round(total_equity * 0.03, 2)` — resets to 0 at each window boundary.
- `min_cash_floor = total_equity * 0.30` — cash at/below this blocks new entries.
- `available_cash = cash - min_cash_floor` — only cash above the floor is deployable.
- Per-trade cap = `min(compute_tier_size(equity), window_budget_left, available_cash, 0.03*equity)`.

**Re-anchoring at each milestone** — as equity crosses a milestone, both the floor and the budget scale up 100x together:

| Equity | Window budget (3%) | Cash floor (30%) | Deployable per window cap | Max concurrent 3% rungs |
|--------|--------------------|------------------|---------------------------|-------------------------|
| $35 | $1.05 | $10.50 | $1.05 | ~23 |
| $100 | $3.00 | $30.00 | $3.00 | ~23 |
| $1,000 | $30.00 | $300.00 | $30.00 | ~23 |
| $10,000 | $300.00 | $3,000.00 | $300.00 | ~23 |
| $100,000 | $3,000.00 | $30,000.00 | $3,000.00 | ~23 |
| $1,000,000 | $30,000.00 | $300,000.00 | $30,000.00 | ~23 |

**Invariant:** deployable fraction stays 70% and max concurrent 3% rungs stays `floor(0.70/0.03) = 23` at every tier. Only the *dollar size per rung* grows 100x per milestone. The floor never binds on a single sequentially-settled trade because `3% <= 70%` deployable by construction; it only limits how many rungs can be open simultaneously (~23).

---

## 3. Code change made (align `poly_us_trader.py` with the ladder)

`compute_tier_size` previously returned **2% of equity above $100k** (`round(total_balance * 0.02, 2)`) plus inert per-tier caps (`min(30/300/3000, …)`). So at exactly $100,000 it sized **$2,000** instead of the spec's **$3,000** — a confirmed deviation (flagged in `docs/TASK4_ZERO_DRAWDOWN_COMPOUNDING.md:50` and `docs/PONYTAIL-REVIEW.md:17`).

**Fix (one function, single caller at `:439`):** `compute_tier_size` now returns `round(total_balance * 0.03, 2)` uniformly — no caps, no 2% branch — so the code implements the ladder exactly ($1.05 / $3.00 / $30.00 / $300.00 / $3,000.00 / $30,000.00). The redundant `min(..., round(total_balance*0.03,2))` at `:440` is kept as a harmless defensive clamp.

**Tests:** `tests/test_poly_us_trader.py::test_compute_tier_size` updated (200,000 -> $6,000 not $4,000) plus milestone-balance assertions ($35->$1.05, $100->$3.00, $1k->$30, $10k->$300, $100k->$3,000). Result: **17/17 pass**, `verify_compiles --all` clean.

---

## 4. Liquidity limits & market depth (price impact)

**Assumptions (not live-measured):** active Polymarket US 15m crypto up/down markets in the 92c-99c band show top-of-book depth on the order of a few hundred to ~$1,000–$1,500 per side (retail venue; depth tails off quickly beyond the best level). The bot places **maker** limit orders (`participateDontInitiate=True`), so it never crosses the spread.

| Consequence | Entry | Exit (settlement) |
|-------------|-------|-------------------|
| Price impact / slippage | **None by design** — resting limit, doesn't cross the book | **None** — binary contract settles at $1.00/$0.00 by the venue |
| Real constraint | Fill quality / depth at resting price | None |

Because entry is passive, "price impact" shows up as **fill degradation / adverse selection**, not slippage: resting a $3,000 bid at 95c when only ~$500 sits at that price makes you the top of the book — you fill only when a taker eats your full size or the price moves through you, i.e. you get filled when informed traders disagree.

**Threshold estimate:** order size starts exceeding plausible single-level depth at **Milestone 4 ($100,000 bankroll, $3,000/window ≈ 3,157 contracts @95c)**. Milestone 3 ($300, ~315 contracts) is likely within depth; Milestone 4 likely is not on a single market.

**Operational rule (roadmap step):** before sizing, probe `polymarket_us_client.get_market_book(slug)` and cap order cost at **25–50% of visible best-side depth**; if a market's depth is thin, split the window budget across multiple markets/windows rather than one large resting order. Confirm live depth figures by running `get_market_book` on active 92-99c crypto markets during dry-run.

---

## 5. Operational roadmap

1. **Code (done):** `compute_tier_size` now implements the clean 3%-per-milestone ladder; tests updated and passing (17/17). Commit + push.
2. **Dry-run validation:** run `python poly_us_trader.py --once --reset 35.0`, verify milestone events fire at $100/$1k/$10k and window budget re-anchors to 3% of equity each cycle. Confirm `milestones_reached` grows monotonically and no `window_budget < 0.90` false blocks at start tier.
3. **Live depth probe:** collect `get_market_book` depth for active 92-99c crypto markets over several cycles; record best-side depth at entry price. Use it to set the 25–50% depth cap and confirm the Milestone 4 ($100k) price-impact threshold empirically.
4. **Concurrency check:** verify the bot can source ~20–23 qualifying 92-99c favorites per 15-min window (the binding flow constraint). If flow is below ~8 rungs/window, wall-clock to $1M stretches far beyond the idealized ~6.5 days; do not loosen the band/gate to chase flow (gate calibration governs edge — see AGENTS.md "REALITY TAX").
5. **Milestone gating:** at each milestone crossing, re-confirm `compute_tier_size` output matches the table ($300 at $10k, $3,000 at $100k) and that the cash floor (30%) scales with equity (e.g. $30,000 floor at $100k). Do not manually override sizing between milestones.
6. **Risk stops:** keep the drawdown halt and consecutive-loss cooldown from `risk.py` active; the ladder's edge (~1c/trade after gate calibration per AGENTS.md) depends on the model gate staying calibrated — if the gate degrades, halt and recalibrate before increasing size.
7. **Cutover to live:** only after dry-run shows clean milestone progression and confirmed depth headroom; `--live` requires `KALSHI_CONFIRM_LIVE=true`.

**Bottom line:** the ladder itself is simple math (3% of equity, 30% floor, ~23 concurrent rungs, constant across tiers). The binding constraints are (a) sourcing enough qualifying 92-99c favorites per 15-min window, (b) keeping the model gate calibrated so edge stays positive (~1c/trade), and (c) order size vs. book depth which becomes a planning concern at Milestone 4 ($100k). Sizing is not the constraint; flow and edge calibration are.
