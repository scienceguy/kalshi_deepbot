# HANDOFF — Kalshi DeepBot

Written at a verified green checkpoint. New session: read this first, then
`git log --oneline -7`. Nothing here is from memory; every number was read
from a file or a command this turn, with the command named beside it.

Repo:  https://github.com/scienceguy/kalshi_deepbot   (private, main)
Dir :  /data/data/com.termux/files/home/kalshi_deepbot
HEAD:  dedc68a "Nice-to-have: verify_source.py - --docs-only and --prose for markdown"

## CORRECTION to earlier handoffs (read this)

Earlier turns of this session claimed the paper balance was
**~$10.34, down from ~$12.55**. That was **stale, carried from memory across
turns and never re-read**. The actual state file `paper_state.json`
(gitignored, `.gitignore:48`) says:

    start_balance 100.0
    pnl            +4.0      (settlement_pnl +4.0, exit_pnl 0.0)
    ledger         1 entry: {"type":"settlement","pnl":4.0,"reason":"settlement replay"}
    balance        104.00    (+4.0%)

So: balance went UP, and the only P&L is a **settlement replay**, not a fill
and not a live trade. $104 is not progress toward $10,000 -- it is an empty ledger
with one synthetic entry. Everyone after this should read paper_state.json,
not prose.

## Verified state

    git rev-list --count HEAD      -> 230
    git status -sb                 -> ## main...origin/main   (clean, in sync)
    git remote get-url origin      -> https://github.com/scienceguy/kalshi_deepbot.git
    python -m pytest tests/ -q     -> 263 passed
    scripts/verify_source.py       -> all clean (rc 0; --all is CODE only)
    scripts/verify_source.py --docs-only --prose -> advisory, rc 0

Last eight commits:

    dedc68a Nice-to-have: verify_source.py - --docs-only and --prose for markdown
    22401ca Must fix: ASSESSMENT.md - remove engine splices from money-path arithmetic
    dd8904b Should fix: track the planning/audit docs; let the gate see markdown
    00d1798 Should fix: verify_source.py - stop failing on emoji variation selectors
    438135f Must fix: identity - agent is termux:opencode:kalshi_deepbot, not kalshi_superbot
    5773849 Should fix: HANDOFF.md - correct the stale pushed claims; record the integrity audit and the MemPalace lock
    df71e18 Nice to have: HANDOFF.md - torch for the next session at a verified green checkpoint

Each is one `git revert <sha>` away from undone. The repo move is recorded at
`~/tmp/opencode/origin_backup.txt` (temp dir: copy it somewhere permanent).

## The repo move (done this session)

This checkout used to push to `scienceguy/kalshi_superbot`, a name that did not
match the directory. It now pushes to a **new private** repo
`scienceguy/kalshi_deepbot`:

    gh repo view scienceguy/kalshi_deepbot -> {"name":"kalshi_deepbot","visibility":"PRIVATE","defaultBranchRef":{"name":"main"}}
    git push -u origin main               -> * [new branch] main -> main
    git rev-parse HEAD origin/main        -> 5773849... 5773849...

`main` now TRACKS `origin/main`; before the move it had no upstream and every
push needed the branch spelled out. The old `kalshi_superbot` remote was left
untouched at `df71e18` (no force-push, no history rewrite).

Before pushing, history was checked for leaked secrets: `.env` never
committed; every `-----BEGIN` hit is a header placeholder in docs or an error
string, with no key body. Clean to carry forward.

## What was fixed (with the evidence that justified it)

### 1. gates.py floored a NO fill with a YES win rate  (304d8f8)

`GATES_MEASURED_Q = 0.9928` is the realized win rate of a **YES** favourite quoted
in 95-99c. Gate 3 applied that floor to any `entry_price_cents` in 95-99c
**without reading `side`**. Kalshi prices NO directly (`no_bid`/`no_ask`,
`signals.py: ep = no_ask - 1`), so a NO fill at 97c -- a contract that almost
certainly settles NO -- was scored a 99.28% winner.

The fix gates on the YES-equivalent price: `100 - p` for NO. Measured on this
device's own candles, the two sides are mirror images:

    95-99c quotes : n=557  settle YES 553  -> 0.9928
     1-5c  quotes : n=560  settle YES   6  -> 0.0107 (6/560)

A NO @ 97c is the second row. The old code was wrong by ~98 points on it (0.9928 vs 0.0107).
**Still awaiting a human reviewer;** 3 tests
(`tests/test_gates.py::TestMeasuredFloorSide`) pin the behaviour, but my tests
are not a review.

### 2. config.py assigned ENTRY_BAND_MIN twice  (3cc30f2)

Line 239 `75` was dead on arrival; line 249 `95` overwrote it. Readers of the
first line (and my own earlier todo) believed discovery started at 75c. Token
scan confirmed the surviving single assignment. The 95-99c band is now
documented as an OVERRIDE of a prior whose sign is unsettled, not a law.

