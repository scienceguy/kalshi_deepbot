# Dry-Run Growth Trajectory — Measured on Live Orderbook Data

**Prepared by**: ubuntu:opencode:kalshi_deepbot
**Date**: 2026-11-21
**Tool**: `scripts/dryrun_growth_sim.py` (generated trades recorded in
`/tmp/opencode/drysim_trades.json`)

---

## Method

The simulation replays **81 days of real per-minute Kalshi orderbook quotes**
(HF dataset `papylove/kalshi-15m-data`, `quote_history/2026-07-23` →
`2026-10-11`, 76,507 fifteen-minute market windows across 15 series) through the
bot's own code paths — no re-implemented logic:

| Mechanism | Reused code |
|---|---|
| Signal, maker price (bid+1 / ask−1), Kelly size, entry band 95–99c, entry window 8–14.5 min | `signals.evaluate()` |
| Cash floor 70%, exposure ceiling 30%, 8% drawdown halt, 3-loss cooldown, per-market daily loss, liquidity checks | `risk.RiskManager` with `paper=True` (dry-run knobs) — clock patched to the simulated timeline |
| Maker entry fee = 0 on these series; settlement is not a trade | `fees.buy_cash_out / sell_cash_in` |
| Settlement pays $1.00 iff `expiration_value >= floor_strike` (verified 100% agreement with the dataset's `result` column) | HF quote_history |
| Fill rule: a resting bid at `entry_price` fills only on a minute where that side's ask ≤ `entry_price` **with quoted volume > 0** — no fabricated fills | `trader._dry_run_fill_count` semantics |

DRY_RUN is set True at runtime (mirrors `trader.py --dry`; the repo `.env`
ships `DRY_RUN=false`).

## Results (start $35)

| Config | Trades | Win rate | Fill rate | Total PnL | Final equity | Max DD | Halted? |
|---|---|---|---|---|---|---|---|
| **Maker, production dry-run guards** (8% DD halt) | 209 | 93.3% | 23.9% | **−$0.98** | $34.02 | 33.5% | yes, at −15% |
| Maker, `--paper-experiment` (the built-in paper knobs: 90% halt threshold) | 2,130 | 95.3% | 21.4% | **+$128.00** | $163.00 | 55.2% | never |
| Taker (`--taker`: cross ask, model gate, taker fee) | 36 (5-day sample) | 94.4% | 100% | **−$2.48** | $32.52 | 15.0% | yes, at −15% |

Measured edge (paper-experiment run): **+$0.0085 per contract**, 26.3
trades/day, daily growth factor **1.0192×**.

### Why the two maker runs differ so much

Both runs place identical signals. The only difference is the drawdown halt:
under the production 8% halt the run stops at the first −15% equity dip (peak
$51.14, then halted — entries never resume because recovery needs a new high
and `HALT_AUTO_RESUME=false`). The paper-experiment knobs
(`PAPER_DRAWDOWN_HALT_PCT=0.9`, `PAPER_GUARDS_ENABLED=false`) exist exactly for
growth-trajectory experiments and survive the drawdowns.

### Taker is fee-negative

The taker run matches the measured REALITY TAX analysis: the bot's fixed
`MODEL_VOLATILITY=0.80` gate is not calibrated to realized per-minute
volatility, so paying the ask + 7%-of-p(1−p) fee removes the edge (−1.5c per
contract measured here, 5-day sample).

## Projection to $3.25M

Using the measured per-trade EV, trades/day and the $25,000 `MAX_TRADE_DOLLARS`
cap (per-trade = 15% of equity until the cap binds):

- Log-compounding estimate (no cap): **602 days**
- With the $25k/trade cap: **787 days** (~2.2 years of continuous trading)

Under production dry-run guards: **never** — the run halts before it
compounds (measured final equity $34.02 < $35 start).

## Validation against prior measurements

- Realistic maker fills with adverse selection (fills only when the ask comes
  to our price): fill rate 21.4% over 81 days.
- Win rate on fills here is 95.3% vs the 91.2% measured in the 2026-11-12
  REALITY TAX note on the same dataset. The entry windows differ (this run uses
  the live 8–14.5 min config; that note measured minutes 6–11), and the periods
  differ (Jul–Oct vs the 14 newest days of Oct). **The win-rate-on-fills
  divergence is unresolved and should be re-measured on identical windows
  before anyone sizes up off this model.**

## What this means for the $3.25M target

The target is not reachable in any short window. Measured, evidence-based
trajectory:

1. **Under production dry-run guards: no trajectory** — breaks even and halts.
2. **Under the paper-experiment knobs: ~787 days** of continuous trading at the
   measured +1.9%/day, with a measured 55% peak-to-trough drawdown along the
   way. A single operator pause, venue outage or parameter regression resets
   the clock; the plan is not robust at this edge.
3. The live-account prerequisite (funded accounts, confirmed on
   `LIVE_READINESS_CHECKLIST.md`) is unchanged; the live Kalshi account currently
   holds **$9.82** real balance, 0 resting orders, 0 positions, risk-halted.

Recommendation: treat 787 days as the model's honest answer, not a plan. The
edge is ~1c/trade and rests on the maker fill rule; it does not survive the
production halt config, and it has not been validated to the point where sizing
above 15% is defensible.

## Reproduce

```bash
cd /opt/superbot
SUPERBOT_DATA_DIR=/tmp/opencode/drysim HF_HUB_OFFLINE=1 \
  python scripts/dryrun_growth_sim.py --days 2026-07-23 ... 2026-10-11 \
  --start 35.0                       # production guards
SUPERBOT_DATA_DIR=/tmp/opencode/drysim HF_HUB_OFFLINE=1 \
  python scripts/dryrun_growth_sim.py --days ... --paper-experiment   # growth model
SUPERBOT_DATA_DIR=/tmp/opencode/drysim HF_HUB_OFFLINE=1 \
  python scripts/dryrun_growth_sim.py --days ... --taker              # taker variant
```

Data is cached under `/tmp/opencode/hf_cache` (81 files, ~12 MB).