# AGENT_TASKS v17 - kalshi_deepbot (FINAL-CANDIDATE)

Date: 2026-11-22
Supersedes: AGENT_TASKSv14.md, AGENT_TASKSv15.md, and AGENT_TASKSv16.md (v16 was
v15 renamed). IDs are now T17-nn. Section 3 lists claims in earlier versions that
later source reading corrected, so no agent acts on them.
Audience: OpenCode agents running on an OCI VPS.
Status: SUGGESTIONS. Every item is a proposal. Verify against the live tree
first, make the smallest change that meets the acceptance check, and record
evidence (command + output), not memory.

## AUTHORITATIVE EXECUTION DIRECTIVES (GOAL_v17_full.txt)

These directives consolidate the uploaded full goal and the earlier concise
v17 draft. They win over older prose and over conflicting task suggestions.

1. MISSION AND EVIDENCE. Maximize SAFE geometric net-PnL growth after real
   fees on Kalshi and Polymarket US, within the drawdown, daily-loss and ruin
   limits pre-registered in T17-50. The $3,250,000 figure is a forecast OUTPUT
   only; report P(target) even below 1% and never size or tune to it. Existing
   evidence is maker -3.2c to -4.06c, ungated taker -0.29c, and taker plus a
   calibrated-vol gate +1.03c (n=727). It is not split by side: NO-only is
   UNMEASURED until T17-08.
2. SESSION PROTOCOL. Sweep the logstream inbox, read AGENTS.md and query the
   single MemPalace before each task. Fetch/rebase safely, record HEAD, use the
   next task in ORDER, and preserve other agents' uncommitted work. Run the
   non-ASCII check, all four source gates and tests before every commit. Log
   command + output + exit status here and file the result in MemPalace.
3. DELEGATION. Maintain at least TWO delegated subagents actively completing
   independent, concrete tasks at all times until done (maximum five). Replace
   completed, failed or idle agents immediately and do not duplicate completed
   work. A DIFFERENT agent identity must review every money-path diff.
4. GATE ORDER. Complete T17-00..04 (including real production Polymarket US
   status and repository hygiene), then commit T17-50 before viewing new study
   results, then T17-06, T17-07 and T17-08. If no NO region has a positive
   market-clustered net-of-fee lower bound, STOP, write the conclusion and
   report to the human. Never widen bands, loosen gates, enable maker, or
   relax thresholds to create edge. A negative result is a valid deliverable.
5. NO-ONLY STRATEGY. Buy only Kalshi NO or Polymarket US Down and hold every
   position to settlement; only unfilled entry orders may be cancelled. Verify
   NO_ONLY tests and default ON. ALLOW_NO_BUYS stays OFF until T17-03 verifies
   the NO-side floor. Use taker plus a calibrated-vol gate only; hard-disable
   the maker branch. Trade only positive-lower-bound regions by NO price,
   minutes left, asset and venue. Size fractional Kelly on the LOWER edge
   bound with depth caps and ONE correlated-exposure budget.
6. DRY=LIVE PARITY. One code path and one config: strategy, gates, sizing,
   caps, ramp, fees and feeds are identical. Only the execution adapter, keys,
   state namespace and log paths may differ. Remove PAPER_PER_TRADE_PCT and
   all dry-only sizing. Log a config hash; live refuses to start unless it
   equals the dry-validated hash.
7. REAL DATA ONLY. Remove the synthetic 45c fallback and every invented price,
   tick, probability or fill. Dry fills require recorded production books and
   public prints; missing data means NO FILL, and unfilled orders are recorded.
   Backtests use production history, recorded production data or real fills
   only. Real maker fill rates require human-approved minimum-size live fills.
8. PILLARS. n8n runs through the job bridge and kill switch, never the hot
   path. SQLite is the only database; finish the Supabase purge. Keep exactly
   ONE MemPalace with MEMPALACE_DIR and MEMPALACE_EXPECTED_DIR and agents as
   clients. Use existing identities/logstream/leases with independent review.
   Entry is event-driven: spot older than 1.0 s means no trade; p95 submit
   latency is under 500 ms and p99 under 1 s. No blocking I/O, LLM, REST, DB,
   n8n or MemPalace call is allowed on the hot path.
9. LIVE PROMOTION. Stay dry until held-out real-data NO-only net EV has a
   market-clustered lower bound above zero over at least 300 settled markets
   and 14 days, then request human approval. Live uses the same config hash
   and T17-43 caps. Never enable --live, raise size or relax a cap without
   recorded human approval. Read balances at run time; never log keys or
   balances. Cross-venue pairs are basis deviation, not arbitrage.
10. STOP AND ASK the human for a money-path/spec contradiction, discovered
    secret, unclear production API status or venue rule, negative NO-only
    result, unexplained halt, or any step requiring live orders.

---------------------------------------------------------------------------
## V17 DELTA (what changed since v16 - read this first)

Source of the changes: a fresh read of the repo on 2026-11-22. AGENTS.md (release
423dbb5, 532 lines) is far more current than the root listing and README I had
used before, so many v16 statements are corrected below. In short:

1. EDGE EVIDENCE IS WORSE THAN v16 SAID. AGENTS.md records a 2026-11-12 "reality
   tax" measurement: maker resting at ask-1 lost 4.06c per trade (36.5% fill rate,
   win rate falling from 96.7% to 91.2% on fills: adverse selection), taker at the
   ask with no gate lost 0.29c, and only taker plus a calibrated model gate was
   positive at about +1.03c (727 trades, 98.6% win). The bot's own fixed-vol gate
   measured -0.01c. The ML model is better calibrated but trades 0 times because it
   learns the venue mid. None of this is split by YES vs NO, so NO-only is
   UNMEASURED. v17 makes a NO-only edge study a hard gate before more building
   (T17-08), and "no NO-only strategy clears the bar" is an acceptable conclusion.
2. DRY/LIVE PARITY IS CURRENTLY BROKEN IN CONFIG. AGENTS.md lists
   PAPER_PER_TRADE_PCT=0.95 (dry-run only) against 0.08 live, plus separate dry and
   live state. New task T17-06.
3. SYNTHETIC DATA STILL EXISTS. AGENTS.md known issue 19 says backtest_engine falls
   back to a synthetic 45c entry price when no quote exists. That violates R8. New
   task T17-07.
4. SUPABASE: AGENTS.md says it was removed on 2026-11-22 (local SQLite is the
   source of truth), but supabase_client.py, supabase/, README, AGENTS.md sections,
   the Supabase MCP tool, and pnl_reconcile's remote leg are still referenced.
   Purge is T17-61.
5. MUCH INFRASTRUCTURE ALREADY EXISTS: a MemPalace logstream protocol with agent
   identities, task.request/task.reply/patch.ready events and sha256-verified
   artifacts; a loopback job bridge for n8n (Execute Command node disabled); six n8n
   workflows; systemd timers for jobs and a credentials guard; a 4-gate
   source-corruption defence; 308 passing tests. v17 audits and enforces these
   rather than rebuilding them.
6. AGENTS.md ALREADY POINTS AT AGENT_TASKSv15.md as the active register. T17-00
   updates that pointer to this file and fixes the stale sections.

---------------------------------------------------------------------------
## 0. RULES OF ENGAGEMENT (apply to every task)

R1 Evidence rule. State a claim, run the check, report the check's output.
   Never repeat a number from prose, a handoff, or an earlier turn
   (HANDOFF.md already records one stale-balance error and one phantom +$4.00
   from a settlement replay).
R2 ASCII rule. Plain ASCII only in source and docs. After every write run
   `LC_ALL=C grep -n '[^ -~]' FILE` and `python scripts/verify_source.py --all`.
   `py_compile` passing proves nothing (HANDOFF.md "Standing trap").
R3 Reviewer rule. Money-path files (gates.py, risk.py, trader.py,
   poly_us_trader.py, fees.py, signals.py, kalshi_client.py,
   polymarket_us_client.py) need review by a DIFFERENT agent identity before
   merge. No self-approval.
R4 One change, one commit, one `git revert` away from undone. Never combine a
   refactor with a behavior change.
R5 Live is human-gated. No agent enables live trading, raises size, or relaxes a
   risk cap without a recorded human approval. `--live` plus the confirm flags
   (KALSHI_CONFIRM_LIVE, POLY_CONFIRM_LIVE) stay required.
R6 Snapshot rule. The source files used to write this document were read at
   DIFFERENT repo snapshots (gates.py at release 653be32; crossvenue.py,
   config.py at release 16a1187; poly_us_trader.py at 4a10310). First task is to
   re-verify everything on HEAD (T17-00).