## The edge, stated soberly

    scripts/measure_edge.py --cluster market   (honest unit)

    95-99c : n=246 markets  win 0.9878  price 0.9682  netEV +0.0175  lo95 0.9741
             breakeven 0.9704 -> clears by ~0.4c, not the 1.3c the per-quote
             interval (n=557) flatters it into
    15-55c : n=3685 quotes  0.3354  -0.0240   clearly NEGATIVE
    90-95c : +0.0049, lower bound below breakeven -> not significant

Limits, all of which must travel with the number:

- 557 quotes are **246 distinct markets**; a settlement is shared inside a market.
- **4 losses** carry the whole estimate.
- one collector's **7-day slice**, 166.5h, **BTC/ETH/SOL only**.
- The literature disagrees in **sign**: Polymarket 90-100c ~88-89% vs 95% implied;
  Whelan/Deng on Kalshi the mirror. Bias documented as decaying.
- The AGENTS.md "Optimal Parameters" 15-55c band is significantly NEGATIVE here;
  do not restore it.

## Objective status: the $10,000 criterion is UNMET and NOT PURSUED

    balance = 104.00, one synthetic settlement replay, no live fills
    target  = 10000    -> needs ~96x, with zero drawdown, "never decreasing"

Recorded plainly rather than attempted by numbers: a non-zero stake on a binary
can lose, so "monotonically increasing" is not a property any live strategy can
promise, and the only kind of work that could *report* it would be a fabricated
balance. The termination criterion used instead was: tree correct, committed,
verified green. That is met (above).

What would actually move this: real settled fills over weeks. 246 markets rests on
4 losses; no parameter sweep can create evidence that is not in the data.

## Blocked on you (cannot be done here)

1. **Reviewer for 304d8f8.** The NO-side floor arithmetic is now backed by the
   1-5c measurement, but it is live money-path code committed by the same session
   that found the bug. Needs a human to read `gates.py` around the floor once.
2. **MemPalace is locked.** `mempalace status` this turn:

       Error opening palace at /opt/mempalace/palace: held by PID 32479
       (/opt/mempalace_venv/bin/mempalace-mcp)

   Same PID as last turn, still holding it. No read, no write, no event posted --
   do not claim otherwise. Either stop that holder (it is another agent's
   mcp server; killing it mid-write can corrupt the palace) or run against its
   path. Pickup commands:

       mempalace --palace /opt/mempalace/palace status
       mempalace event list --to-agent termux:opencode:kalshi_deepbot --preview

   Findings to file once unlocked: the NO-side flooring bug; the
   4-loss/246-market limit; the stale-balance error (below).
3. **ponytail-audit and ponytail-review have produced ZERO findings.** Two
   subagent dispatches both died on a signup gate ("Sign up for free to continue
   and explore 500 other models"). There is no clean audit here -- there is no
   audit. Run it in-session, or on a model that does not need that gate.
4. **10 planning `.md` files are invisible to git** via `.gitignore: (line
   matching *.md)`: TODO_LOOP.md, TODO.md, COMPREHENSIVE_TODO.md Elf,
   PONYTAIL_AUDIT.md, MEMPALACE_GUIDE.md, TODO_FIXES.md and others. Decide: track
   them (`git add -f`, or negate the pattern in .gitignore) or delete them.

## Standing trap: this session's write path corrupts source

Observed this session, all in committed-or-near-committed Python:

    r["系列"] for a dict key      -> reads None at runtime, compiles fine
    metrics只因["x"] = ...        -> caught by verify_source.py
    95 senses)                   -> caught by verify_source.py
    getedi / minded              -> ASCII words: CAUGHT BY NOTHING
    import io quay               -> stray import

`python -m py_compile` passing means **nothing**. Verify every write with:

    LC_ALL=C grep -n '[^ -~]' FILE        # non-ASCII, incl. inside strings

and `grep` the diff for ASCII-word splices by eye, because no tool here catches
those. `scripts/verify_source.py --all` catches the non-ASCII letter class only;
run it anyway, as a floor.

Second trap, newly documented: **do not trust your own reading of terminal
output either.** Three times this session a "corruption" was reported
(`auditvideocam`, `palaceista`, `entropy`) that `grep` showed was not on disk. The
reliable pattern is: state a claim, run the byte check, report the *check's*
output. Never the memory of it.

## Next steps, in order

1. Get a human to read the `304d8f8` floor block in `gates.py`. If wrong reverting
   is one command: `git revert 304d8f8`.
2. Fix the gitignore so the TODO/audit docs are either tracked or gone. Do this before
   anyone loses them to a worktree cleanup.
3. Once the MemPalace lock is free, file the three findings above.
4. Run ponytail-audit in-session. Work the MUST FIX list. Only then anything
   about strategy.
5. Re-run `scripts/measure_edge.py --cluster market` on new data before
   changing any band constant. If the clustered lower bound stops clearing
   breakeven, the band goes back to the wider default -- that is the honest
   outcome, not a failure.
