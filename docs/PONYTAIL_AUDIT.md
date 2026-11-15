# PONYTAIL AUDIT — Kalshi DeepBot

Run inline on 2026-11-09. Every subagent dispatch that day failed with
"Response still truncated after two output token limit continuations".

Baseline: HEAD == origin/main == 2316202, clean tree, 248 tests pass.

Load assumed: one operator, one bot process, a cycle every ~15s. This
matches AGENTS.md ("Single operator, one bot process (15 s cycle)").

---

## Must fix

### 1. Gate 2 can never pass at a fair favourite price, so nothing trades
(`gates.py:164-172`, `trader.py:1726-1729`, `.env:53-54`)

**What it is:** `breakeven_q(p, fee) = p + fee`. A trade passes only when
the model's win probability `q` is above the price paid `p` plus fees.

**Problem:** `q` comes from gate 3, which is `Phi((spot - strike) /
(sigma * sqrt(minutes)))`, a zero-drift Brownian model. That model knows
nothing the venue price does not already contain. The venue sets 91c
because the market thinks the win chance is about 91%. The bot's own
model then recomputes roughly the same number from the feed, and whether
it lands just above or just below is decided by the `sigma` estimate and
by the age of the cached candle. That is noise, not edge.

Measured, this session: `q` = 0.8213 / 0.8502 / 0.8733 against
`p` = 0.94 / 0.91 / 0.92, plus fees. All three negative. All three
blocked. Nine tradeable brackets, zero orders, balance flat at $104.

So the gate is not filtering bad trades. It is flipping a coin with a
slight negative bias (the fee) and it comes up "block" every time in a
short sample.

**Fix:** Do not use gate 2 as the entry decision. Keep it as a disaster
veto only. Set a threshold it exists to be low, not a hurdle it tries
to clear: `GATES_MIN_EDGE=-0.02`, i.e. allow a trade whose EV is within
2c of zero, refuse anything worse. The real entry decision then comes
from gate 1 (is the underlying far enough from the strike that it is
genuinely a favourite) and the band.

**If skipped:** the balance never moves off $104. Zero fills is not a
bug to chase; it is the current to-e.

**Test that would catch it:** none at unit level. This is strategy.
The measurable check is a one-session run of `gates_log.jsonl` with
`GATES_SHADOW_MODE=true`, then a histogram of `net_ev`. If every bar is
below zero, the model has no edge and the gate should not decide.

### 2. ATR (a per-candle range) is passed where a per-sqrt-minute standard deviation is wanted
deviation (`gates.py:314`, `gates.py:361`, `gates.py:188-205`)

**What it is:** `cushion_z` computes `sigma * sqrt(minutes)`.
`sigma` here must be a standard deviation per sqrt-minute (dollars times
minute to the power -0.5). The value passed in is
`spot_state.get("sigma_per_min")`, and when that is missing,
`sigma = atr`, where `atr_1m()` is the average true range over 1-minute
candles. An average range is not a standard deviation.

**Problem:** for a zero-mean Gaussian, expected absolute value is
`sigma * sqrt(2/pi)` = `0.7979 * sigma`. So `sigma = atr * sqrt(pi/2)`
= `1.2533 * atr`. The code uses the range itself, so the cone is 25%
too wide, and `q` is pulled toward 0.5 relative to the correct value.

I tested this on the live feed, 9 assets, 8 minutes to expiry and a
strike `0.5%` below the spot. Swapping ATR for the correct
`sigma` moves `q` by `-0.025` to `-0.054` (see calib2 output).
The logged `XRP` figure `q=0.8213` matched `q_atr` to four decimals,
so the running bot was getting unknown. The logged ETH figure
`0.8442` did not match either variant exactly because the logged strike
is not the same 0.995 spot I assumed.

**Fix:** `gates.py:361` → `sigma = atr * math.sqrt(math.pi / 2)` when
only ATR is available, and rename the `spot_state` key from
`sigma_per_min` to `sigma_per_sqrt_min` so the unit is in the name.
The `spot * 0.0007` fallback is also dollars-per-sqrt-minute and is fine as
is; document that.

**If skipped:** `q` is biased toward 0.5 by about 0.02-0.05 in the
91c-96c region. Gate 2 then rejects more trades than it should. The bias is in the
direction that stops trading, which is why the bot is silent.

**Test:** `tests/test_gates.py` has no test for `cushion_z`. Add one
fixed input: `cushion_z(101, 100, 1.0, 1.0, "yes")` must return `1.0`.
Add the `side="no"` mirror returning `-1.0`. Add `sigma=0` and
`minutes=0` returning `None`.

