# Dry-Run Growth Trajectory — Measured on Live Orderbook Data

**Prepared by**: ubuntu:opencode:kalshi_deepbot
**Date**: 2026-11-21 (fill-rule correction, v2)
**Tool**: `scripts/dryrun_growth_sim.py` (generated trades recorded in
`$TMPDIR/drysim_trades.json`; live-collected replay → `live_trades.json`)

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
| Fill rule: a resting bid at `entry_price` fills only on a minute where **our side's ask** ≤ `entry_price` **and the counterparty depth is > 0** — no fabricated fills | `trader._dry_run_fill_count` semantics |

DRY_RUN is set True at runtime (mirrors `trader.py --dry`; the repo `.env`
ships `DRY_RUN=false`).

### Fill-rule correction (v2 — retracted the v1 numbers)

The first version of the sim checked the **YES ask for every signal** and
capped the fill by the **same side's** volume. `trader._dry_run_fill_count`
defines it differently, and the v1 run therefore reported **fabricated fills**:

- a NO bid must fill against the **NO ask** (`100 − yes_bid`), not the YES ask;
- the available contracts are the **counterparty** depth — buying YES lifts
  the best NO bid (`no_liquidity`), buying NO lifts the best YES bid
  (`yes_liquidity`).

v1 (wrong rule): 2,130 trades, 95.3% win, **+$128.00** — **retracted**.
v2 (faithful rule, verified against the function's docstring and code, with a
side-aware regression test in `tests/test_dryrun_growth_sim.py`): below.

## Results (start $35, 81 days)

| Config | Fills / signals | Win rate | Total PnL | Final equity | Max DD | Halted? |
|---|---|---|---|---|---|---|
| **Maker, production dry-run guards** (8% DD halt) | 44 / 385 (11.4%) | 93.2% | **−$5.06** | $29.94 | 27.4% | yes, at −15% |
| Maker, `--paper-experiment` growth knobs | 300 / 10,357 (2.9%) | 92.7% | **−$25.23** | $9.77 | 76.4% | never (0.9844×/day) |
| Taker (`--taker`: cross ask, model gate, taker fee) | 36 (5-day sample) | 94.4% | **−$2.48** | $32.52 | 15.0% | yes, at −15% |

Measured edge under the corrected maker rule: **−3.2c per contract**
(paper-experiment run). **Both maker configs lose money.** The v1 "+$128" was
an artifact of the fill-rule bug, not a measured edge.

### Why the fill rate collapsed

The resting bid sits at ask−1. Under the faithful rule it only fills when the
market actually crosses into us — and depth must sit behind that cross. Over 81
days that happened for 300 of 10,357 quote cycles (2.9%). The v1 rule filled
NO orders whenever the *YES* ask was cheap, which is not how the venue matches
orders.

## Projection to $3.25M

Using the measured per-trade EV, fills/day and the $25,000 `MAX_TRADE_DOLLARS`
cap (per-trade = 15% of equity until the cap binds):

- **Maker: never.** Daily growth factor 0.9844× — the measured EV does not
  compound to the target at any horizon (capped model: not in 100 years).
- **Taker: never** (fee-negative, −1.5c/contract on the 5-day sample).

Under production dry-run guards: **never** — the run halts before it
compounds (measured final equity $29.94 < $35 start).

## Validation against prior measurements

- The 2026-11-12 REALITY TAX note measured maker resting at ask−1 at
  **−4.06c/trade** with a 36.5% fill rate and win rate dropping 96.7% → 91.2%
  on fills (adverse selection). This run's corrected maker EV of −3.2c/contract
  is consistent with that measurement; the v1 win rate of 95.3% was inflated by
  the same bug.
- Taker + a calibrated vol gate was +1.03c in that note; this sim's taker is
  negative because `MODEL_VOLATILITY=0.80` is fixed and uncalibrated — the
  binding problem is the gate, not the band.

## What this means for the $3.25M target

The target is not reachable in any short window. Measured, evidence-based
trajectory:

1. **Maker: no trajectory** — negative EV at every horizon measured. The
   honest answer is that a 1c-inside-the-spread maker strategy on this venue
   and band does not pay.
2. **Taker: no trajectory** — fees exceed the edge under the current gate.
3. The live-account prerequisite (funded accounts, confirmed on
   `LIVE_READINESS_CHECKLIST.md`) is unchanged; the live Kalshi account currently
   holds **$9.82** real balance, 0 resting orders, 0 positions, risk-halted.

Recommendation: there is no measured path to $3.25M at this edge. Any plan that
reaches it must first change the *strategy* (a calibrated model gate — the
+1.03c/trade taker variant from the REALITY TAX note — or a different venue),
not the sizing. The corrected rule is a bug fix; the earlier +$128 result
should not be cited.

## Reproduce

```bash
cd /opt/superbot
SUPERBOT_DATA_DIR=$TMPDIR/drysim HF_HUB_OFFLINE=1 \
  python scripts/dryrun_growth_sim.py --days 2026-07-23 ... 2026-10-11 \
  --start 35.0                       # production guards
SUPERBOT_DATA_DIR=$TMPDIR/drysim HF_HUB_OFFLINE=1 \
  python scripts/dryrun_growth_sim.py --days ... --paper-experiment   # growth model
SUPERBOT_DATA_DIR=$TMPDIR/drysim HF_HUB_OFFLINE=1 \
  python scripts/dryrun_growth_sim.py --days ... --taker              # taker variant
```