R8 Real data only, never synthetic. Fills, prices, books, balances, and fees
   come only from production APIs, recorded production history, or our own real
   fills. No invented prices/ticks/slippage/fill odds, no sandbox or demo APIs, no
   interpolated gaps (missing data means no fill). Dry-mode fills are decided from
   recorded production books and public trade prints: a taker consumes real depth;
   a maker fills only if real prints trade through its price after the queue ahead.
   Real maker fill rates come from human-approved minimum-size live fills.
   Forecasts may resample or model REAL trade logs to project forward, but never
   feed fills, sizing, or balances from simulated data.
R9 No Supabase. Supabase is out of scope: remove supabase_client usage, config,
   env vars, and dependencies from every code path. Wherever this document speaks of
   a Supabase replica, outbox to Supabase, or Supabase reconciliation (F-12, T17-61,
   Pillar 2, T17-70), read it as: local SQLite only, with backups (sqlite3 .backup
   or litestream). Keep the n8n event outbox; it is a separate mechanism. Trading
   and reporting must work with no remote database.
R10 Shared-tree rules (from AGENTS.md; this document does not override them). One
   working tree (/opt/superbot), several agents, one main. `git fetch && git pull
   --rebase origin main` before every push. Never run `git reset --hard`,
   `git checkout -- <path>`, `git clean`, `git stash`, or force-push while the tree
   holds uncommitted work you care about; probe in a throwaway clone. Announce
   risky or cross-cutting changes on the logstream. Never claim a command
   succeeded without its output and exit status. Optional improvement: one git
   worktree per agent.
R11 Corruption gates. Before every commit the four gates must pass (verify_source,
   verify_compiles, verify_undefined, verify_operators, installed by
   scripts/install-pre-commit.sh) plus the tests. AGENTS.md documents what they do
   NOT catch (a defined name swapped for another, a condition moved, a deleted
   branch, a pure inserted condition), so every money-path diff also needs a second
   agent to read the diff against the stated intent and a test that references the
   change.
R12 Active register pointer. AGENTS.md names the active task file; keep it pointing
   at the newest version, and never leave two task files that both claim to be
   active.
R7 Targets never loosen risk. The $3.25M figure is evaluated by the forecast
   module. It is not a reason to widen caps, add martingale/doubling, or tune
   parameters until a backtest reaches it.

---------------------------------------------------------------------------
## 1. SUMMARY

Goal under test: trade Polymarket US and Kalshi using GBM, LLM evaluation, ML,
KL divergence, box arbitrage, and fair-value comparison, compounding from the
CURRENT REAL balances toward a $3,250,000 moonshot within 14 days on either
balance, with both balances growing. Backtests must produce honest 14-day
forecasts from several model families.

Inputs required: Binance WS spot, Coinbase WS spot, and the Kalshi reference
index. The request wrote "Kalshi BRIT"; source reading confirms the intended
object is BRTI (config.py and crossvenue.py both describe Kalshi's settlement as
the CF Benchmarks BRTI 60-second average).

The five pillars, each of which already exists in part and is hardened or
completed by this round:

1. n8n integration and workflow management. n8n on the VPS is the orchestration
   layer (scheduled backtests/retrains, settlement P&L routing, alerts, the
   kill-switch webhook, the agent task board, the daily forecast report).
   Already present: a typed event stream (emit.py), a signed durable outbox
   (N1), a job bridge with an optional gate key (J7/J11), per-identity HMAC for a
   two-person resume rule (J10), four injected workflows. n8n never sits on the
   tick-to-order path.
2. Local-first database. SQLite on the VPS is the only source of truth; no remote
   database (Supabase is being purged, R9). Already present: local DB with retention
   jobs, pnl reconciliation, and a sync layer that must be retired or repointed
   with the Supabase purge (backups via litestream or sqlite3 .backup). Open: the Polymarket trader
   still treats its JSON state file as authoritative for cash (finding F-12).
3. Full MemPalace utilization. One MemPalace service per palace; agents are
   clients. Already present: mempalace_bridge (status, checkpoints, halt and
   calibration drawers, knowledge-graph facts). Open: a direct palace open that
   bypasses the service, and no filing of backtests/forecasts/model cards.
4. Live inter-agent communication and collaboration. Named identities, a bus
   inbox, durable events via MemPalace, claim/lease on tasks and files, and the
   reviewer rule. Already present: agent_identity, bus_inbox retention, signed
   control lane. Open: claim/lease and enforced review.
5. Subsecond latency. Target p95 tick-to-order-submit under 500 ms, p99 under
   1 s, measured. Today the gates cache spot for 30 s and the main loop runs
   every 15 s (findings F-08, F-09), so the subsecond path is a build item, not a
   tuning item.

### Reality check the agents must carry (do not soften, do not delete)

Required constant daily growth g to turn balance B into $3,250,000 in 14 days is
g = (3,250,000 / B)^(1/14):

    B = $100       -> x32,500 total -> about +110% per day
    B = $1,000     -> x3,250        -> about +78% per day
    B = $10,000    -> x325          -> about +51% per day
    B = $100,000   -> x32.5         -> about +28% per day

Measured evidence in the repo itself is far below that:
- scripts/measure_edge.py --cluster market (HANDOFF.md): 95-99c band, 246
  distinct markets, 4 losses, net EV about +1.75c per contract, lower bound
  clearing breakeven by roughly 0.3-0.4c, one 7-day BTC/ETH/SOL slice.
- config.py comments: realized-vol GBM measured about +1.03c per trade over 14
  days against about -0.01c with fixed 0.80 vol; the 15-55c band is
  significantly negative; the aggregated prediction-market literature is mixed
  in sign for high-priced favorites.
- Order book depth in 15-minute crypto markets will cap position size long before
  six or seven figures.

Therefore the forecast is expected to report a very small probability of
reaching $3.25M. Report whatever the numbers say. Track the target as an output
of the forecast, never as an input to sizing.

### Evidence added in v17 (measured by the repo, 2026-11-12, per AGENTS.md)

Sample: third-party HF dataset papylove/kalshi-15m-data, 14 newest days, 15,420
windows, per-minute quotes, entry in minutes 6-11, hold to settlement, band 95-99c:
  idealized (fill at ask, no fee)           +0.43c/trade
  taker at ask, no gate                     -0.29c
  maker resting at ask-1                    -4.06c (fill 36.5%; win 96.7% -> 91.2%)
  taker + model gate                        +1.03c (n=727, win 98.6%)
  bot gate, fixed MODEL_VOLATILITY 0.80     -0.01c
  bands 15-55c / 75-95c                     -0.94c / -0.34c
Rough arithmetic from those numbers (assumptions flagged, none verified): +1.03c on
a ~97c stake is about 1.06% of stake; at 8% of equity per trade that is about
0.085% of equity per trade; 727 trades over 14 days is about 52 per day, so about
+4.4% per day if the edge is real, independent, filled at the ask with unlimited
depth, and stable. That compounds to roughly x1.8 to x1.9 over 14 days, which is
far below the growth the $3.25M target needs. The same numbers imply about 0.7
losses per day, and one loss at ~97c costs about 7.8% of equity at 8% sizing.
These figures are for both sides combined; NO-only is not measured (F-18).

### What "final iteration" means here

v17 is written to be the last strategy-feature round. It is final if, when its
MUST tasks are done, (a) every claim in this document has been re-verified on
HEAD, (b) the forecast module reports calibrated 14-day distributions for both
venues from live balances, (c) paper trading runs on the VPS with all five
pillars operating, and (d) the staged-ramp rules (T17-43) are in config. After
that, rounds should be data-driven maintenance only: fix what the forecast
coverage, reconciliation, and latency reports flag. A new strategy idea enters
only through the pre-registered evaluation (T17-50).

---------------------------------------------------------------------------
## 2. VERIFIED STATE (what source reading actually showed)

Read in full or in large part: README, HANDOFF.md, gates.py (542 lines),
crossvenue.py (614 lines), config.py (first ~1000 of 1142 lines),
poly_us_trader.py (first ~1000 of 3478 lines). Not read: trader.py, signals.py,
risk.py, fees.py, local_db.py, binance_feed.py, ml_model.py, llm_eval.py,
n8n_*.py, mempalace_bridge.py, emit.py, poly_us_stream.py, resting_guard.py,
latency.py, crossvenue_edge.py, scripts/, tests/, docs/, ASSESSMENT.md,
AGENTS.md. Everything about those is inferred from imports and config and is
UNVERIFIED.

