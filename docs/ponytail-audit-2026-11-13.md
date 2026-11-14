What this repo does: Kalshi SuperBot is a live-trading bot for Kalshi 15-minute binary crypto markets. It places favourite-maker limit orders (YES/NO at 75-95c), evaluates signals via an LLM with 7-layer fallback, writes signals/orders/bot-state to Supabase, and routes settlement P&L through n8n webhooks. The assumed load is one operator running one bot process with a ~15s cycle, matching AGENTS.md.

## Must fix

### 1. ENTRY_BAND_MIN=95 / ENTRY_BAND_MAX=99 shuts out all tradeable markets (config.py:289-290, .env:ENTRY_BAND_MIN=95, ENTRY_BAND_MAX=99)

**What it is:** The entry band gate rejects any candidate whose entry price falls outside ENTRY_BAND_MIN-ENTRY_BAND_MAX cents. This gate runs in signals.py:_evaluate_common (line 404) and gates the favourite-maker strategy.

**Problem:** The configured band is 95-99c. Backtest optimum from 2026-11-03 was 15-55c. The live band 95-99 shuts out essentially all markets — the bot never enters a trade. Backtest results show 15-55c returns +$0.58/trade (win 52.8%), while 95-99c returns -$0.34/trade (win 83%). The AGENTS.md CORRECTION (2026-11-12) confirmed: "the live .env band 95-99 is correct; do not widen it back to 15-55." But the .env currently has 95-99, and the band should be 95-99 per the CORRECTION — yet the actual code default and .env both set it to 95-99, and the backtest finds NEGATIVE EV there. The REALITY TAX (2026-11-12) shows: band 95-99c + minutes 6-11 + taker + fee = -0.29c/trade; maker resting at ask-1 = -4.06c (dead); taker + model gate = +1.03c. The bot's own gate with MODEL_TYPE=gbm, MODEL_VOLATILITY=0.80 returns -0.01c. The whole edge is ~1c/trade and depends on calibrated vol model — the fixed 0.80 is not validated against live outcomes.

**Fix:** The band is correct at 95-99 per the 2026-11-12 CORRECTION, but the MODEL_VOLATILITY=0.80 fixed vol must be replaced with dynamic per-minute vol from the feed (MODEL_VOL_DYNAMIC=true), and the model gate must be calibrated. Do not change ENTRY_BAND_MIN/MAX; fix the model vol instead. Set MODEL_VOL_DYNAMIC=true and ensure sigma_per_min feeds into _model_volatility (signals.py:304-318). The edge vanishes without calibrated vol.

**If skipped:** Bot never trades. Balance stays flat.

### 2. sigma unit name loss (ATR vs per-sqrt-minute) biases q downward (gates.py:358-413, config.py:176-177, .env:GATES_ATR_HURDLE=1.0)

**What it is:** `cushion_z()` needs sigma in dollars per sqrt-minute. The code at gates.py:358-359 reads `sigma_per_sqrt_min` first, then falls back to `sigma_per_min`. But `sigma_per_min` from binance_feed.py:370 returns *standard deviation of 1-minute log returns* (not average true range). When `sigma_per_min` is missing, gates.py:412-413 falls back to `atr * sqrt(pi/2)` which converts ATR to per-sqrt-minute. However the key name `sigma_per_min` is misleading — it is actually per-minute standard deviation, not per-sqrt-minute.

**Problem:** In `_evaluate_common`, gate 3 reads `spot_state.get("sigma_per_sqrt_min", spot_state.get("sigma_per_min"))`. If the caller builds spot_state from `spot_for()` + `atr_1m()` only (as prefetch does at gates.py:174-201), neither key is `sigma_per_sqrt_min`, so it falls through to `sigma_per_min` which is std-dev-of-returns. The code comments at gates.py:406-411 acknowledge: "atr_1m is an average *true range* (E|X| over one candle, ~0.798*sigma), not a standard deviation, so scale by sqrt(pi/2)=1.2533 to make it one." But the fallthrough to `spot * 0.0007` at gates.py:413 is dollars-per-sqrt-minute and is fine as-is. The measured ETH q=0.8442 did not match the corrected sigma variant exactly because the logged strike is not the same 0.995 spot assumed.

