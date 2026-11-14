# PONYTAIL-AUDIT — kalshi_deepbot repo-wide, 2026-11-14

Base: 20 targeted web searches (Polymarket US API, favourite-maker adverse selection, BS/lognormal digital pricing, cross-venue arb, half-Kelly compounding, daemon/HTTP patterns) + reads of poly_us_trader.py, polymarket_us_client.py, gates.py, config.py, risk.py, crossvenue_edge.py.

## What this repo does

Live + paper trading of Kalshi 15m crypto binaries and Polymarket US favourites at 92-99c: GBM/log strike gates, cross-venue edge detection, Supabase/n8n logging, and a $35 paper bankroll compounding toward $250k (aspirational).

## Must fix

1. **Favourite-maker default is the measured-dead variant — config.py `AGGRESSIVE_FILL=False` (286), `kalshi_client.py:672-675` (post_only rest), `polymarket_us_client.py:296,322` (`participateDontInitiate=True`)**
   - What this is: Default entries are passive maker limits on the favourite.
   - Problem: Measured -4.06c/trade resting at ask-1 on 95-99c (win 96.7%→91.2% on fills); Bartlett & O'Hara (NBER 2026, 41.6M Kalshi trades): makers hold to settlement so the spread can't protect, VPIN predicts maker losses; Polymarket FLB data: <10c buyers lose 19.3c/$, ≥90c earn only +0.83c.
   - Fix: Default to taker-at-ask behind the gate; keep maker only behind a VPIN-style toxicity/fill-quality check (sub-minute fills scored -0.31, 10min+ fills +0.43).
   - If we skip it: Every fill sits on the toxic side of the selection; the -0.31% maker rebate does not cover ~4c of adverse selection.

2. **NO-side order price sent as literal, not YES-complement — `polymarket_us_client.py:267-329` (`preview_order`/`place_maker_order`)**
   - What this is: `price.value` is passed straight from `price_dollars` for both YES and NO intents.
   - Problem: Polymarket US defines `price.value` as the YES side always; NO at 97c must send 0.03. Sending 0.97 with `ORDER_INTENT_BUY_SHORT` validates/places the wrong order; the paper engine then "verifies" a fiction.
   - Fix: `value = price if side=="yes" else round(1-price, 4)`; confirm once against live `orders.preview` (free) and pin it in a test.
   - If we skip it: All NO-side flow is fiction — preview, paper fills, and any future live order.

3. **Cross-venue edge threshold 0.5c is inside the noise — `poly_us_trader.py:262-276`**
   - What this is: `best_edge_cents >= 0.5` at best_price 0.92-0.99 qualifies as a tradeable edge.
   - Problem: Cross-venue arb needs >3-3.5c after fees (taker ~0.7c, Poly maker rebate -0.31%) because settlement references disagree 5.7-6.2% (Chainlink vs BRTI), execution is non-atomic, and quotes lag Binance ~347ms median.
   - Fix: Require ≥3.0c net of fee + oracle-gap buffer, or drop the cross-edge leg and trade the cushion gate only; recalibrate `min_edge` with it.
   - If we skip it: Paper P&L attribution learns from noise; no edge survives the oracle gap.

4. **Fixed vol 0.80 fallback is the gate's poisoned input — `config.py:343` (`MODEL_VOLATILITY`), `crossvenue_edge.py` vol path**
   - What this is: When realized-vol feed is empty, the GBM falls back to a fixed 80% vol.
   - Problem: Measured on 14 days of 95-99c: fixed 0.80 → -0.01c/trade; realized vol → +1.03c. The whole edge is the vol calibration, not the band.
   - Fix: If `MODEL_VOL_DYNAMIC` is on and the feed is empty/stale, emit no signal (fail closed) rather than fall back to 0.80; keep the fallback only so old behaviour is reachable for forensics.
   - If we skip it: Every dry run silently measures the uncalibrated model and concludes "no edge" for the wrong reason.

5. **Expiry/rotation exit books fictional prices — `poly_us_trader.py:253-255` (`if hours_left is not None`), `poly_us_trader.py:508-515`**
   - What this is: 24h filter passes markets with no parseable end date; stale-position rotation pays out at last cached MTM, not a venue quote.
   - Problem: Undated junk never gets filtered and never rotates — capital locks in-flight forever; dated rotations book arbitrary P&L that poisons the $35→$250k compounding measurement.
   - Fix: Require a parseable end date for entry (default-deny); rotate at best executable quote or skip and mark-to-settlement; log skipped exits.
   - If we skip it: The headline paper-bankroll curve is meaningless exactly where it matters (long-dated, finally-settled trades).

## Should fix

