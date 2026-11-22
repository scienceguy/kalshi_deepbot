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

### HF reality sweep: every band x window x side is negative (2026-11-21)

Full sweep of the high-resolution sample (1,146,514 quote rows, 76,507
tickers) over 14 bands x 8 entry windows x YES/NO, both honest entry models:

- **taker** (cross the ask, pay the Kalshi quadratic fee): 0 of 224 combos
  positive. Best cell [97,99]c in minutes 3-9 at -0.395c/trade (n=5,354,
  98.3% win rate).
- **maker** (rest one cent inside the ask, no fee, fill only when the ask
  then trades down to the resting price — informed flow, adverse selection
  priced in): 0 of 224 combos positive. Best cell [98,99]/[3,9] at
  -0.219c/trade on 384 fills (12.6% fill rate — thin); the [95,99] maker
  cell is -2.407c/trade.

The published "+1.49c/contract maker ceiling" (botforkalshi, 2026-10-20)
does not survive honest fill modelling on this data: a resting favourite
fills only when the quote moves against it. Secondary sources agree — a live
join-the-touch maker bot measured -12% over 7 days (-13.9c/contract at 4.7
sigma), and the Kalshi 15M speed study shows a faster bot collects *more* of
the losing fills (88.7% vs 76.4% at 650ms vs 5s). The one published maker
result that survives (Buergi/Deng/Whelan: +2.6% after fees on >=50c makers)
describes the international CLOB, not this series.
**The maker lane stays dead by adverse selection; the taker lane is negative
everywhere measured.**

### 15M cross-venue, settled verdict: the apparent edge was feed-vs-index basis (2026-11-21)

The first two 15M up/down windows collected on both venues
(KXBTC15M <-> cpc-btc-updown-15m, same BRTI strike on each side — both
settle on the CF Benchmarks closing 60s average) both settled `no`:

| Window | Kalshi result | Closing 60s BRTI avg | Strike | Composite feed at close-1m | Feed minus index | Venues' final YES quote |
|---|---|---|---|---|---|---|
| 211630 | no | 60351.17 | 60362.52 | 60509.99 | +$158.8 (+26bps) | 0.125 at 4.3 min |
| 211645 | no | 60296.57 | 60351.39 | 60440.00 | +$143.4 (+23bps) | 0.0005 at 0.6 min |

The composite-feed model quoted fair 0.94-1.00 (edge +39c to +73c) through
both windows while both venues quoted 0.0005-0.125. **Both venues were right
both times.** The composite's own price sources disagreed by ~$142 during
these rows — the same order as its gap to the settlement index — so the
"edge" was entirely a basis artifact: the composite prints ~$145 (+24bps)
above the BRTI closing average both venues settle on. Crossing the model's
edge would have bought a losing contract at 59.5c.

The venues' mutual divergence inside a window oscillates instead (0, -9.5,
-2, +11, +2c in window 211630; 0, -3, +3, -3c in 211645) — no stable
venue-vs-venue bias to harvest at n=2 either.