Confirmed present (extend, do not rebuild):
- Kalshi: 9 series (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, ZEC, NEAR 15M),
  final-minute favorite taker/maker design, gates (ATR hurdle, fee-adjusted EV,
  time-decay cushion, optional pin-risk, momentum with Coinbase lead/lag, KL
  flags), fractional Kelly 0.25, 8% per trade, max 2 open positions, daily loss
  limit 5%, drawdown and cooldown halts, paper/live separation, live needs
  --live plus KALSHI_CONFIRM_LIVE.
- Polymarket US paper/preview engine: WS BBO with REST fallback, resting-order
  guard (1 s fast loop; cancels on spot move, 5-tick book move, stale feed, or
  halt), real orders.preview validation, exposure caps (25% per asset and per
  expiry), daily loss limit as the larger of $5 and 5% of equity (T13-03), ruin
  and venue-reconciliation halts (live mode), fee model per
  docs.polymarket.us/fees (Theta x C x p x (1-p), Tier 1 = 0.095 default,
  half-even cent rounding, makers free; scripts/fee_probe.py exists to confirm
  the real tier), calibration circuit breaker (Brier, Wilson-bound win rate,
  time-bounded halt, probe sizing).
- Models: GBM (MODEL_TYPE default "gbm", MODEL_VOL_DYNAMIC on), locked_share
  (BRTI average lock-in) option, momentum, optional ML gate (ML_MODEL_PATH,
  opt-in), LLM evaluation (cached verdicts with TTL, 3-model quorum, fail-open,
  enforcement OFF by default).
- Infrastructure: local DB tables for event_outbox, alert_history, bus_inbox,
  pending_send, latency_measurements, sync_quarantine, spot_ticks; latency budgets
  (guard tick 250 ms, signal eval 50 ms, order place 200 ms, settlement 100 ms)
  with the regression gate OFF; SUPERBOT_DATA_DIR isolation; BOT_STATE_HOST
  namespace (the VPS is the authoritative writer).
- Several T13-xx items are already applied (config comments cite T13-03,
  T13-12, T13-25). Treat v13 as partially or largely done and verify per item.

Polymarket US market availability (external, searched 2026-11-20):
- Reports say Polymarket US lists crypto up/down markets for BTC, ETH, SOL and
  XRP in 5-minute, 15-minute, hourly and 4-hour windows (BTC 15-minute launched
  on the US app 2026-09-22; ETH/SOL/XRP added 2026-10-15; 5-minute 2026-10-27).
- One report says the US contracts' stated resolution source differs from the
  international site's; another says settlement uses Chainlink oracles.
- CONFLICT: config.py (comment dated 2026-11-13) says crypto up/down markets
  were still in PREPROD on api.polymarket.us, that no crypto markets existed on
  the US retail API, and defaults POLYMARKET_ENABLED to false. Resolve in T17-01
  before any Polymarket crypto work: the app listing and the production API may
  differ, or the comment may be stale.

---------------------------------------------------------------------------
## 2b. VERIFIED STATE UPDATE (AGENTS.md, release 423dbb5, 2026-11-22)

- Host: the repo runs from /opt/superbot on a VPS; Termux constraints no longer
  apply. Services run as separate identities (ubuntu:service:kalshi_trader,
  ubuntu:service:poly_us_trader; coding agent ubuntu:opencode:kalshi_deepbot).
- Tests: 308 passed at the last checklist (AGENTS.md also says 69 in one table;
  treat the suite output as the truth).
- Storage: local SQLite is the source of truth; Supabase removed 2026-11-22.
- n8n: loopback 127.0.0.1:5678, six webhook workflows (backtest, collect, trade,
  monitor, coordinate, control); Execute Command node must stay disabled (H5); a
  loopback job bridge (scripts/job_bridge.py, 127.0.0.1:18711, fixed allowlisted
  argv) runs scripts; digest and watchdogs run from superbot-jobs.timer
  (scripts/scheduled_jobs.py), not n8n; credentials restored from a Bitwarden
  mirror by scripts/n8n_creds_sync.py with a 15-minute guard timer.
- MemPalace: shared palace directory (MEMPALACE_DIR, e.g.
  /var/lib/superbot/mempalace) for every identity; the fallback is ~/.mempalace
  (a possible second palace); logstream events with task.request, task.reply,
  patch.ready, status; patches via artifact get + sha256 verify.
- Polymarket US: wired with Key ID + Ed25519 secret; orders via the polymarket_us
  SDK; polymarket_us_client.py stays read-only; market slug pattern
  cpc-btc-updown-15m-...; orders.preview takes {"request": {...}}.
- Known-issues table: items 1-62 mostly FIXED; open by design: 22 (stability gate),
  24 (hedges off), 57 (cash floor + exposure ceiling sum to 1.0).
- Kalshi key trap: plain "Kalshi API Key"/"Kalshi RSA Key" vault items are STALE;
  the live pair is the "GPT" items; restore two mirror files after any pull.
- History: main was rewritten on 2026-11-01 to purge a leaked key; SHAs before
  ac0b91b are invalid.
- Still unread: trader.py (about 3,600 lines), signals.py, local_db.py, risk.py,
  fees.py, binance_feed.py, llm_eval.py, mempalace_bridge.py, emit.py, scripts/,
  tests/, ASSESSMENT.md, COMPOUNDING_TODO.md (fetches were rate-limited or failed).

---------------------------------------------------------------------------
## 3. CORRECTIONS TO v14 AND v16 (do not act on the older versions)

C-a The NO-side floor is opt-in and OFF (GATES_MEASURED_FLOOR_ENABLED=false) and
    NO buys are OFF (ALLOW_NO_BUYS=false). The suspected inversion in
    commit 304d8f8 is therefore LATENT, not live (finding F-04).
C-b The ATR-to-sigma overstatement was already identified in-repo (B11) and a
    corrected estimator exists behind GATES_SIGMA_FROM_ATR_V2 (default false).
    The task is to validate and enable it, not to discover it (F-05).
C-c The main loop default is 15 s (min 5 s), not 30 s; CYCLE_TIMEOUT is 30 s. The
    gates cache spot for 30 s and ATR/sigma for 60 s; that is the real latency
    problem (F-08).
C-d Poly daily-loss limit scales with equity (larger of $5 and 5%); v14's "flat $5"
    claim was wrong. The $35 default bankroll and $100 docstring mismatch (F-13)
    still stands.
C-e Coinbase is not absent: a momentum gate uses Coinbase lead/lag and
    COINBASE_ENABLED / BINANCE_ENABLED flags exist (both default false). What is
    missing is confirmed WebSocket spot from Coinbase and a BRTI proxy.
C-f KL divergence is not absent: crossvenue.compute_kl_divergence and
    GATES_KL_* exist, but it imports gates.symmetric_kl_divergence which is not in
    the gates.py snapshot I read (F-03).
C-g Local-first sync, the bus inbox, signed n8n outbox, and MemPalace parity are
    largely built. v14 described them as new builds; v17 treats them as audits
    plus gap-fills.

C-h Maker. v16 and its goal text said to use maker entries where EV after fill
    probability beats taker. Measured evidence says maker at ask-1 loses 4.06c
    with adverse selection. Default is taker plus a calibrated gate; the maker path
    stays disabled until real fills show otherwise (T17-21).
C-i Supabase is already declared removed (2026-11-22); the task is finishing the
    purge, not deciding it (T17-61).
C-j Several v16 "build" items already exist and only need audit/enforcement: the
    logstream protocol, agent identities, loopback job bridge, signed n8n lane,
    credentials guard, source-corruption gates, systemd timers.
C-k LLM layers: README/config say 7 layers, AGENTS.md says 4 layers from 2
    providers. Verify the real layer count before documenting it.
C-l v16 suggested one git worktree per agent. The repo uses one shared tree with
    pull-rebase rules (R10). Worktrees are an optional improvement.
C-m The README's band (82-93c) and AGENTS.md's historical "optimal" 15-55c band are
    both wrong: AGENTS.md itself says 15-55c is EV-negative and the live band is
    95-99c.

---------------------------------------------------------------------------
## 4. FINDINGS REGISTER (verify each on HEAD; severity is my estimate)