6. **Paper state save missing fsync — `poly_us_trader.py:106-112`**
   - What this is: Atomic JSON write via sibling tmp + `os.replace`, no flush/fsync.
   - Problem: Crash between replace and page-flush can leave 0-byte state; on Android, page-cache pressure makes this more likely than on Linux.
   - Fix: `tmp.write_text` → `fh.flush(); os.fsync(fh)` → `os.replace` → (optional) fsync dir; keep single-writer assumption.
   - If we skip it: Rare, silent bankroll loss; un-debuggable after the fact (no ledger gap, no error).

7. **Gating edge rests on 0.3c margin — `config.py:204-228` (`GATES_MEASURED_FLOOR_ENABLED=False`), `gates.py:483-499`**
   - What this is: The +1.03c result comes from a model that is overconfident vs venue mid; clustered by market the 95-99c band clears breakeven by lo95 0.9741 vs 0.9710.
   - Problem: A 0.3c margin on 4 tail losses, over one 7-day slice, BTC/ETH/SOL only — any drift in fill behaviour or the band kills it silently.
   - Fix: Re-run `scripts/measure_edge.py --cluster market` on every new data slice; keep `GATES_MEASURED_FLOOR_ENABLED=False`; alarm on lo95 < breakeven.
   - If we skip it: Live size gets placed on a statistically thin edge with no tripwire.

8. **25 concurrent Poly positions treated as independent — `poly_us_trader.py:47` (`max_open_positions=25`)**
   - What this is: Up to 75% notional (25×3%) into what is mostly one correlated crypto bet.
   - Problem: BTC/ETH/SOL 15m outcomes are one factor; correlated positions are one bet (bankroll literature: cap combined correlated bucket ~15%).
   - Fix: Add per-symbol/correlation-bucket exposure sub-cap (~15% combined) or max 2 crypto + N uncorrelated event markets.
   - If we skip it: One 99c tail event wipes months of 0.4c edges in one settlement.

9. **Silent decision paths — `poly_us_trader.py:405-424` (execute_paper_trade)**
   - What this is: Four `return False` exits (already held / 25 max / cash floor / <$0.90) with no log.
   - Problem: Operator cannot tell "no opportunity" from "floor blocked" from "bug"; tuning drifts blind.
   - Fix: Debug-log one line with the reason at each exit; count blocks per cycle in the summary line.
   - If we skip it: Every "bot did nothing today" investigation starts from zero telemetry.

10. **Settlement poll has no backoff/jitter — `poly_us_trader.py:204-230`**
    - What this is: Every cycle, every closed-but-unsettled position hits `/v1/markets/{slug}/settlement` until 200.
    - Problem: 15m cycle × N stuck positions → REST hammering and rate-limit risk; 404s are normal pre-settlement.
    - Fix: Per-slug next-check timestamp (e.g. 5, 15, 30, 60, 120s backoff); keep 404 as "keep holding".
    - If we skip it: Bursts of 429s during the exact window when settlements land and cash frees up.

## Nice to have

11. **Keep ML auto-load impossible — `config.py:283`, `llm_eval.py` precedent**
    - What this is: MLFairValue exists but `--ml-model` stays explicit (43-feature model loses to venue mid in the OpenMarket null result).
    - Fix: Leave as-is; any auto-load PR is a stop-trading bug.
    - If we skip it: n/a (protective).

12. **$35→$250k expectation setting**
    - At +0.43c idealized per trade and ~$1/trade at 3% sizing, EV/day ≈ cents, not 3x. Milestones list is fine; the 7-day moonshot is not attainable at fee-net edge; track EV/trade and win rate, not equity curve.
    - If we skip it: Operator "fixes" sizing upward when the curve is flat — the one change guaranteed to ruin it.

13. **In-flight gates.py changes (log-z cushion + pin-risk deny) align with search evidence** (minutes ≤6 win ~97-98% vs ~100% at 7-11min; BS digital d2 = ln(S/K)/σ√T driftless) — review when committed; do not "fix" them back to arithmetic.

## Verdict

Money-path items: NO-price complement (2-line fix) and maker-default. Everything else is hygiene on a thin, gate-calibrated edge that is real but ~1c.

## Lean

Cut nothing further; the file count is right. Add the fsync, the reason-logs, the end-date requirement. Leave architecture (series gates, batched state writes, pre-commit gates) alone.

## Not checked

Live Polymarket US order placement (paper only), n8n/Supabase runtime health, test suite re-run (462 claimed passing, not re-verified), `docs/poly_us_settlement_latency.md` (untracked), uncommitted gates.py/config.py diffs in full, backtest validity post-fix.