Verdict: the 15M cross-venue comparison produces no repeatable edge in the
composite-fed model. The basis is now quantified over **8 adjudicated
windows** (211630..211815, every one CONSISTENT with the index-implied
result): live composite-minus-index is +6.6..+26bps (median +21.6bps, n=6
rows with a composite snapshot) against +4.1bps median on the October HF
sample — time-varying by ~10x, so a fixed correction is unsafe, and it is
zero by construction in the index-priced replay.
*Correction on the record:* an intermediate run flagged 211745/211800 as
index MISMATCHes. That was a parse bug in the close time (the seconds digits
of the HHMMSS ticker were dropped, putting two windows in the same closing
60 seconds — their identical closing averages, 60336.37 for both, gave it
away). Correct closes follow the +15-minute sequence from the API-verified
211630->21:30Z. The consecutive-window chain now also checks out: each
window's strike equals the prior window's closing average (211800's strike
60064.13 = 211745's close 60064.13).
**$3.25M remains honestly unreachable at the measured edge** — the
legitimate remaining lane is the volatility model the gate depends on (the
fixed 0.80 vol measures -0.01c live; the same gate with realised-vol
scaling measured +1.03c on HF data), not a wider band or a looser gate.

### Gate sweep on HF data: the first index-validated candidate (2026-11-21)

Full sweep of the fair-value log over the same recorded inputs as
`hf_fairvalue` (99,420 joined quote rows / 8,225 tickers, 2026-09-29..10-11;
fair value = zero-drift digital on dynamic per-minute realized vol; entry =
first qualifying minute per ticker): taker-with-gate **106/152 configs
positive** (n>=100), led by band [85,99]c in minutes 0-5 at +7.0c/trade;
maker 0/80 positive again.

Train/test split (split at 2026-10-09; train = the 10 log-production days
09-29..10-08, test = the 3 held-out days 10-09..10-11): the [85,99]/[0,5]
taker cells are positive on **both** splits (train +6.0..+6.9c, n=82-188;
test +7.4..+8.6c, n=25-53, win 96-98.1%) — not an in-day artifact. But the
fair value was computed on the composite feed, which measured +6.6..+26bps
*rich* against the settlement index on the live windows — the same basis
artifact class that faked the cross-venue edge. So the decisive test re-prices
the identical grid on the settlement index itself.

Index-priced replay (`scripts/hf_gate_index.py`; CF Benchmarks ticks for the
test days pulled through the Kalshi passthrough by `fetch_brti_history.py`,
1,296,000 BTC ticks plus per-asset for all 8 coins; the last index tick at or
before each row's timestamp; **every one of the 21,599 test rows carries an
index price**):

- The basis is real but small and unstable: composite-minus-index median
  **+4.1bps** (p10 -4.4, p90 +16.6; 77.1% of rows positive). Per coin:
  BTC +1.5, ETH +0.6, SOL +7.7, XRP +5.8, DOGE +6.6, HYPE +7.2, ZEC +12.5,
  NEAR +1.9bps. The live evening windows measured larger (+6.6..+26bps), so a
  fixed correction would not be safe; pricing on the index removes it by
  construction.
- **The [85,99]/[0,5] taker-gate edge survives index pricing**: +7.7..+8.6c
  on the composite -> +4.5..+6.0c on the index (n=33-53, win 93.9-95.3%);
  **142/142** index-priced configs positive (n>=20); the margin-0.05 grid
  runs +8.2..+9.1c (n=37-53). This is the first config with positive EV
  against the settlement index on held-out days.
- Two failed runs are on the record because the diagnostics caught them, not
  the results: mapping every row to BRTI made a $0.08 DOGE spot look -99.9%
  against the ~$82k BTC index (basis median -9989bps), and an index-id /
  asset-name key mismatch mapped 0/21,599 rows. What shipped is the per-coin
  mapping with a loud guard (`rows with index: 0` aborts the run).

Growth arithmetic for the surviving config (honest), now measured on both
splits: ~14 trades/day; per-trade return on balance at the 8% cap is
0.08 x (EV / ~89c ask), so +6.0c/test-day EV is +0.5%/trade (~+7%/day best
case) and +2.4c/train-day EV is +0.2%/trade (~+3%/day). From $10.34 that is
~$13.5-17.7 after 7 days; the $3.25M target needs ~+88%/day (x314,000) --
about 4-5 orders of magnitude beyond the measured edge. Caveats before anyone
acts on it: n=43-225 per split (win-rate SE ~3-10pp), the fair value is a
model whose vol input is computed on the composite (the fixed 0.80 vol
measures -0.01c on the same data), no live fills validate the taker fill
assumption yet, and Kalshi's own calibration study (97.2% accuracy at close
over 2.3M markets) means the ask near settlement is already close to fair
value -- the edge has to come from information priced later than the quote,
not the quote itself.