F-01 HIGH  Three different settlement references. Kalshi 15M: CF Benchmarks BRTI
  60-second average. Polymarket international (per crossvenue.py): Binance 1m
  candle. Polymarket US: stated differently (reports: possibly Chainlink-based;
  verify from the market rules). A cross-venue pair is a statistical
  relationship with basis risk, not a riskless arbitrage; crossvenue.py itself
  says so. Overlap of underlyings is only BTC, ETH, SOL, XRP (Kalshi also lists
  DOGE, BNB, HYPE, ZEC, NEAR). Tie rules also differ: Polymarket US resolves Up
  when the end price is greater than or equal to the start.

F-02 HIGH  crossvenue.py's Polymarket path is probably dead for the 15-minute
  product. fetch_pm_ladder_us fetches ONE market by slug and returns a ladder
  with ONE rung; Ladder.ok requires at least 3 rungs, and
  _fetch_pm_ladder_with_fallback returns the US ladder whenever it is non-None,
  so the Gamma fallback is never tried. Separately, the Gamma fallback targets the
  INTERNATIONAL Polymarket, not the US venue you trade. And the module compares
  threshold ladders ("above-on" slugs), whereas the 15-minute products are
  start-relative Up/Down markets. A new matcher is needed (T17-31).

F-03 MED   gates.symmetric_kl_divergence: crossvenue imports it; it is absent from
  the gates.py snapshot read. If still absent on HEAD, KL flags silently return
  an empty list (the module logs a warning and returns []).

F-04 MED (latent)  NO-side measured floor. GATES_MEASURED_Q (0.9928) is
  P(YES wins | YES quoted 95-99c). `q` is the probability OUR side wins. Commit
  304d8f8 gates on yes_equiv = 100 - price for NO. A NO favorite at 97c
  (yes_equiv 3c; YES settled only about 1.07%, 6 of 560) wins about 98.9% and now
  gets no floor (conservative, discards a measured edge); a NO LONGSHOT at 3c
  (yes_equiv 97c) would get q floored to 0.9928 (wrong direction; true win rate
  about 0.7%). Latent while the floor and NO buys are off. config.py also argues
  the floor is a "rubber stamp" that replaces rather than checks the model.
  Fix path: write tests for all four (side, price) corners; apply a side-specific
  floor on the own-side price, or delete the floor if the model alone clears
  breakeven.

F-05 MED   Volatility. gates.py computes sigma = ATR * sqrt(pi/2), roughly 2x too
  large because ATR is a mean true range (about 1.6 sigma). GATES_SIGMA_FROM_ATR_V2
  (divide by 1.6) exists, default off. Units: gates.cushion_z wants dollars per
  sqrt-minute; binance_feed.prob_above (used by crossvenue.three_way) wants a
  fractional per-minute sigma. Two GBM implementations exist; unify (T17-20).