Data is cached under `$TMPDIR/hf_cache` (81 files, ~12 MB).

### Today's live fill data

`scripts/live_book_collector.py` records the same schema from the live venue
(top-of-book per-side depth + settlement result), so the replay is not limited
to the 3-week-stale HF history:

```bash
TMPDIR=/tmp/opencode python scripts/live_book_collector.py \
    --out $TMPDIR/live_books.jsonl --minutes 95
python scripts/dryrun_growth_sim.py --collected $TMPDIR/live_books.jsonl
```

One fidelity caveat between the two sources: the HF parquet carries a single
`volume` column (a per-minute venue number, not top-of-book depth), while the
live collector records per-side `yes_vol`/`no_vol`. First live sample
(27 settled windows, 2026-11-21 07:20–08:00Z): 28 of 171 book-minutes in the
8–14 entry window had an ask in the 94–99c band, but most of those minutes
sit on 0–30 contracts of our-side depth and `risk.check_liquidity`
(`MIN_DEPTH_CONTRACTS`) correctly refuses them. The historical numbers in the
table above are therefore still **optimistic on the liquidity axis**: real
top-of-book depth at these prices is thinner than the single HF volume column
implied. The live-data replay is the trustworthy one.

### Measured on today's live fill data (2026-11-21 07:24–08:59Z)

Full collection: **905 rows, 63 settled windows, 9 series, 5.25h span**
(41 settled NO / 13 YES). Replayed through the same code paths
(`--collected`, start $100):

| Config | Signals attempted | Fills | Fill rate | Total PnL | Final equity |
|---|---|---|---|---|---|
| Maker, production dry-run guards | 7 | 0 | 0% | $0.00 | $100.00 |
| Maker, `--paper-experiment` knobs | 7 | 0 | 0% | $0.00 | $100.00 |

Zero fills: the resting maker bids (ask−1) were never crossed by the live book
inside those windows, and in-band minutes mostly carried 0–30 contracts of
our-side depth, so `check_liquidity` refused most of them. This is the
strategy's actual live behaviour — consistent with the 81-day replay's
−3.2c/contract and with the 2026-11-12 REALITY TAX adverse-selection
measurement. Over this sample the strategy neither earned nor lost; it simply
did not get filled.

(Schema note: the collector emits dollar prices like the HF parquet; a
conversion of the pre-fix cents rows was applied once to this file.)

### Settlement-index replay on the completed sample (2026-11-21 12:13–12:28Z)

Second collection: **559 rows, 54 settled windows, 9 series, 2.8h span**,
replayed as the taker variant (`scripts/dryrun_growth_sim.py --taker
--collected`, start $100):

| Gate | Signals | Fills | Win rate | Total PnL |
|---|---|---|---|---|
| Raw feed (no index terms) | 1 | 1 | 100% | +$0.58 |
| Index-corrected (bias −13.15bps, dispersion 17.5bps, both measured) | 0 | 0 | — | $0.00 |

One clean pass (`KXXRP15M-26NOV210530-30`, minute 11, yes@97c, depth 60) is
n=1 — it cannot support the +1.03c/trade edge the 2026-11-12 REALITY TAX note
measured on the HF set. With the settlement index priced in — the CF
Benchmarks BRTI 60s average the contracts actually settle on, measured
−13.15bps below the feed over 45 windows with 17–20bps cross-venue
dispersion — the gate finds nothing. The apparent taker edge is a
data-source mismatch, not an edge: the model was scoring the venue mid, and
the ask sits above that. **$3.25M remains honestly unreachable at the
currently measured edge.** A strategy change needs a demonstrated positive
edge against the settlement index first; the maker variant is dead by
adverse selection (REALITY TAX: fill rate 36.5%, win rate drops 96.7% →
91.2% on fills).

### Priced against the actual settlement index (2026-11-21, zero basis)

Kalshi's API passes the CF Benchmarks index through with the same key
(`/trade-api/v2/cfbenchmarks/*`, `BRTI` and `{ASSET}USD_RTI`), so the
settlement index itself is now reachable — no proxy, no measured bias:
`scripts/fetch_brti_history.py` pulls tick-level history (5Hz BTC/ETH/SOL/
XRP/DOGE, 1Hz the rest; 835,200 ticks over the sample span) and
`scripts/dryrun_growth_sim.py --index-priced` rewrites each row's feed spot
to the index value at its minute. Replayed over the same sample as the taker
variant (72 settled windows, 9 series, 7.0h span, all 759 rows carrying the
index at their minute):

| Gate | Signals | Fills | Win rate | Total PnL |
|---|---|---|---|---|
| Settlement index, zero basis by construction | 1 | 1 | 100% | +$0.58 |

One window survives with zero basis — it was genuine relative edge, not a
feed/index artifact — but n=1 over 72 windows is not an edge, and the
sensitivity runs (17.5bps dispersion → 0 signals; 1bps → 0 signals) show the
gate's output swings on assumptions no larger sample has calibrated. The
data-source comparison is now complete for this strategy: with the
settlement index priced in, the gate produces nothing repeatable.
**$3.25M remains honestly unreachable at the currently measured edge**
(needs a demonstrated positive EV against the BRTI settlement index first;
the maker variant is dead by adverse selection).