Against this candidate the independent evidence weighs the other way, and is
recorded here in full (Research Batch 4, 2026-11-21): the publisher of the
"+1.49c maker ceiling" ran his own live maker test (815 paper + 207 live
contracts, fills averaging 49.5c winning 45.3% against 49.5% implied) at
**-5.79c/contract** and no longer quotes the ceiling; a 6,303-window re-run
of the same band structure (leading side at the ask) measured **+0.67c before
fees and -0.42c after**, and the 90-99c first-touch "sure thing" won 96.75%
at an average price of 96.74c — a fair price, -0.19c after fees; 237 live
trades over two weeks realized **-1.21** against +0.7..+9.6 predicted from
history, with 86.9% of fills at top of book (execution was not the failure);
1,124 paper bets across five rules all came out negative; and an independent
backtest system found the market-implied probability is the best-calibrated
forecaster measured (ECE ~0.020 vs 0.03+ for every trained model) with every
taker strategy failing to clear the ~2.5c round cost out of sample. The
candidate survives on 3 held-out days (n=43); these samples are 5-50x larger.
**The 10 train days replayed at index pricing (2026-11-22, same tool): it
survives, thinner.** Train days 09-29..10-08 (77,821 rows / 6,762 tickers, all
rows carrying an index price, formula replication corr=0.999998): the
[85,99]/[0,5] taker cells stay positive on the index at +0.7c (n=76, margin
0.05), +4.2c (n=129, 0.03), +2.4c (n=150, 0.02) and +2.3c (n=196, 0) --
against +4.5..+6.0c on the test days for the same config. The composite side
reproduces the earlier split exactly (+6.9/+6.4/+6.1/+3.0c), so the difference
is the basis cost, which is larger on the train days (train basis median
+2.6bps, per coin -0.7..+8.5). Best index-priced train cells: [75,99]/[0,5]
margin 0.08 +6.2c (n=96, win 88.5%), [90,99]/[0,5] margin 0.03 +5.5c (n=63),
[85,99]/[3,9] margin 0.08 +5.2c (n=45); 166/175 index-priced configs positive
(n>=20). Over the full 13 days the config's index-priced EV is positive on
every split but ranges +0.7..+6.0c with the win rate 89.5-95.3% -- unstable by
~5x across splits, and still dwarfed by the independent n=5,787..6,303 windows
above. It means nothing until it replicates on live fills.