F-06 MED   Config drift. MIN_YES_PRICE_CENTS defaults from the env var
  ENTRY_BAND_MIN with fallback 95, while ENTRY_BAND_MIN itself defaults to 92.
  Header comments still describe a 15-55c "backtest best" band (measured
  significantly negative) and the README says 82-93c. ENTRY_WINDOW_START/END
  (13.0-14.5 minutes elapsed, final 1-2 minutes) conflicts with the measured
  evidence cited in config.py (minutes 6-11 elapsed; "entries at 6 or fewer
  minutes left fall below 98%, 7-11 minutes left win about 100%"). Decide which
  window actually has edge from data, then keep exactly one definition.

F-07 HIGH  Edge is thin and unproven (see Reality check). Fees: Kalshi taker about
  0.79c at 87c; Polymarket US taker Theta x p x (1-p) at Tier 1. Paper fills at
  the ask; live orders are post-only maker entries. Fill probability under the
  live model is unmeasured, so paper results may not transfer.

F-08 MED   Stale inputs on the decision path. GATES_SPOT_CACHE_SECONDS = 30,
  ATR/sigma caches 60 s, stale-ok reuse up to 300 s. In the final minutes a
  cushion_z computed from a 30-second-old spot is not a subsecond decision. The
  resting guard already uses a 1.5 s age bound; the entry gates do not.

F-09 MED   Latency structure. Polling loop 15 s; per-decision file I/O in gates
  (_write_log). Latency budgets exist but LATENCY_REGRESSION_GATE is off.

F-10 MED   Public-repo exposure. config.py hardcodes a default VERTEX_PROJECT_ID
  (a GCP project identifier). Not a credential, but it identifies your project;
  restrict API keys and remove the default. Also verify logs/, data/cfb, and
  paper_state_precutover_2026-11-13.json do not expose balances, account IDs, or
  keys, and scan history.

F-11 MED   Size ceilings. MAX_TRADE_DOLLARS 25,000 and MAX_CONTRACTS_CAP 10,000 are
  high; the only depth check is MIN_DEPTH_CONTRACTS = 10. Add a depth-aware
  size cap and a market-impact estimate before any ramp.

F-12 MED   Two sources of truth in poly_us_trader.py (JSON state authoritative for
  cash; DB "source of truth" in docstrings; DB mirror is best-effort). Also a
  3,478-line monolith, and `_mp_resume_hint` opens the MemPalace Logstream
  directly (bypasses the service; plausible cause of the lock in HANDOFF.md).

F-13 MED   Bankroll config drift. Docstring $100, constructor default $35,
  DRY_STARTING_BALANCE 100, and BOT_STATE_HOST notes describe two dry-run traders
  (VPS and local) that wrote the same Supabase keys and a phantom +$4.00. The
  forecast must use LIVE venue balances; dry-run balances are never targets.

F-14 LOW   LLM evaluation is cached, fail-open, and NOT enforced by default. Keep
  logging verdicts against outcomes; enforce only if T17-16 criteria are met.
  The only cited external test found the LLM re-picked the favorite and lost
  after fees.

F-15 LOW   Polymarket US fee tier is assumed Tier 1; run scripts/fee_probe.py and
  record the real tier; fee schedule changes (config cites effective 2026-11-14).

F-16 LOW   Termux leftovers: ml_python proot wrapper, termux-job-scheduler job 7002
  in README, Termux paths in HANDOFF.md.

F-17 HIGH  Dry/live parity is broken. AGENTS.md lists PAPER_PER_TRADE_PCT=0.95
  (dry-run only) against PER_TRADE_PCT 0.08 live, a dry paper bankroll
  (DRY_STARTING_BALANCE), and separate dry/live state files. Under the identical
  config rule these must go. Every DRY_RUN branch in trader.py, poly_us_trader.py,
  risk.py, signals.py, gates.py and config.py must be listed and justified
  (T17-06).

F-18 HIGH  Edge evidence has weak provenance and no NO-side split. The numbers come
  from a third-party HF dataset (14 days, per-minute quotes, no trade prints), not
  split by YES/NO. The ML study showed the GBM gate's positive EV comes from
  overconfidence relative to the venue quote, not extra information. Per-minute
  quotes also cannot support maker fill simulation. NO-only is therefore
  unmeasured; "no NO region clears the lower bound" is a valid outcome (T17-08).

F-19 HIGH  Synthetic data remains. AGENTS.md known issue 19: backtest_engine uses a
  synthetic 45c entry price when no bid/ask exists, and earlier sweeps promoted a
  15-55c band that was later shown EV-negative. Violates R8. Remove every synthetic
  fallback (grep for synthetic, proxy, fallback prices); skip a market with no real
  quote (T17-07).

F-20 MED   Maker is dead on measured data (-4.06c, adverse selection). Polymarket US
  makers pay no fee, which may help, but adverse selection is unmeasured there.

F-21 MED   Tail risk of hold-to-expiry. At 8% sizing one ~97c loss is about 7.8% of
  equity and the measured gate loses about 0.7 trades per day. Risk brakes
  (consecutive-loss cooldown, drawdown halt, per-market loss limit, daily loss) must
  be verified to trigger before an account-threatening drawdown; sizing should be
  derived from the lower edge bound, not the 8% default (T17-40).

F-22 MED   Docs stale: AGENTS.md still lists the Supabase MCP tool, Supabase
  database sections, supabase_client.py, "4 workflows", "69 tests", and the
  v15 file pointer; README is for an older design.

F-23 MED   Remaining Supabase surface: supabase_client.py, supabase/, README,
  AGENTS.md, pnl_reconcile's remote leg (issue 15), inject_n8n_workflows settlement
  workflow needing SUPABASE_*, workflow JSON, tests, env vars, MCP configuration.

F-24 LOW   Possible second MemPalace: MEMPALACE_DIR falls back to ~/.mempalace when
  unset. Confirm exactly one palace directory exists and every unit sets
  MEMPALACE_DIR and MEMPALACE_EXPECTED_DIR.

F-25 LOW   Shared-tree hazards (single working tree, several agents, past history
  rewrite). Covered by R10; verify the pre-commit hooks are installed on the VPS
  checkout.

F-26 LOW   NO_ONLY already exists: config.NO_ONLY is enforced in
  TradeSignal.is_trade (signals.py), the single entry choke point, so every YES
  signal is untradeable. Verify it has tests and defaults on. In the taker path a
  NO entry is a NO FAVORITE (NO ask inside the entry band, 95-99c), so NO longshots
  never enter and the F-04 floor concern stays latent.

F-27 HIGH  The maker branch in signals.py scores edge with an INVENTED win
  probability (0.95 + (entry-94)*0.01 for 94-99c), not a measured or model value;
  violates R8. Its own comment (2026-11-21) says maker 94-99c measured -3.2c per
  contract over 81 days and 0 fills of 7 signals on live depth. Delete or hard
  disable the maker branch until real fills exist (T17-21).

F-28 MED   Kelly sizing uses the model's POINT win probability
  (risk.kelly_size(win_prob=trade_prob)). AGENTS.md says the gate's apparent edge
  comes from overconfidence relative to the venue quote, so Kelly on that number
  oversizes. Size from a lower confidence bound (shrunk toward the quote), not the
  point estimate (T17-40).

F-29 MED   _compute_kl_divergence in signals.py makes REST calls (Kalshi ladder,
  Polymarket per candidate slug) inside signal evaluation, on the decision path.
  It is also probably dead for 15-minute markets (F-02). Move it off the hot path
  or remove it (T17-64, T17-31).

F-30 LOW   Dry fills are simulated from the live book (signals.py B19 comments:
  17 of 17 dry fills; unfilled maker orders are never inserted), so a true maker
  fill rate cannot be computed. Record unfilled orders too (T17-13).

---------------------------------------------------------------------------
## 5. TASKS

Priority tags: MUST (needed for the definition of done), SHOULD, COULD.
Each task: goal, suggested approach, acceptance. Build on existing modules; do
not create parallel versions.

### Phase 0 - Verify and clean (all MUST)

T17-00 MUST  Sync and re-verify on HEAD
  - `git pull`, record HEAD hash, `git ls-files > docs/FILES_<hash>.txt`, run the
    full test suite, then re-check findings F-01 to F-16 and corrections C-a to
    C-g against HEAD. Mark each CONFIRMED / FIXED / WRONG in the status table.
  - Merge any open items from AGENT_TASKS v10-v13 if those files exist anywhere
    (none were visible in the root listing); mark v13 items DONE/PARTIAL/OPEN by
    grepping for their T13-nn tags.
  - Update AGENTS.md: point the active register at AGENT_TASKSv17.md, remove stale
    Supabase sections and the Supabase MCP line, correct the test count and
    workflow count (R12, F-22).
  - Accept: status table filled; no finding left "unchecked"; AGENTS.md pointer fixed.

T17-01 MUST  Polymarket US production status and market rules
  - Using the authenticated client and polymarket_us_client, list production
    markets for BTC/ETH/SOL/XRP up/down (5m, 15m, hourly, 4h). Record for each:
    slug pattern, assetPriceTerms, resolution source, tie rule (>= start?),
    tick size, fee tier, order types (post-only, GTD/IOC), minimum size.
  - Resolve the conflict between the 2026-11-13 PREPROD comment and public
    reports that the markets are live. Update the config.py comment and
    POLYMARKET_ENABLED accordingly, with the date and command evidence.
  - Run scripts/fee_probe.py and record the real fee tier (F-15).
  - Accept: docs/POLY_US_MARKETS.md committed with the table and evidence.

T17-02 MUST  Public-repo hygiene
  - gitleaks/trufflehog on full history; remove the hardcoded VERTEX_PROJECT_ID
    default; confirm logs/, data/, and paper_state_precutover_2026-11-13.json
    contain no balances/IDs/keys you are not willing to publish; rotate anything
    found; decide intentionally whether the repo stays public.
  - Accept: clean scan report under docs/; no secret-looking defaults in config.

T17-03 MUST  Review the two pending gate changes (different agent than author)
  - F-04: tests for four (side, price) corners; apply side-specific floor or
    delete the floor. F-05: validate GATES_SIGMA_FROM_ATR_V2 against realized
    close-to-close sigma on the same candles (report bias and error), then
    enable it only if it improves calibration.
  - Accept: review note filed in MemPalace; tests green; calibration table shows
    Brier before/after.

T17-04 MUST  One definition of bands, windows, and sizing
  - Resolve F-06: choose entry band, entry window, and pin-risk minutes from data
    (re-run scripts/measure_edge.py --cluster market on VPS-collected data), keep
    exactly one config definition, delete stale comments, add a config test that
    fails if band/window definitions disagree.
  - Accept: `pytest tests/test_config_consistency.py` green; README matches.

T17-05 SHOULD  Docs refresh
  - README (rename to DeepBot, remove modules that do not exist, VPS host),
    HANDOFF (regenerate "Verified state" from commands), retire Termux content
    (F-16), keep AGENTS.md aligned with this document.

T17-06 MUST  Dry/live parity audit (F-17)
  - Enumerate every use of DRY_RUN/dry/paper in trader.py, poly_us_trader.py,
    risk.py, signals.py, gates.py, config.py, fees.py. Allowed mode branches live
    only in the execution adapter (how orders are submitted and where fills come
    from), key selection, state namespace, and log paths. Delete everything else,
    including PAPER_PER_TRADE_PCT.
  - Compute a config hash over the resolved config excluding only the allowed
    differing keys; log it at startup; live refuses to start unless it equals the
    hash validated in dry.
  - Accept: a test fails if strategy, gate, sizing, risk, or fee code reads the
    mode flag; docs/PARITY_AUDIT.md lists each remaining branch with a reason.

T17-07 MUST  Real-data audit (F-19, R8)
  - Remove every synthetic fallback price or quote (backtest_engine 45c entry and
    any similar); a market without a real quote is skipped and counted.
  - Record the HF dataset's provenance (source, date range, fields, hash) and
    cross-check a random sample of windows against Kalshi production history.
    State clearly that per-minute quotes cannot decide maker fills and that own
    collection (T17-13) is required for that.
  - Accept: grep shows no synthetic price paths; docs/DATA_PROVENANCE.md committed.

T17-08 MUST  NO-only edge study (F-18) - gate for further strategy work
  - Fix metrics and thresholds first (T17-50), then compute, from real recorded
    data only, net-of-fee EV by NO price band, minutes to expiry, underlying, venue,
    and taker vs maker, using the realized-vol-scaled gate; report YES-only beside
    it for comparison; cluster confidence intervals by market.
  - If no NO-only region has a clustered lower bound above zero after fees, the
    conclusion is "no NO-only strategy clears the bar": stop building strategy
    features and report to the human instead of widening bands or loosening gates.
  - Accept: docs/NO_ONLY_STUDY.md plus a MemPalace entry with the numbers and the
    command that produced them.

### Phase 1 - Data plane

T17-10 MUST  Event-driven spot snapshot (Binance WS, Coinbase WS)
  - A single in-process tick bus (asyncio + uvloop, bounded queues, drop-oldest on
    slow consumers) feeding a lock-free latest-snapshot per symbol. Binance and
    Coinbase WS feeds with sequence/gap detection, jittered reconnect, staleness
    flag. Read current Coinbase and Binance docs for channels and auth and record
    the doc URL and date in each module header.
  - Maintain ATR and sigma incrementally from ticks in memory. Replace the 30 s /
    60 s caches on the ENTRY path with the snapshot; staleness bound for entry
    decisions at most WS_SPOT_STALE_SECONDS (1.0 s); stale means no trade.
  - Reuse existing spot_ticks table (throttled to one persisted tick per
    SPOT_TICK_RECORD_SECONDS) for the record, not for the decision.
  - Accept: 1 h soak, zero unflagged gaps; a test proves a gate evaluation never
    uses a spot older than the bound; bus overhead p99 under 2 ms at 5k msg/s.

T17-11 MUST  Kalshi reference index (BRTI) tracker
  - Step 1: read Kalshi's rules for each series for the exact reference, window,
    and timestamp. Step 2: if real-time BRTI is not available under your license,
    build a proxy from constituent exchanges you can reach (median or
    volume-weighted, Coinbase included) and measure proxy-vs-settlement error by
    hour. Do not redistribute licensed data.
  - Feed the existing locked_share logic with the running 60 s window average so
    the model prices the AVERAGE near expiry, not the last tick.
  - Accept: docs/INDEX_PROXY_ERROR.md (MAE in bps by hour); proxy and BRTI-style
    average exposed as `index_ref` with Binance/Coinbase as fallback.

T17-12 MUST  Venue books and book-age gate
  - Kalshi WS book (mirroring poly_us_stream) and Polymarket US WS BBO with depth
    levels and last-update age; trading refuses on stale or one-sided books.
  - Accept: unit tests for stale/one-sided refusal; book-age metric exported.

T17-13 MUST  Backtest data capture on the VPS
  - Persist, for both venues: top-of-book and depth snapshots at decision times,
    spot snapshots (Binance, Coinbase, proxy), settlements with the venue's
    stated outcome and reference price, fee tier, and our own decision log.
    Retention for this dataset must exceed the current 3-day spot_ticks default;
    archive to compressed Parquet/CSV with a size budget; keep the hot table small.
  - Target at least 60 days before any model is promoted; until then every
    forecast carries a sample-size warning.
  - Accept: collector runs under systemd; daily integrity check (row counts, gaps).

### Phase 2 - Models

T17-20 MUST  One GBM module
  - Extract the Brownian/GBM math from gates.cushion_z and binance_feed.prob_above
    into gbm.py with explicit units (document dollars vs fractional sigma), P =
    Phi(d2) for point-in-time and an average-window (BRTI-style) variant, a
    fat-tail/jump adjustment, and calibration (isotonic or Platt) fitted on
    settled history. Existing RollingCalibrationMonitor scores it.
  - Accept: reliability diagram and Brier vs constant 0.5 and vs market mid on a
    held-out week; the two old call sites import the new function.

T17-21 MUST  Fair-value engine (taker-first; maker disabled until real fills)
  - Evidence (C-h, F-20): maker at ask-1 measured -4.06c. Default entry is taker
    plus a calibrated gate. A maker path may be added only behind a flag that
    stays off until real maker fills (human-approved minimum-size live orders)
    show positive net EV with a clustered lower bound.
  - Per contract: ensemble p_hat with an uncertainty band from model
    disagreement; executable price per fill model; fee via fees.py (Kalshi) or
    the Polymarket US tier model (reuse _taker_fee; takes the tier from T17-01);
    EV for maker entry (with fill probability measured from real fills only) and for
    taker entry; trade only when the LOWER bound of the better EV clears a margin.
  - Accept: golden fee tests; EV never computed from mid alone.

T17-22 MUST  KL divergence module (kl_tools.py)
  - Define symmetric_kl_divergence in one place (fixes F-03), import it from
    gates and crossvenue. Uses: (1) ladder consistency and monotonicity checks;
    (2) model-vs-market KL on multi-strike products; (3) drift gate, KL or PSI of
    live feature distributions vs training, shrinking size or falling back to
    GBM-only above a threshold; (4) log-loss-based ensemble weights. Clip
    probabilities with an epsilon; test 0/1 inputs.
  - Accept: tests including degenerate inputs; KL drift metric on the dashboard.

T17-23 SHOULD  ML model path
  - Pick one stack (torch exists; xgboost/lightgbm install natively on the VPS, so
    the proot wrapper can go). Features: gbm p, index momentum at several
    horizons, Binance-Coinbase skew, book imbalance, spread, time to expiry,
    hour of day. Walk-forward CV with an embargo of one market length. Model card
    (data window, features, metrics, hash) filed in MemPalace and DB.
  - Promote only under T17-16 criteria; otherwise stays paper-only (as README
    already states).
  - Accept: walk-forward log-loss improvement over GBM with a bootstrap lower
    bound above zero, or no promotion.

T17-24 SHOULD  LLM evaluation (keep it off the hot path)
  - Keep the existing cached-verdict design (TTL, quorum, fail-open, not
    enforced). Add outcome-linked calibration reporting (llm_prob vs result) and
    weight the LLM in the ensemble by that evidence only. Do not enable
    LLM_EVAL_ENFORCE without T17-16 evidence.
  - Accept: hot-path p99 unchanged with the LLM stack on vs off.

T17-25 MUST  Decision policy
  - Combine gates, fair-value, KL health, and calibration breaker into one
    documented decision function that defaults to ABSTAIN, logs every input and
    the reason, and is unit-tested on corner cases (stale data, wide spread, halted,
    probe sizing).
  - Accept: decision log schema committed; replay of a day reproduces decisions.

### Phase 3 - Arbitrage

T17-30 SHOULD  Same-venue box scanner (log-only first)
  - On each venue, YES ask + NO ask below $1.00 after fees and rounding locks a
    profit with identical settlement. Expect few or none; record frequency, size,
    and duration for both venues before trading it.
  - Accept: 7-day opportunity report per venue; no orders placed by the scanner.

T17-31 MUST  Cross-venue up/down matcher (fixes F-02)
  - Pair each Kalshi KX*15M window with the Polymarket US 15-minute market for the
    same underlying, start time, and end time (BTC, ETH, SOL, XRP only). Compare
    P(up) directly: Kalshi YES against Polymarket Up, with the start-reference
    rule and tie rule of each venue written into a human-reviewed equivalence
    table (settlement source, averaging window, timestamp, >= vs > at the start).
  - Model basis risk explicitly from measured dispersion between the reference
    sources (spot-source dispersion is already printed by crossvenue.main) and
    from the BRTI-proxy error (T17-11). Size any pair as DIRECTIONAL risk with a
    basis allowance until the measured basis error is small and stable; label
    results "deviation" not "arbitrage" unless settlement sources are identical.
  - Do not use the international Gamma API as a stand-in for the US venue.
  - Accept: 14-day paper study: gross vs net-of-fees deviations, fillable size,
    basis error distribution, and leg-failure rate in docs/CROSSVENUE_STUDY.md.

T17-32 SHOULD  Leg-risk execution
  - Place the less liquid leg first (or use IOC/FOK where supported), timeout and
    unwind partial fills, cap unhedged exposure, include fees, tick rounding and
    capital locked until settlement (cross-venue transfers are slow; never assume
    quick rebalancing).
  - Accept: simulated and paper leg-failure drills with logs.

### Phase 4 - Sizing, risk, execution

T17-40 MUST  Sizing policy across both venues
  - Fractional Kelly (existing 0.25 as ceiling) on the LOWER edge bound; a depth
    cap (fraction of visible depth at the executable price) and a market-impact
    estimate (F-11); per-venue and per-underlying caps expressed as a fraction of
    the live balance; one correlated-exposure budget across venues (BTC on Kalshi
    and BTC on Polymarket is one bet).
  - Read each venue's real balance every cycle (polymarket_portfolio.py and
    fund_tracker.py exist) and write the balance used next to every decision.
  - No martingale, no doubling after loss, no cap overrides from the forecast.
  - Accept: property tests (size monotone in edge, zero when lower bound <= 0,
    never above any cap); a test that a $35/$100 hardcoded bankroll is never used
    in live mode.