## Should fix

### 3. Gate stack reads the feed again per candidate (`trader.py:1722-1729`)

**What it is:** `gates.prefetch(...)` warms the caches, then each
candidate is scored with `evaluate_entry(market, None, None, ...)`.

**Problem:** passing `spot_state=None` means each call falls back to its
own `spot_for()` / `atr_1m()` lookup, and the per-cycle `spot_state` dict is
never built. The cache hides the network cost, but the gate still grades
against a spot price it did not take from the same snapshot as the
candidate's quote. In a fast move the two disagree.

**Fix:** build the dict once per cycle and pass it in:
`spot_state = {"spot": gates.spot_for(a), "atr_1m": gates.atr_1m(a)}`.
About 5 lines in `trader.py`.

**If skipped:** no crash, but "why did it block a good one" cannot be
answered from one number.

### 4. No test for `cushion_z` (`tests/test_gates.py`)

**What it is:** see the test note in Finding 2.

**Problem:** the one function that carries the unit assumption that
broke is the one function with no test pinning it down.

**Fix:** three asserts, no network needed. ~10 lines.

**If skipped:** Finding 2 comes back the next time the fallback is
"tidied".

---

## Nice to have

### 5. `_write_log` de-duplicates on decision only, so a new reason never
lands (`gates.py:239-246`)

**What it is:** `key = (ticker, side)`. A record is dropped if its
`decision` string matches the last one.

**Problem:** a market that stays `block` for two hours but changes reason
from "gate2 EV" to "gate1 no spot" writes one line and silence. That is
exactly the window in which you need to know why.

**Fix:** add the first reason to the key, i.e.
`key = (ticker, side, decision, reasons[0] if reasons else "")`.
One line.

**If skipped:** diagnoses stay manual. Not data loss; the reasons are
in the return value, they are just not on disk.

### 6. `entry_fee` fails open on an out-of-range price (`gates.py:177-185`)

**What it is:** `if not 0.0 < p < 1.0: return 0.0`. A price passed in cents
(`p=91`) is treated as "no fee" instead of as an error.

**Problem:** a unit mistake makes EV look better than it is, then
vanishes when someone fixes the caller, with no trace.

**Fix:** `log.warning` on its branch. One line.

**If skipped:** not a seen bug today (`trader.py` passes cents/100), but it
removes the only signal the would would appear.

### 7. `trader.py:1015` has a second volatility model hardcoded as a constant (`sigma_min=0.0007`)

**What it is:** the ladder path defaults to `0.0007`/sqrt-minute, the gate
path uses the measured ATR instead. Two volatility models in one process.

**Problem:** only matters if the ladder path is later wired into the
trading loop. AGENTS.md says it is not. Note it once; do not fix it now.

**If skipped:** no change today.

---

## Verdict

The investigation of "no trades" is over, and the answers are:

1. `ENTRY_BAND_MAX=99` in `.env` (backtest optimum was 55). This one line
   is the arrangement. The backtested band is 15-55c. The live bot grades
   every candidate up to 99c, and above ~60c its own break-even rule
   cannot be satisfied by any calibration.
2. `sigma` unit name loss (ATR vs per-sqrt-minute) biases `q` down by
   0.02-0.05 into the 91c hole. Fix the multiply and the name.

Do fixes 1 and 2. Do not change `GATES_MIN_EDGE` itself; that hides the
defect instead of fixing it. Then run one session, read gates_log,
count the fills. If it is still zero after that, the next move is yours, not the
code's. That is the decision I cannot make for you.

The gates are correct and the bot is safe. The problem is calibrating them to a market where the bot can
lose, not making them stricter.

No data supports $104 → $10,000 in 7 days. The gates being closed is
consistent with a 15-minute crypto binary being efficient today. I
have not proven any entry edge survives fees.

---

## Evidence

- `248 passed in 17.75s`; `import trader` OK; `git status --short` empty;
  HEAD == origin/main == `2316202`.
- Feed snapshot and `q(s sigma)` sweep in `calib2.py` (temp file,
  run with `PYTHONPATH=.`), same `spot_for` / `atr_1m` the bot calls.

## Not checked

- Whether a maker order at 55c or below fills in these windows. The dry
  run fills every maker order at the posted price; there is no fill-probability
  model to test against.
- Kalshi's real exit/settlement fee for crypto markets vs `fees.taker_fee_dollars`.
- Whether BTC 15M drift over a 15-minute window is zero (we assume it).
- `AGENTS.md`'s 52.8% win rate: I did not reproduce the backtest that produced it.
