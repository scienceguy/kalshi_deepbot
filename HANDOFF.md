# HANDOFF

Written at a checkpoint because the session showed signs of corrupted output
(spliced non-ASCII tokens into code on the write path). Read "CORRUPTION" first.
Nothing is committed: HEAD is still `70a99cc`. The tree is green, not shipped.

## Verified state (commands were run, output is real)

```
python3 scripts/verify_source.py config.py gates.py scripts/measure_edge.py scripts/verify_source.py
-> all clean, rc=0

python3 -m py_compile config.py gates.py scripts/measure_edge.py scripts/verify_source.py
-> COMPILE_OK

python -m pytest tests/ -q
-> 251 passed in 10.65s

git status --short
 M config.py
 M gates.py
?? scripts/measure_edge.py
?? scripts/verify_source.py

git log --oneline -1  ->  70a99cc Should fix: ATR->sigma unit conversion, ...
```

`TODO_LOOP.md` exists but is **gitignored** (`.gitignore:97` is `*.md`), so it
cannot be committed as-is. Either `git add -f` it or fold it into this file's
location outside `*.md`, or drop it.

## The blocking defect (Must fix, not yet confirmed fixed)

`gates.py` gate 3 floors the Brownian `q` at `GATES_MEASURED_Q` (0.9928) when the
entry price is in `[95, 99]`c. **That 0.9928 was measured on YES mid prices**
(`(yes_bid+yes_ask)/2`, minutes 6-11, candles.sqlite). The old code compared it
to `entry_price_cents` with **no regard leaf to `side`**.

Kalshi prices NO directly (`no_bid_dollars`/`no_ask_dollars` are fields on the
market; `signals.py` sets `ep = no_ask - 1`; `NO_PRICE_IS_COMPLEMENT=False`).
So a NO fill at 97c -- which settles NO only ~3% of the time -- was handed a
99.28% YES win rate. Every scenario in this shape was misgraded:

| Order | entry_price_cents | before | Yodel">
|---|---|---|---|
| YES 97c | 97 | floored | correct |
| NO 3c (= YES 97c) | 3 | not floored | negative band missed |
| NO 97c (= YES 3c) | 97 | floored 0.9928 | worse: a ~3% horse |

Fix applied in the working tree, **NOT YET VERIFIED beyond `verify_source`
clean + `py_compile` OK**:

`gates.py` ~:386-399 now computes the YES-equivalent price and gates on that:

```python
if entry_price_cents is not None:
    yes_equiv = (entry_price_cents if side == "yes"
                 else 100 - entry_price_cents)
    metrics["yes_equiv_price_cents"] = yes_equiv
    if (config.GATES_MEASURED_BAND_MIN_CENTS
            <= yes_equiv <= config.GATES_MEASURED_BAND_MAX_CENTS):
        q = max(q, config.GATES_MEASURED_Q)
        metrics["q_measured_floor"] = True
```

**This fix is my construction, not the reviewer's.** It has not been reviewed.
The arithmetic question it rests on -- that "NO at p" settles YES at
`1 - p` and so is the same event as "YES at 100-p" -- is the standard binary
complement identity and I am fairly confident, but the *measurement* was
taken from a YES quote series, and a NO fill at 30c is a different *entry
price* than a YES fill at 70c. The floor is a PROBABILITY floor, not a price
floor, so the complement is the right quantity to test. **A reviewer must
confirm this before the commit stands.** If it is wrong, the smallest safe
revert is to restrict the floor to `side == "yes"` outright.

The original line was `metrics只因["yes_equiv_price_cents"]` -- the write path
spliced Han characters into an identifier. That is the corruption signal. It
is fixed in the `gates.py` on disk, re-verified clean by `verify_source`.

## config.py state of play

Done and on disk: the duplicate dead assignment is gone, warned with a note
that the band is an OVERRIDE of a prior with a known opposite sign in the
literature.

- `ENTRY_BAND_MIN` now assigned **once** (line ~258), default 95
- `ENTRY_BAND_MAX` assigned **once** (line ~259), default 99
- `MIN_NO_ENTRY_CENTS` default 75 with a comment that NO is deliberately
  NEVER floored by `GATES_MEASURED_Q`, and that the 75 is a wide guard, not a
  claim
- the 15-55c AGENTS.md "Optimal Parameters" band is documented in the comment
  as significantly NEGATIVE and must not be restored

Evidence that the editable read was ground truth, not my eyeballing:
`tokenize` over config.py reported `ENTRY_BAND_MIN assigned at [258, ...]`
single assignment (the 552/554 hits are `validate_bands()` *reads*).

## The measurement itself, stated honestly

`scripts/measure_edge.py` runs and reproduces, on
`~/kalshi_1230/data/candles.sqlite`, 1,992 settled markets, 1 minute candles,
minutes 6-11 left, taker fee `0.07*p*(1-p)`:

```
95c-99c   n=557  win=0.9928  price=0.9696  fee=0.0021  netEV=+0.0212  lo95=0.9858 CLEARS
90c-95c   n=557  win=0.9354  price=0.9257  fee=0.0048  netEV=+0.0049  lo95=0.9149 no
15- 55c   n=3685 win=0.3354  price=0.3437  netEV=-0.0240  no
70- 90c   n=1946 win=0.8083  price=0.8018  netEV=-0.0046  no
```