T17-41 MUST  Reconciliation on both venues
  - Poly venue reconciliation exists (live mode, every 20 cycles, $1 tolerance).
    Add the Kalshi equivalent and run both in paper against venue state where
    possible. On mismatch halt new entries until a human or the two-person rule
    clears it.
  - Accept: 24 h paper run with empty reconciliation diff.

T17-42 MUST  Kill switches and drills
  - Halt flag in the local DB honored by every order path. Triggers: n8n
    kill-switch webhook, daily loss, drawdown, calibration breaker, stale feeds,
    reconciliation mismatch, venue error spike, clock skew. The existing two-person
    resume (per-identity HMAC, J10) must be exercised in a drill.
  - Accept: each trigger covered by a test or drill with the log line shown.

T17-43 MUST  Staged ramp rules in config
  - Paper -> live-dry (`--live --dry`) -> live at minimum size -> stepwise ramp by
    explicit human approval after N settled markets whose realized edge matches
    the backtest within tolerance. Rules live in config, not in agent discretion.
  - Accept: ramp table committed; a test that live mode refuses to exceed the
    current stage cap.

### Phase 5 - Backtests and 14-day forecasts (centerpiece)

T17-50 MUST  Pre-registered evaluation protocol (docs/EVAL_PROTOCOL.md)
  - Before any forecast run: fix the metric set, walk-forward windows, a held-out
    final period never used for tuning, the fee models, the fill rules (derived
    from recorded books and trade prints per R8), a latency model, and
    the count of strategy variants tried (apply a multiple-testing haircut such as
    deflated Sharpe or Bonferroni). Cluster by market, not by quote.
  - Accept: protocol committed BEFORE the first forecast run; later edits are
    versioned with a reason.