**$3.25M remains honestly unreachable at the measured edge** — the candidate
survives index pricing on both splits but at +0.7..+6.0c/trade (unstable ~5x
across splits), it is unvalidated on live fills, and the growth arithmetic
(+3..+7%/day best case) is ~4-5 orders of magnitude short. The dry-run lane
the objective names is shipped and its first honest replay is on the record:
`scripts/paper_trades.py` replays the rule over the recorded crossvenue rows
(both venues' top-of-book bid/ask -- recorded since 2026-11-22T00:45Z, the
`three_way` rungs always held them and the collector now writes them), fills
a taker at the ask or a maker when a later quote trades through the resting
bid, settles against Kalshi's own API result (result + close_time, never a
parsed label), and tracks realized and unrealized PnL, the per-trade cash
cap, max drawdown and a drawdown halt, with fees from `fees.py`'s own
taker/maker model. A daily-ladder guard (commit `924a95e`) keeps the
collector's "above" ladder snapshots (venue mids diverging by up to 30c -- a
different market family) out of the ledger. The lane sizes with the live
path's own rule (`signals.floor_contracts` -- Kalshi trades fractional
contracts down to the 0.01 lot per `kalshi_client.MIN_CONTRACTS`, matching
Kalshi's Jan-2026 CFTC rulebook amendment allowing sub-integer shares), after
the first version's whole-contract floor wrongly dropped every entry below a
~$11 balance. **First booked paper trade (2026-11-22T01:49:33Z):
KXBTC15M-26NOV212100 entered the rule at minute 5, ask 0.880, fair 0.999
(+11.9c gap), filled 0.94 contracts for $0.84 cash ($0.0069 taker fee),
settled YES against the Kalshi API result, realized +$0.10 -- balance $10.34
-> $10.44, n=1.** One trade proves nothing (win-rate SE at n=1 is ~50pp); the
hit rate is the candidate's own (~5% of minute 0-5 rows in the 85-99c band
with a 3c fair-above-ask gap), so the trajectory fills in as the collector
accumulates. **Repriced after the data-source audit below, this entry's true
fair against the settlement index is 0.9285 (gap +4.85c, not the feed's
0.9994 / +11.9c); the realized +$0.10 is unchanged.** The venue comparison stays honest on the 15M lane: median
divergence 1.00c (p90 5.30c, n=35 rows), the >2c rows clustered in minutes
13-15 as one venue lags the opened strike, same-snapshot change corr 0.854,
and no lead-lag resolvable at the 60s cadence -- consistent with the
independent co-located study (venues track within ~1c; cross-venue signals
are a latency/flow question, not a mispricing free lunch). That matches the
zero-basis replay verdict: no repeatable venue edge; the candidate is the
index-validated time-decay taker gate. Reality tax on the fill model itself
(independent literature, same session): the taker-at-ask fill is the
OPTIMISTIC rung -- a queue-aware replay of one maker strategy deleted 58% of
touch-fills and flipped the month's PnL sign (live shadow -$1,150 against
+$1,900 for the optimistic rung), and another maker simulator under-filled by
5x with fills biased toward the toxic tail ("simulated volume is a lower
bound, not an estimate"); the only validation that means anything is a paired
live/sim run over the same window compared on fill count AND fill quality
(time-to-fill distributions), not PnL. So this lane's numbers are an upper
bound on the candidate until a shadow run against real fills exists.
KL-divergence edge (the objective's named mechanism, measured 2026-11-22 on
the 37 test-day entries at the 3c gate): D_KL(fair || ask) has median 0.025
bits, and 64.9% of entries sit below 0.05 bits -- the threshold the KL
literature calls the trap zone where fees win -- with only 13.5% in the
0.10-0.20 "real edge" band. The first live dry-run entry (fair 0.999, ask
0.880) measures 0.176 bits, near the band's top, but n=1. A model that
equals the book scores 0.000 by construction, and the independent
calibration evidence (2,783 blocks: sides priced ~89c settle YES ~9 times
in 10; "a record that merely equals it is the book, relabelled") says the
measured 89.5-95.3% win rate at a mean ask of 88.7c matches the book's own
number -- so the right tail of the KL distribution is where any real edge
would live, and it is 13.5% of entries, unvalidated on live fills.
Staleness tax on this lane's own fills: an independent Kalshi lab measured a
paper engine that priced at a remembered ask (~20s old) at +1.85c/trade and
-7.12c/trade when repriced at the live ask (median staleness cost 3c, mean
3.92c), and its checklist says to assume any paper edge under 3c is zero.
Our collector's 60s cadence means every fill in this ledger is up to 60s
stale by construction -- the first trade's +$0.10 carries that bias, and the
fix that lab names (count a fill only when a real trade prints at or through
the price) is what the recorded bid/ask rows cannot yet provide.
Remaining lanes: vol-input calibration, trade-print fill confirmation, and
the paired live/sim shadow run.

**DECISIVE DATA-SOURCE AUDIT (2026-11-22) — "the edges lie in careful
comparison between data sources": the live collector's fair values were priced
off the wrong price source.** Measured directly (BRTI ticks fetched through
`scripts/fetch_brti_history.py` for 00:00-03:00Z, 54,000 BTC ticks):

- The composite spot feed (median of Binance/Coinbase/OKX/Kraken —
  `binance_feed.composite_price`) sat **$118-149 above BRTI at every timestamp
  checked** (00:50:02Z, 00:52:23Z, 00:54:42Z, 01:00:31Z, 01:46-01:51Z;
  ~19-25bps). Root cause: BRTI's constituent exchanges are Coinbase, Kraken,
  Gemini, Bitstamp, Bullish, Crypto.com and LMAX Digital (CF Benchmarks
  Constituent Exchanges List v13.7) — **Binance and OKX are not
  constituents**, so the composite carries the non-constituent/USDT basis the
  settlement index never sees.
- The strikes are exact. strike(212015) vs BRTI's settlement TWAP over the
  212000 window's final minute: **+$0.37 error (+0.1bps)**; strike(212100) vs
  the 212045 window's: **+$0.07 (+0.0bps)**. The collector's strike source is
  the venue's own index, and the chain rule (strike(N) = 60s BRTI TWAP of
  window N-1's final minute) is independently confirmed by DegenHedge's
  27,834-window audit (27,366/27,370 consecutive pairs equal to the cent) and
  CF Benchmarks' methodology (60 BRTI values in the last minute before
  expiry, averaged).