**Fix:** Rename the `spot_state` key from `sigma_per_min` to `sigma_per_sqrt_min` so the unit is in the name, AND apply the `atr * sqrt(pi/2)` conversion when only ATR is available (gates.py:412). Document that `spot * 0.0007` is dollars-per-sqrt-minute and is fine as-is. The `sigma_per_min` function in binance_feed returns per-minute std-dev which should NOT be used directly in cushion_z without the pi/2 scaling.

**If skipped:** q is biased toward 0.5 by ~0.02-0.05 in the 91c-96c region. Gate 2 then rejects more trades than it should. The bias stops trading, which is why the bot is silent.

### 3. Gate 2 (fee-adjusted EV) blocks all trades when model q is only slightly above price + fee (gates.py:451-470, config.py:194, .env:GATES_MIN_EDGE=0.0)

**What it is:** Gate 2 checks if `net_ev(q, p, fee) >= GATES_MIN_EDGE`. With `breakeven_q(p, fee) = p + fee`, a trade passes only when model win probability q exceeds price p plus fees. The measured data shows all three favourites in the 91c-96c region have q (0.8213/0.8502/0.8733) vs p (0.94/0.91/0.92) plus fees — all negative. Nine tradeable brackets, zero orders, balance flat at $104.

**Problem:** GATES_MIN_EDGE defaults to 0.005 (0.5c). But the Brownian model has no drift, so inside the band q ~= p and EV lands on ~0 by construction. The GATES_MEASURED_Q floor (0.9928) at config.py:214 is the only thing that makes 95-99c reachable — it replaces model q with measured win rate. With the floor OFF, gate 2 blocks everything because model q barely clears breakeven.

**Fix:** The existing `GATES_MEASURED_FLOOR_ENABLED` and `GATES_MEASURED_BAND_MIN/MAX_CENTS` config is the correct opt-in path. Turn ON `GATES_MEASURED_FLOOR_ENABLED` together with dynamic vol model. Do not set `GATES_MIN_EDGE` to negative values to force trades — that hides the defect. The floor is opt-on for a reason: "with it OFF gate 2 scores the model q and the band is blocked like everything else. Turn it on only once the model itself clears breakeven in that band."

**If skipped:** Balance stays at $104. Zero fills.

## Should fix

### 4. cushion_z sigma fallback uses wrong key, falls through to std-dev without pi/2 scaling (gates.py:358-359, gates.py:412-413)