T17-51 MUST  One backtest engine
  - Remove the synthetic entry-price fallback (F-19) before consolidating.
  - Consolidate backtest.py, backtest_engine.py, backtest_real.py and
    parallel_backtest.py behind one engine that reads the T17-13 dataset, applies
    the real fee code and fill models, and emits a trade log used by every
    forecast model. Keep old entry points as thin wrappers until tests pass.
  - Accept: engine reproduces measure_edge.py's headline numbers on the same data
    within tolerance.

T17-52 MUST  forecast14d.py
  - From the SAME trade log, per venue, produce 14-day bankroll forecasts with
    independent model families so disagreement is visible:
    1. Trade-level bootstrap with the live sizing policy, compounding.
    2. Block bootstrap of daily returns (preserves autocorrelation).
    3. GBM/log-normal bankroll model with mu and sigma fitted to daily log
       returns, parameter uncertainty sampled rather than point estimates.
    4. Regime-switching model (2-3 volatility regimes, HMM) driving edge and trade
       frequency.
    5. Fat-tail/jump model (Student-t or compound Poisson shocks) for gap losses.
    6. ML quantile model (lightgbm quantile) of daily P&L from day-start features.
    7. Stress variants of 1-6: fees +25%, fill rate -25%, edge -50%.
  - Output per venue and model: median, p5, p25, p75, p95 of the day-14 balance,
    P(balance >= $3,250,000), P(drawdown > 30%), P(ruin), the daily growth
    required from the CURRENT REAL balance, the sample-size warning, and the
    balances/seed/code hash used.
  - Accept: `python forecast14d.py --venue both --seed 7` is deterministic; results
    archived to DB and MemPalace; the report states plainly when P(target) is
    under 1%.

T17-53 MUST  Forecast honesty tracking
  - Log predicted p5/p50/p95 daily and compare with realized; report coverage. If
    realized values fall outside the band more often than nominal, widen bands and
    flag the model families responsible.

T17-16 MUST  Promotion criteria for any model, band, or gate change
  - Promote only if, on the held-out period, net-of-fee EV is positive with a
    clustered lower bound above zero, drawdown stays within limits, and paper
    results agree within tolerance. Otherwise it stays paper-only.

### Phase 6 - Pillar tasks

T17-60 MUST  n8n (build on emit.py, n8n_client.py, signed outbox, job bridge)
  - Keep H5: no Execute Command node; scripts run through the loopback job bridge
    with allowlisted argv. Digest and watchdogs stay on superbot-jobs.timer; n8n
    owns reports, alerts, approvals, and the kill-switch webhook.
  - Define the event catalog in one doc (daily.summary, settlement, risk.halt,
    sync.lag, feed.stale, forecast.ready, agent.event, latency.breach) and have
    each workflow subscribe by type. Workflows exported as JSON under
    n8n/workflows/ and injected idempotently (upsert by name, dry-run flag).
  - Workflows: settlement-pnl, nightly-backtest-and-forecast, ml-retrain (paper
    only), data-health, kill-switch (authenticated webhook that sets the halt
    flag), daily-report, agent-board, latency-watch.
  - Production hardening: set SUPERBOT_REQUIRE_GATE_KEY=true, keep n8n on
    localhost/behind an authenticated proxy, protect webhook replay, back up the
    n8n DB. n8n failure must never block the trader.
  - Accept: fresh-VPS import works; killing n8n does not stop the trader; the
    kill-switch webhook halts new orders within one cycle.

T17-61 MUST  Local-first DB as the single truth; finish the Supabase purge
  - Purge scope (F-23): supabase_client.py, supabase/, README and AGENTS.md
    sections, the Supabase MCP configuration, SUPABASE_* env vars, pnl_reconcile's
    remote leg (reconcile local state against the venue instead), the settlement
    workflow's Supabase dependency in inject_n8n_workflows.py, workflow JSON, and
    tests. Local SQLite stays the only database.
  - Audit local_db against the config's tables; resolve F-12 by moving cash,
    positions, and ledger authority into the DB behind a flag, JSON files demoted
    to exported snapshots, using the existing ledger_realized_gap check as the
    migration test. Run old and new side by side in paper until the gap is zero for
    several days.
  - Keep the outbox pattern (idempotency keys), sync breaker, dead-row quarantine,
    startup reconciliation (refuse to trade until reconciled), and backups
    (litestream or periodic sqlite3 .backup to OCI Object Storage).
  - Accept: a 30-minute network-down drill in paper (n8n and all internet
    services unreachable) loses no rows and the trader keeps running; a restore
    from backup reproduces the ledger.