- The 57c "gap" row is fully explained: at 00:50:02Z BRTI was **$32.75 below
  the recorded strike**, so the index-priced fair is ~32c against the venues'
  ~35c quotes — both venues were pricing their own settlement index tightly,
  and our fair input was the outlier.
- Consequence for the lane: **the first booked entry's signal was ~2.5x
  overstated.** Feed-priced fair 0.9994 (gap +11.9c) vs the index-priced fair
  **0.9285** (gap **+4.85c**) — which lands inside the 13-day index replay's
  own test verdict (+4.5..+6.0c). The candidate survives repricing; the
  artifact does not. The realized +$0.10 stands (the settlement is the venue's
  own record, feed-independent).
- Fix shipped: `python scripts/paper_trades.py --file <day>.jsonl --settle api
  --index-spot <brti.jsonl>` reprices every replayed row's fair with the
  collector's own model (`binance_feed.prob_above`, zero-drift lognormal)
  against the settlement index; rows with no index tick within 45s of the
  snapshot instant are not replayed at all; the ledger records `fair` and
  `fair_source` ("index" | "feed"). 13 tests in
  `tests/test_paper_trades.py` pin the money paths (the index fair governs the
  entry — the feed's +4c gap does not book when the index prices -40c; rows
  without coverage are not replayed; the recorded fair equals prob_above on
  the index price).
- Independent corroboration: DegenHedge's Coinbase/Kraken spot reads sat a
  **median $7.77** from Kalshi's official settlement values (0.13%) — an order
  of magnitude below our composite's basis, because their reads were
  constituents; a Feb-2025-to-Jan-2026 academic study measures
  constituent-vs-non-constituent basis divergence in settlement windows (6.7
  bps per SD of strike proximity, p=0.026); Kalshi BackTest's settlement-drift
  study measures Coinbase leading BRTI by 8-14s in fast moves — a latency lane
  our 60s cadence cannot use, consistent with the co-located literature.
- Known remaining bias: the vol input (`sigma_min`) is still the feed's
  estimate — only the spot level is repaired. The 13-day replay already
  answers the EV question on index-priced data; the live lane now prices
  entries the same way.

Reproduce:

    python scripts/fetch_brti_history.py --start 2026-10-09 --hours 72 --out /tmp/opencode/brti_testdays.jsonl
    python scripts/fetch_brti_history.py --start 2026-09-29 --hours 240 --out /tmp/opencode/brti_traindays.jsonl
    python scripts/hf_gate_sweep.py     # full-sample grid
    python scripts/hf_gate_split.py     # train/test split of the grid
    python scripts/hf_gate_index.py     # index-priced replay (test days)
    python scripts/hf_gate_index.py --days 2026-09-29,2026-09-30,2026-10-01,2026-10-02,2026-10-03,2026-10-04,2026-10-05,2026-10-06,2026-10-07,2026-10-08 --brti /tmp/opencode/brti_traindays.jsonl --label TRAIN

Dry-run paper-trade replay against the recorded live quotes (default rule =
the candidate config; settles each window against the Kalshi API and writes
a ledger with realized and unrealized PnL, the cap, drawdown and halt):

    python scripts/paper_trades.py --file data/crossvenue/20261122.jsonl --settle api
    python scripts/paper_trades.py --file data/crossvenue/20261122.jsonl --mode maker --venue poly --settle api