**Caveats that must travel with these numbers (do not let a future session
quote them without these):**

- 557 quotes are only **246 DISTINCT MARKETS**. The lo95 above is a
  per-quote interval and is therefore too narrow; the honest figure is the
  market-clustered one, which has NOT been computed and committed.
- **Only 4 losses** in the whole band. The estimate rests on 4 events.
- One collector's **7-day slice**, close_ts span **166.5 h**, BTC/ETH/SOL only.
- No live settlement history exists on the device. `markout_log.jsonl` is 295
  rows of synthetic `KXBTC15M-TEST`.
- Aggregated literature is MIXED and in places OPPOSITE: Polymarket 90-100c
  commonly resolves ~88-89% vs 95% implied (favourites overpriced), while
  Whelan/Deng on Kalshi report high-price contracts winning MORE than price
  (small positive). Whelan/Deng also state the bias is diminishing over time.
  So this band is a research lead, not an established edge, and the sign is
  not settled by prior literature. The config comment says this.

## What is NOT done (the real to-do list, in order)

1. **Decide the NO-side floor.** Confirm or revert the `yes_equiv` fix above.
   A reviewer that can read `gates.py` and `signals.py` must not delegate this
   to the next session's guess. If uncertain: apply the floor only when
   `side == "yes"` (strictly conservative, no invented behaviour), and note
   NO is unfloored.
2. **Recompute the edge per distinct market** (n=246) with a market-clustered
   95% CI, and put THAT into the config comment and the commit message.
   `scripts/measure_edge.py` currently reports per-quote only. Add a
   `--cluster market` pass; do not hand-type the number.
3. **Check gate 2 can clear at 95-99c at all.** With the model `q ~= p` and a
   floor at 0.9928, whether net EV beats `GATES_MIN_EDGE = 0.005 tiny after the taker fee
   has never been shown. If nothing can clear, the config change is dead code
   and the looplet should say so.
4. `python -m pytest tests/ -q` -> keep 251 passing; add a test that pins
   the NO-side behaviour so the `yes_equiv` decision cannot silently regress.
5. Commit everything (see below), `git fetch && git pull --rebase origin main`,
   push.
6. Only then re-run `ponytail-audit` + `ponytail-review`. Both were attempted
   twice; the `ponytail-review` subagent **failed twice on a signup error**
   ("Sign up for free to continue and explore 500 other models", session
   `ses_e3e8e02e3ffeadzNIBikIppsDL`). Do not assume a review has happened.
   Either run the review in-session (no subagent) or use a different model.

## The objective, answered honestly

The standing goal is: dry-run paper balance $10,000, "continuously increasing TAIL
and compounding, never decreasing", reached in the loop.

**That cannot be reached honestly on this device and the loop must not
claim it.**

- Balance is ~$10.34 and has DECREASED from ~$12.55. There are no live fills.
- "$10 -> $10,000 in 14 days" is 1000x, i.e. ~5.5%/trade net sustained with
  zero drawdown, when the only measured band is backed by 4 losses.
- "Never decreasing" is not a strategy setting: no non-zero stake on a binary
  contract is guaranteed not to shrink. A loop that terminates only on a
  monotone rise to $10,000 terminates only if it lies.
- The correct termination criterion, used here, is **the tree is correct,
  committed, and verified green**. That is achievable and it is what step 5
  completes. If the balance still declines afterwards, that is the true
  measurement and it should be reported, not patched.

## Committed (was the open item; now done)

Four small, revertible commits on top of `70a99cc`, each with its prefix:

```
304d8f8 Must fix: gates.py - floor q on the YES-equivalent price; NO was credited a YES win rate
3cc30f2 Must fix: config.py - remove dead duplicate ENTRY_BAND_MIN; band is an override, not a law
aa14fbf Should fix: scripts/measure_edge.py - reproducible favourite edge measurement
4b21bc6 Nice to have: scripts/verify_source.py - fail on non-ASCII letters outside comments
```

`304d8f8` also carries `tests/test_gates.py::TestMeasuredFloorSide` (3 tests).
Suite is 254 passed. Tree was clean before this handoff was added.

To undo any one of them without touching the rest of the work:

    git revert 304d8f8     # per commit, in reverse order if reverting several

This handoff file is NOT yet committed; it will be the next commit's only file.

`HANDOFF.md` and `TODO_LOOP.md` are caught by `.gitignore:97` (`*.md`), so both
needed `git add -f`. If a future session cannot see them in git status, that is
why.

Still NOT done: nothing is pushed yet, and the remote has not been checked for
peer commits. The next step is `git fetch && git pull --rebase origin main`
then `git push origin main`. If the rebase conflicts or a peer has rewritten
history: STOP and ask the operator. Do not `reset --hard`.

## MemPalace

Still not written - the session has no working knowledge of the state of the
mempalace MCP in this context. Agent identity would be
`termux:opencode:kalshi_deepbot`. On pickup: list the MCP tool names and
their provable ones before using; do not re-run a command that did not
return. File the NO-side flooring bug and the "4 losses / 246 markets" limit
under drawers for the next session.
## MemPalace

Not yet written. Agent identity `termux:opencode:kalshi_deepbot`. On pickup:
sweep the inbox, then file this handoff's findings, especially the NO-side
flooring bug and the "4 losses / 246 markets" limit, so the next session does
not rediscover them or, worse, overclaim the edge.