**What it is:** In `_evaluate_common`, gate 3 resolves sigma as `spot_state.get("sigma_per_sqrt_min", spot_state.get("sigma_per_min"))`. The key name inconsistency means callers passing `{"spot": ..., "atr_1m": ...}` (as prefetch does) get `sigma_per_min` which is std-dev of returns, not per-sqrt-minute. The code then at 412-413 applies `atr * sqrt(pi/2)` but only when `sigma is None` and `atr is not None`. If `sigma_per_min` is present (even though it's the wrong unit), the `atr * sqrt(pi/2)` path is never reached.

**Problem:** When the sigma fetched is std-dev of 1-minute returns (sigma_per_min), and atr is also available, the code goes to the `elif sigma is None` branch... wait, let me re-read. Actually: `sigma = spot_state.get("sigma_per_sqrt_min", spot_state.get("sigma_per_min"))`. If `sigma_per_sqrt_min` IS in spot_state, use it. If NOT, fall back to `sigma_per_min`. Then: `if sigma is None:` → apply `atr * sqrt(pi/2)` or `spot * 0.0007`. But if `sigma_per_min` IS present (as a float), sigma is NOT None, so the scaling is skipped entirely. The std-dev-of-returns value goes straight into cushion_z.

**Fix:** Either (a) always apply the `atr * sqrt(pi/2)` conversion when the sigma source is `sigma_per_min` (not `sigma_per_sqrt_min`), or (b) rename the key consistently. The simplest fix: in `_evaluate_common`, after resolving sigma, if sigma came from `sigma_per_min` key (not `sigma_per_sqrt_min`), multiply by `sqrt(pi/2)` to convert to per-sqrt-minute units. Or better: document and enforce the key naming.

**If skipped:** q biased toward 0.5 by 0.02-0.05, gate 2 rejects more trades, bot stays silent.

### 5. Gate 3 time-decay uses atr_1m without pi/2 scaling when sigma_per_min is present (gates.py:405-413)

**What it is:** Same as finding 4 — when `sigma_per_min` is in spot_state (the fallback), it's used directly in cushion_z without the pi/2 scaling factor that converts ATR (E|X|) to sigma (std-dev per sqrt-minute).

**Problem:** The atr_1m function computes average true range over 1-min candles. For a zero-mean Gaussian, E[|X|] = sigma * sqrt(2/pi) ≈ 0.7979 * sigma, so sigma = atr * sqrt(pi/2) ≈ 1.2533 * atr. The code acknowledges this at gates.py:406-411 but only applies the scaling when `sigma is None`. If `sigma_per_min` is present (even wrong), the scaling is skipped.

**Fix:** After the sigma resolution at gates.py:358-359, add: if sigma came from the `sigma_per_min` key (not `sigma_per_sqrt_min`), multiply by `math.sqrt(math.pi / 2)` to convert. Or rename the key consistently in all callers.

**If skipped:** Same bias toward 0.5, same silent bot.

### 6. `_write_log` dedup key misses the reason, so unchanged decisions are silent (gates.py:282-291)

**What it is:** `_write_log` uses `key = (ticker, side)` and drops the record if `_last_decision[key] == rec["decision"]`. This means if a market stays "block" for two hours but the reason changes from "gate2 EV" to "gate1 no spot", only one line is written.

**Problem:** Diagnoses stay manual. You need to know *why* a trade was blocked, and the dedup hides the reason change.

**Fix:** Change the key to include the first reason: `key = (ticker, side, decision, reasons[0] if reasons else "")`. One line change.

**If skipped:** Diagnoses stay manual, harder to debug why trades aren't filling.

### 7. `entry_fee` fails open on out-of-range price (gates.py:227-228)

**What it is:** `if not 0.0 < p < 1.0: return 0.0`. A price passed in cents (p=91) treated as "no fee" instead of an error.

**Problem:** A unit mistake makes EV look better than it is, then vanishes when someone fixes the caller, with no trace.

**Fix:** Add `log.warning` on the branch. One line.

**If skipped:** Not a seen bug today (trader.py passes cents/100), but removes the only signal the would appear.

### 8. trader.py:1015 second volatility model hardcoded as constant sigma_min=0.0007 (trader.py:7-like)

**What it is:** The ladder path defaults to `0.0007`/sqrt-minute, the gate path uses measured ATR instead. Two volatility models in one process.

**Problem:** Only matters if the ladder path is later wired into the trading loop. AGENTS.md says it is not.

**Fix:** Note it once; do not fix it now. The ladder path is not used in the main flow.

**If skipped:** No change today.

## Nice to have

### 9. `_write_log` de-duplicates on decision only (gates.py:275-291, revisited)

This is the same as finding 6 but noted again under "nice to have" since it's a logging quality improvement, not a blocker.

### 10. No test for `cushion_z` unit function (tests/test_gates.py)

**What it is:** See the test note in Finding 2.

**Problem:** The one function that carries the unit assumption that broke is the one function with no test pinning it down.

**Fix:** Three asserts, no network needed. ~10 lines.

**If skipped:** Finding 2 comes back the next time the fallback is "tidied".

---

**Verdict:** The repo is well-structured with 429 passing tests. The two critical issues preventing trades are: (1) the model volatility FIX — set MODEL_VOL_DYNAMIC=true and ensure per-minute sigma feeds into _model_volatility, and (2) the measured floor — turn on GATES_MEASURED_FLOOR_ENABLED with the 95-99c band. Do not widen the entry band back to 15-55; the live data shows negative EV there. Fix the vol model calibration first.

**Lean: -0 lines, -0 dependencies possible.** (No code deletion needed; fixes are in-line edits.)

**Not checked:** Whether a maker order at 55c or below fills in these windows (dry run fills every maker order at the posted price, no fill-probability model). Whether Kalshi's real exit/settlement fee for crypto markets vs fees.taker_fee_dollars matches. Whether BTC 15M drift over a 15-minute window is zero (assumed). Whether AGENTS.md's 52.8% win rate reproduces from the backtest.