T17-62 MUST  MemPalace as a managed service (exactly one palace)
  - F-24: find every palace directory on the host (including ~/.mempalace);
    keep exactly one (the shared MEMPALACE_DIR), merge or retire any other, and
    set MEMPALACE_DIR and MEMPALACE_EXPECTED_DIR in every unit. Follow the
    logstream protocol already in AGENTS.md rather than inventing a new one.
  - EXACTLY ONE palace and one mempalace-mcp under systemd (never create a
    second palace); no direct opens (fix
    `_mp_resume_hint`, F-12); health check and documented safe restart.
  - File: findings (this document's register), model cards, backtests, forecasts
    with their coverage, incidents, post-mortems, decisions, and venue quirks.
    Agents query the palace before starting a task and file results with the
    evidence command afterward. Nightly n8n job summarizes the day. Never store
    secrets or raw balances beyond what a report needs.
  - Read MEMPALACE_GUIDE.md for exact verbs; only `mempalace status` and
    `mempalace event list --to-agent <id>` are confirmed from HANDOFF.md.
  - Accept: a fresh agent session answers "what did we learn about the NO-side
    floor?" and "what was the last forecast and its coverage?" from MemPalace with
    entry IDs.

T17-63 MUST  Inter-agent protocol (enforce what AGENTS.md already defines)
  - Existing: identities from agent_identity.py, logstream events (task.request,
    task.reply, patch.ready, status), artifact get + sha256 verify, an inbox sweep
    at session start, and per-identity HMAC for the two-person resume (J10). Add
    only what is missing: task claim with lease/heartbeat, and enforcement of the
    reviewer rule (R3, R11).
  - Roles: data, model, exec, risk, backtest, reviewer, ops, each with a stable
    identity (agent_identity exists). Fast channel: local queue/bus inbox for
    heartbeats, claims, status. Durable channel: MemPalace events. Human channel:
    n8n notifications.
  - Task claim with lease and heartbeat in the DB; one git worktree per agent;
    announce intent before touching money-path files; reviewer must be a different
    identity (R3); broadcast shared invariants on change (halt flag, caps, model
    version, venue status).
  - Accept: two agents attempt one task and only one holds the lease; a money-path
    PR cannot merge without a reviewer entry from another identity.

T17-64 MUST  Subsecond latency
  - Replace the polling entry path with: WS tick or book update -> snapshot ->
    gates/fair-value -> order. Keep the 15 s loop for housekeeping (settlement,
    reconciliation, reports).
  - Budget (p95, start point): feed receive 30 ms, features and GBM 10 ms,
    ensemble lookup 5 ms, risk and sizing 2 ms, order build and sign 10 ms, network
    submit per measured RTT; total tick-to-submit under 500 ms, p99 under 1 s.
  - Move per-decision file I/O (gates _write_log) to the writer thread; run
    uvloop/orjson, persistent connections, pre-built signed-request templates where
    signing allows, zero synchronous disk/DB/LLM/n8n/MemPalace calls on the hot
    path; avoid import-time subprocess calls near hot code.
  - Turn LATENCY_REGRESSION_GATE on after a baseline run; export p50/p95/p99 per
    stage (existing latency_measurements table and budgets); alert via n8n when
    p95 breaks budget for 5 minutes.
  - Accept: 24 h latency report with percentiles; a test fails if a hot-path
    function performs blocking I/O.

### Phase 7 - Deployment

T17-70 MUST  systemd and reproducible deploy
  - Units: deepbot-trader-kalshi, deepbot-trader-poly, deepbot-feeds,
    deepbot-collector, deepbot-sync, mempalace-mcp, n8n; timers for retrain,
    forecast, backups. Restart on failure, resource limits, journald rotation.
    Replace continuous_loop.sh and termux scheduling.
  - Region choice by measured RTT to both venues (grid_latency.py) from two
    candidate OCI regions; Python 3.12 or 3.13 venv with a lockfile; chrony.
  - Exchange keys trade-only (no withdrawal permission), via EnvironmentFile mode
    600 or OCI Vault; never logged or sent to n8n/MemPalace.
  - Single authoritative writer per bot_state namespace (BOT_STATE_HOST); the
    local/phone instance writes only under its own tag.
  - Accept: reboot test returns all services; traders refuse to trade until
    startup reconciliation passes.

T17-71 SHOULD  Compliance and venue-rules brief (human decision, agents prepare)
  - Account eligibility, position limits, automated-trading terms, self-trade and
    market-maker rules for both venues; file to MemPalace.

---------------------------------------------------------------------------
## 6. SUGGESTED ORDER

1. T17-00, T17-01, T17-02, T17-03, T17-04, then write the T17-50 protocol (metrics
   and thresholds only), then T17-06 (parity), T17-07 (real-data audit), T17-08
   (NO-only study). If T17-08 finds no NO-only edge, STOP and report
   to the human before building further strategy features.
2. T17-62, T17-61, T17-63 (memory, local-first DB, agent protocol) so later work is
   recorded, recoverable, and reviewable.
3. T17-10, T17-11, T17-12, T17-13 (data plane and dataset).
4. T17-50 (protocol) before any modeling result is looked at, then T17-20, T17-22,
   T17-21, T17-25, then T17-23/T17-24 if data supports them.
5. T17-51, T17-52, T17-53, T17-16 (backtest engine, forecasts, honesty tracking).
6. T17-31, T17-30, T17-32 (arbitrage study; paper only).
7. T17-40, T17-41, T17-42, T17-64, T17-60, T17-70, then T17-43 (ramp rules).
The n8n kill-switch workflow and the reconciliation drill must exist before any
live ramp. Daily reporting and retrain scheduling can start earlier.

---------------------------------------------------------------------------
## 7. EXIT CRITERIA FOR THE TASK-LIST PROCESS (when v17 is "done")

- The NO-only study (T17-08) has a written conclusion, positive or negative.
- Dry/live parity audit passed (T17-06) and no synthetic data remains (T17-07).
- Status table below has no OPEN MUST items, and every WRONG finding has been
  corrected in this document's history or in code.
- Paper trading runs on the VPS for both venues with all five pillars operating
  and a 7-day latency report inside budget (or an honest miss with numbers).
- forecast14d.py has produced at least 14 consecutive daily forecasts with coverage
  tracking; the written conclusion states P(target) from live balances.
- No live-size increase without a recorded human approval.
- Afterward: maintenance rounds only (bug fixes, drift, coverage and latency
  breaches). New strategy ideas must pass T17-50 and T17-16 first.

---------------------------------------------------------------------------
## 8. STATUS TABLE (agents fill in: DONE / PARTIAL / OPEN / WRONG / WONTFIX + evidence)

| ID            | Status | Evidence (command + output summary) | Agent | Reviewer |
|---------------|--------|-------------------------------------|-------|----------|
| F-01 .. F-30  | PARTIAL| F-04/F-05 fixed and independently reviewed; F-12 audit FAIL; F-23 purge open; F-24 palace fixes committed; remaining findings need systematic HEAD verification | kalshi_deepbot | docs/T16_GATE_REVIEW.md; docs/T16_LOCAL_DB_AUDIT.md; docs/T16_PALACE_AUDIT.md |
| C-a .. C-m    | PARTIAL| C-a NO floor is removed and C-b sigma V2 default is false; other corrections are carried as unverified until T17-00 completes | kalshi_deepbot | docs/T16_GATE_REVIEW.md |
| T17-00        | PARTIAL| AGENTS.md points to v17; full suite on e692c94 worktree: 1673 passed, 3 skipped in 226.27s; findings F/C verification still open | kalshi_deepbot | |
| T17-01        | PARTIAL| docs/POLY_US_MARKETS.md committed with production evidence; current production/rules revalidation delegated after prior 429 failures | kalshi_deepbot | |
| T17-02        | PARTIAL| docs/SECRET_HYGIENE_2026-11-22.md committed; full-history and VERTEX_PROJECT_ID acceptance checks remain | kalshi_deepbot | |
| T17-03        | PARTIAL| F-04 floor removed and F-05 default false in 52eb7e7; 98 focused tests passed; independent review in docs/T16_GATE_REVIEW.md; HEAD verification pending | kalshi_deepbot | docs/T16_GATE_REVIEW.md |
| T17-04        | PARTIAL| docs/T16_BAND_MEASUREMENT.md: 863338 quotes/60000 markets, no NO band clears clustered bound; one band/window/sizing definition not yet accepted | kalshi_deepbot | |
| T17-05        | PARTIAL| AGENTS.md pointer/current-state edits in progress; README.md and HANDOFF.md contain separate uncommitted work and are not included here | kalshi_deepbot | |
| T17-06        | OPEN   | docs/PARITY_AUDIT.md absent; PAPER_PER_TRADE_PCT and config-hash enforcement not audited | kalshi_deepbot | |
| T17-07        | OPEN   | docs/DATA_PROVENANCE.md absent; synthetic 45c fallback removal and production provenance cross-check not complete | kalshi_deepbot | |
| T17-08        | OPEN   | docs/NO_ONLY_STUDY.md absent; T16 ask-side band study is not the pre-registered T17 NO-only calibrated-gate study | kalshi_deepbot | |
| T17-50        | PARTIAL| docs/EVAL_PROTOCOL.md exists; v17 NO-only thresholds, risk limits and stop-rule alignment review delegated | kalshi_deepbot | |
| T17-51..53    | OPEN   | forecast14d.py absent; forecast14.py is synthetic; no 14-day coverage series | kalshi_deepbot | |
| T17-61        | PARTIAL| docs/T16_LOCAL_DB_AUDIT.md overall FAIL: JSON/DB dual authority, migration and fail-open paths; Supabase purge and drills open | kalshi_deepbot | docs/T16_LOCAL_DB_AUDIT.md |
| T17-62        | PARTIAL| docs/T16_PALACE_AUDIT.md plus host unit MEMPALACE_DIR/EXPECTED_DIR fix and e692c94 spill no-mint fix; managed mempalace-mcp service/direct opens still open | kalshi_deepbot | docs/T16_PALACE_AUDIT.md |
| T17-63        | OPEN   | Existing identities/logstream verified; task leases and enforced different-agent review not complete | kalshi_deepbot | |
| T17-10        | PARTIAL| 42 feed/tick-bus tests passed; real Cryptofeed smoke failed Coinbase 401 and Binance 451; native tick bus remains opt-in | kalshi_deepbot | |
| T17-11        | PARTIAL| brti_tracker.py exists; docs/INDEX_PROXY_ERROR.md absent | kalshi_deepbot | |
| T17-12        | PARTIAL| Book-age gate exists; complete Kalshi/Polymarket US WS depth integration not verified | kalshi_deepbot | |
| T17-13        | PARTIAL| spot/book tables exist; durable archive, collector service and integrity evidence incomplete | kalshi_deepbot | |
| T17-16        | PARTIAL| Promotion criteria exist in docs; merge-enforced held-out lower-bound rule not verified | kalshi_deepbot | |
| T17-20..25    | OPEN   | After-gate model work; no v17 model result or promotion attempted | kalshi_deepbot | |
| T17-30..32    | OPEN   | After-gate deviation study; no v17 cross-venue execution or claim of arbitrage | kalshi_deepbot | |
| T17-40..43    | OPEN   | Lower-bound Kelly, correlated budget and ramp acceptance not implemented | kalshi_deepbot | |
| T17-60        | PARTIAL| Durable n8n outbox exists; complete event catalog, kill-switch drill and fresh-VPS import evidence open | kalshi_deepbot | |
| T17-64        | PARTIAL| tick bus/latency modules exist; event-driven entry and p95/p99 production report not accepted | kalshi_deepbot | |
| T17-70..71    | PARTIAL| systemd units exist; reproducible fresh-VPS, offsite restore and compliance evidence incomplete | kalshi_deepbot | |

---------------------------------------------------------------------------
## 9. APPENDIX - cross-venue equivalence table template (T17-31)

For each pair record, with a source link and date: underlying; window start and
end (ET and UTC); reference price source (Kalshi BRTI 60 s average; Polymarket US
as stated in its market rules; Polymarket international Binance 1m candle);
averaging or point-in-time; start-reference rule; tie rule; tick size; fee model;
order types; settlement latency; reviewer initials. Any unfilled cell means the
pair is NOT eligible for trading.
