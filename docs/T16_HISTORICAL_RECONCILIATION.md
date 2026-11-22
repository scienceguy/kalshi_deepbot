# T16 historical reconciliation

## Scope and evidence rules

This is the historical part of T16-00. It reconciles every task defined in the
supplied v13 register and every still-relevant unchecked or carry-forward item
in the supplied v6-v12 registers. Historical status prose was not accepted as
proof. Source references below are to `git show HEAD:<file>` unless explicitly
marked as a working-tree check.

Final source revision used: `98ac689736a83a5c5169c64d3cf1a8abd6daacaf`.
The investigation started at `8e6ecd90da2f51c1a889f34b97ef99f181c3e26e`;
`git diff --name-status 8e6ecd90..98ac689` shows only `pyproject.toml` and
`requirements.txt` changed (cryptofeed dependency pin), so the audited Python
implementation is unchanged. The full-suite command was run from a clean
`git archive` of 8e6ecd90, avoiding other agents' working-tree edits.

The shared worktree was not clean at the final check. It had modifications to
`binance_feed.py`, `config.py`, `tests/test_n2_outbox.py`, and `trader.py`, plus
untracked Kaggle files, `tests/test_binance_feed_ws.py`, and other files. Those
uncommitted changes are not used to close any HEAD item. No source, config,
service, account, or git state was changed by this reconciliation.

Status meanings:

- DONE: the v13 acceptance substance is implemented and supported by current
  source plus a passing targeted check or a fresh read-only command.
- PARTIAL: useful implementation exists, but a material acceptance condition is
  absent, unsafe, unmeasured, or currently failing.
- OPEN: the central requested behavior is absent or the existing artifact is
  not valid for the requested purpose.
- OBSOLETE/SUPERSEDED: v16 R9 removes the Supabase requirement, or a later T16
  task deliberately replaces the old design. Supabase replica work is not
  silently reopened.

## Verification actually run

- `gitleaks detect --source . --redact --exit-code 1`: 704 commits scanned,
  "no leaks found", exit 0.
- `gh repo view scienceguy/kalshi_deepbot --json visibility`:
  `{"visibility":"PUBLIC"}`.
- Clean HEAD archive, `python -m pytest tests/ -q -p no:cacheprovider`:
  **1650 passed, 16 failed, 4 skipped**. One failure,
  `test_no_stale_identity_anywhere_outside_docs`, was caused by the archive not
  being a git checkout and passed when rerun in `/opt/superbot`. The other 15
  failures were reproduced together in a second run; they are not dismissed as
  full-suite ordering noise.
- Focused models/data run: **179 passed, 1 failed**. The additional failure was
  `tests/test_tick_bus.py::TestTickBusSnapshot::test_snapshot_median_price`,
  ending in recursive asyncio cancellation. The full suite also emitted
  unhandled `CancelledError` warnings from tick-bus and BRTI threads.
- Focused gates/n8n/MemPalace/WS/units run: **264 passed**. This included
  `test_gates.py`, `test_gates_sigma_v2.py`, `test_q_floor_holdout.py`, the N1-
  N3 outbox/event tests, installer, palace, lease, Kalshi/Polymarket stream,
  units, and H15 tests.
- Clean archive `python -c 'import poly_us_trader, trader'`: `imports ok`.
  It also printed the current incomplete-Supabase-credentials warning.

The 15 persistent failures are: one stale entry-window hedge test, one live
exit state test, four dry-run growth replay tests, two L6 order-path tests, and
seven Polymarket execution/setup tests. In particular, the dry-run replay now
returns zero fills in tests that require one, and several Polymarket paper-buy
tests return `False`. This is a current T16-00 blocker, not a historical footnote.

## Highest-priority remaining MUST gaps

| T16 owner | Gap and exact current evidence |
|---|---|
| T16-00 | HEAD test suite is red: 15 reproducible failures after excluding the archive-only git test. The failed replay and paper-execution tests directly undermine later backtest and forecast claims. |
| T16-61 | The original money-record loss remains: `poly_us_trader.py:1011-1017` catches `queue.Full`, decrements pending, increments `_ledger_dropped`, and drops the record. JSON is still authoritative for cash (`poly_us_trader.py:907`, `:995-996`, `:1130`), while SQLite is a best-effort mirror. |
| T16-52, R8 | `forecast14d.py` is absent. `forecast14.py:152-191` invents market conditions, `:250-259` simulates an LLM, `:267-295` invents KL and 5% box opportunities, and `:59-66` defaults to a prose balance and the old $6.25M target. This is not a real-data forecaster. |
| T16-21 | `fair_value.py:222-235` computes EV from point estimate `q`, not `p_hat_lower`; `:47-85` hardcodes fill probabilities. This misses the v13 lower-bound rule and v16 real-fill rule. |
| T16-30/31/32 | `box_arbitrage.py:325-342` declares resolution equivalence merely from BTC/ETH/SOL text and still describes Polymarket US as Binance-candle settled. `:266-320` evaluates same-side leg pairs as boxes, and no executable depth/fee study exists. `docs/CROSSVENUE_STUDY.md` is absent. |
| T16-10/64 | `config.py:379` keeps a 30-second gate spot cache, `:398` defaults `USE_TICK_BUS=False`, and `:790` keeps the 15-second loop. The focused tick-bus test failed; no 24-hour event-driven entry soak exists. |
| T16-51 | `backtest_engine.py` is a shared sweep engine, but has no walk-forward/purge/embargo or clustered-interval implementation. `backtest_real.py` is absent; four replay tests fail. |
| T16-40 | `sizing.py:295-324` trusts the supplied `win_prob` rather than enforcing a lower bound, and `:409-416` truncates to integer contracts. Forecast sizing is separate, not the live sizing function. |
| T16-62/63 | No MemPalace or n8n systemd unit is tracked. `scripts/gen_handoff.py`, protocol documents, and merge-time non-author review enforcement are absent; `agent_identity.py` defines service identities, not the full data/model/risk/reviewer role roster. |
| T16-60/70 | `SUPERBOT_REQUIRE_GATE_KEY` defaults false (`config.py:909`) and is not set by tracked deploy units. Fresh-VPS import, one-hour n8n outage, reboot, and restore drills are not evidenced. |
| T16-05/R9 | README still advertises Supabase, says direct DB workflow upsert although `inject_n8n_workflows.py` now uses REST, and lists root `data_collector.py`/`kalshieye_downloader.py` that are not present. Current R9 cleanup is incomplete; this does not revive replica tasks. |

## v13 task-by-task reconciliation

Only IDs actually defined by v13 are listed; v13 did not define every integer
between 01 and 50.

| ID | Status | Current file/test/command evidence and remaining owner |
|---|---|---|
| T13-01 | DONE | Fresh redacted gitleaks scan: 704 commits, no leaks; fresh visibility check: PUBLIC. `docs/SECURITY_SCAN_2026-11-22.md` records the prior scan and `config.py` has an empty VERTEX default. `mempalace search 'T15-02 gitleaks no leaks found security scan'` returns the T13-01 completion finding and, verbatim, "Operator Q2 (repo visibility) answered: repo is public by design for open-source bot; no action to flip to private." The same finding records no credentials requiring rotation. |
| T13-02 | PARTIAL | `gates.py:628-648` now uses own-side price and NO complement. Four-corner tests are in `tests/test_gates.py:380-505` and `:797-886`; the focused 264-test run passed. `docs/REVIEW_PACKET_304d8f8.md` exists, but its review checklist is unchecked and no current non-author approval for the latest own-side change is in HEAD. T16-03 owns final review. |
| T13-03 | PARTIAL | Equity-scaled Poly halt is implemented at `poly_us_trader.py:665-674`; `test_daily_loss_limit_scales_with_equity` passed. That test covers $35 and $50k, not the stated $1k case, and the fixed $5 floor remains. Full cross-venue lower-bound/depth/correlated sizing remains T16-40. |
| T13-04 | OPEN | MemPalace spill passed, including the 10x-capacity order test (`tests/test_mempalace_bridge.py:269-292`), and n8n has a durable outbox. But the target Poly ledger still drops on `queue.Full` (`poly_us_trader.py:1011-1017`). T16-61 must fix the money path; the Supabase-outbox subpart is OBSOLETE under R9. |
| T13-05 | PARTIAL | Poly has `PolyLocalDB`, legacy import, and transactional tests, but JSON remains cash/position authority and DB writes are explicitly best-effort (`poly_us_trader.py:907`, `:995-996`, `:1051-1055`). No 100-point kill -9 acceptance run exists. T16-61. |
| T13-06 | PARTIAL | README is renamed and has VPS instructions; clean archive imports succeeded. It is nevertheless stale on Supabase, workflow installation, and module names, and no fresh-clone green pytest/`--once --dry` acceptance exists (the suite is red). T16-05. |
| T13-07 | DONE | `docs/AUDIT_REPORT_v13.md` is a non-empty in-session report with 14 evidence-tagged findings. Its acceptance was the report itself. Unresolved findings are carried below, not hidden by this DONE: missing dedicated `tests/test_local_db.py`, monolith split, WS unsubscribe TODO, gates/feed coupling, and audit follow-ups map to T16-61/64/10/05. |
| T13-10 | PARTIAL | `tick_bus.py`, `coinbase_feed.py`, `binance_feed.py`, `cryptofeed_feed.py`, and tests exist. However `USE_TICK_BUS=False`, no 24-hour failover soak is present, and the focused tick-bus test failed. Also `spot_consensus.py:53-65` computes `now - age_seconds` even though the provider stores an already-computed age at `:140-146`, so small ages can be misclassified as stale. T16-10. |
| T13-11 | PARTIAL | `brti_tracker.py`, Kalshi CFB channels, `scripts/measure_index_basis.py`, and `tests/test_brti_tracker.py` exist and ran. `docs/INDEX_PROXY_ERROR.md` and a nightly per-asset/venue basis histogram are absent. T16-11. |
| T13-12 | PARTIAL | `spot_ticks` plus book/BBO/settlement tables exist (`local_db.py:433-489`); `test_spot_ticks.py` passed, and `scripts/live_book_collector.py` can record replayable books. Spot ticks are retention-pruned (`local_db.py:2314-2323`), not a durable full-tick archive, and replay tests currently fail. T16-13/51. |

| T13-20 | PARTIAL | `gbm.py` consolidates point/average-window math and has a fat-tail approximation. `gates.py` and `binance_feed.py` still retain legacy call paths, and no current held-out reliability/Brier result beating market mid with a clustered interval was found. T16-20 owns the remaining migration and validation. |
| T13-21 | PARTIAL | `kl_tools.py` supplies Bernoulli, ladder, PSI, and log-loss helpers; gate/cross-venue tests passed. It does not implement the claimed epsilon behavior at boundaries: `kl_divergence` returns infinity for 0/1 (`kl_tools.py:38-42`). No `ensemble.py` or backtest proving each KL use adds value exists. T16-22. |
| T13-22 | PARTIAL | `box_arbitrage.py` and `tests/test_box_arbitrage.py` exist and passed, but the tests pin unsafe semantics: asset-name resolution equivalence and same-side combinations are accepted. No fee/depth-aware realized box capture study or leg-risk drill exists. T16-30/31/32. |
| T13-23 | PARTIAL | `fair_value.py` and `decision.py` are integrated and their focused tests passed. EV uses `q`, not the lower bound (`fair_value.py:222-235`), and fill probabilities are hardcoded. A uniform order-row schema containing q_lcb/ask/fee/edge/all gate verdicts was not found. T16-21/25. |
| T13-24 | PARTIAL | Native `ml_model_lgb.py`, opt-in torch path, walk-forward training scripts, and `deploy/superbot-retrain.timer` exist. The proot wrapper `ml_python` is still tracked, and no current model card plus out-of-sample promotion win over GBM is evidenced. ML remains paper-only under T16-23/16. |
| T13-25 | PARTIAL | TTL caching is implemented (`llm_eval.py:519-563`) and cache tests passed. Outcome/Brier recording also passed (`tests/test_llm_privacy.py`). A cache miss still calls `_evaluate_trade_uncached` synchronously inside `evaluate_trade`, rather than discovery-time precompute only; no one-week production verdict/outcome table or measured on/off hot-path comparison is present. T16-24/64. |
| T13-30 | PARTIAL | `backtest.py` and `parallel_backtest.py` share `backtest_engine.py`, and `docs/EVAL_PROTOCOL.md` pre-registers metrics and splits. The engine lacks the required walk-forward/purge/embargo and clustered intervals; `backtest_real.py` is absent, no seeded bit-for-bit/`measure_edge` equivalence check was found, and replay tests are red. T16-50/51. |
| T13-31 | OPEN | The only forecaster is the synthetic `forecast14.py` described in the MUST table. It does not consume one real trade log, has no seven independent families or calibration tracking, and violates v16 R8. No `forecast14d.py` or forecast results/coverage store exists. T16-52/53. |
| T13-32 | PARTIAL | First-write-wins `day0_balance` is implemented (`local_db.py:421-432`, `:2040-2061`) and its idempotence test passed; both trader startup paths call it. Live reporting is not fully migrated: `forecast14.py:59` defaults to 12.55 and never calls `get_day0_balance`; dry mode records paper cash. T16-40/52. |
| T13-33 | PARTIAL | `sizing.py` and Kelly integration exist, with depth/impact/correlated-cap code. It does not enforce that its input is q_lcb, combines venue portfolios rather than guaranteeing independent compounding, truncates to int contracts, and is not used by `forecast14.py`. T16-40/52. |
| T13-34 | PARTIAL | Kalshi and Polymarket have independent entry points and service files, shared tick/index infrastructure, and separate balance snapshots. A single cross-venue risk officer/correlated budget is only partial in `sizing.py`; no kill-one-venue drill or combined validated forecast dashboard exists. T16-40/52/70. |
| T13-40 | PARTIAL | Eighteen workflow JSON files are tracked; `inject_n8n_workflows.py:342-351` upserts through REST and never opens SQLite. Signed durable outbox tests passed and no Execute Command node is present. Fresh-n8n import and a one-hour outage/replay soak were not run; nightly forecast content is not valid yet. T16-60/52. |
| T13-41 | PARTIAL | Local WAL, n8n event outbox, idempotency, breaker/quarantine, retention, and backup code exist. The required remote-replica partition equality and Supabase conflict policy are **OBSOLETE/SUPERSEDED by v16 R9**. Local single-truth and backup/network-down drills remain T16-61/70; n8n outbox remains T16-60. |
| T13-42 | PARTIAL | `_mp_resume_hint` uses `mempalace_bridge` (`poly_us_trader.py:138-143`), shared-palace tests passed, and units set one `MEMPALACE_DIR`. The bridge itself is the sanctioned direct-open owner, but no tracked mempalace-mcp systemd unit or safe-restart/recovery drill proves the single-owner service acceptance. T16-62. |
| T13-43 | PARTIAL | Checkpoint, halt/calibration drawer, KG add/supersede/invalidate, heartbeat, and read-before/write-after instructions exist; focused palace tests passed. Model cards, valid nightly forecasts/coverage, latency/basis facts, and a fresh-agent reconstruction check are absent or unverified. T16-62 owns the remaining measured-fact filings. |
| T13-44 | PARTIAL | `agent_bus.py`, durable inbox, signed control, per-identity HMAC, heartbeats, and `leases.py` exist and tests passed. The complete role roster, enforced claim/signoff protocol, sub-2-second ack/15-second death measurement, and binding veto-to-order drill are not evidenced. T16-42/63. |
| T13-45 | PARTIAL | Tick bus, streams, latency tables, persistent clients, and a measured but incomplete `docs/LATENCY_BASELINE.md` exist. The entry path still polls, gate spot cache is 30 seconds, `LATENCY_REGRESSION_GATE=False`, and no 24-hour stage histogram meeting budget exists. T16-64. |
| T13-46 | OPEN | `poly_us_trader.py` has grown to 3822 lines at HEAD and still owns state, ledger, fills, MemPalace, guard, and cycle responsibilities. `poly_state.py` is absent and no recorded-replay equivalence run exists. Fold the split into T16-61/64 rather than creating a parallel engine. |
| T13-50 | PARTIAL | Release-directory trader/auxiliary units, retrain/jobs timers, hardening, live-confirm drop-in, and units tests exist and passed. There are no mempalace-mcp, n8n, feeds, collector, sync, or forecast units as required by T16-70, and no reboot/restore evidence. Termux scheduler instructions are gone, but `ml_python` remains. |

## Older registers: still-relevant open or partial items

The tables below carry every unresolved older item into a T16 owner or mark it
obsolete. The closing coverage ledger then accounts for IDs considered landed
or deliberately superseded, so no unchecked item is silently dropped.

### v6

| IDs | Disposition and current evidence | T16 mapping |
|---|---|---|
| D1 | PARTIAL: Poly DB exists, but JSON remains authoritative and settlement/mirror writes are best-effort. | T16-61 |
| L1, L6 | PARTIAL: latency recorder/baseline exist, regression gate is off, and the money ledger still drops under pressure. | T16-61/64 |
| L3, L5 | PARTIAL: Poly streams and Kalshi WS exist, but event-driven entry and the Kalshi hot-path conversion are incomplete. | T16-12/64 |
| N2 | PARTIAL: REST workflow upsert landed; `scripts/n8n_drift_check.py` is absent and fresh-import drift evidence is missing. | T16-60 |
| M2 | PARTIAL: operational MemPalace integration exists; valid model/forecast facts and read-side reconstruction are incomplete. | T16-62 |
| I2 | PARTIAL: bus/logstream primitives exist, but the requested `scripts/agent_task.py` apply-and-ack workflow was not found. | T16-63 |
| V2 | PARTIAL: core units exist; full service/timer coverage, offsite restore, and reboot proof do not. | T16-70 |
| Q3 | PARTIAL: VPS identity/path documentation improved, but README still has stale Supabase/workflow/module claims. | T16-05 |
| D2 | OBSOLETE for remote sync columns, Supabase idempotency, remote audit, and pull lane under R9. Local n8n outbox and SQLite migrations are covered separately. | T16-60/61 only for local remnants |

### v7

| IDs | Disposition and current evidence | T16 mapping |
|---|---|---|
| D2 | PARTIAL: local Poly tables/importer landed, but the central SQLite-authority requirement did not. Any Supabase table migration is obsolete. | T16-61 |
| L4, L6 | PARTIAL: guard/fast components and order efficiency landed, but no event-driven entry loop and no completely nonblocking money-record path. | T16-61/64 |
| V2 | PARTIAL: trader units exist; n8n/MemPalace and the full hardened service set are not tracked. | T16-62/70 |
| V3 | OPEN AGAIN: hermetic socket blocking exists, but the clean HEAD suite has 15 persistent failures. | T16-00 |
| D3 | OBSOLETE: Supabase outbox sync/quarantine/remote lag acceptance is removed by R9. Local quarantine and n8n lag remain implemented. | No remote follow-up; local T16-60/61 |

### v8

| IDs | Disposition and current evidence | T16 mapping |
|---|---|---|
| H8 | PARTIAL: the measured baseline covers market discovery/local order build, not the complete tick-to-ack path; event-driven entries remain unbuilt. | T16-64 |
| H16, V3-related environment work | OPEN AGAIN: systemd/environment-specific tests were hardened, but the current clean suite is red for the 15 reasons listed above. | T16-00 |
| X1 | PARTIAL: `scripts/e2e_smoke.py` and stub tests exist. No current real-VPS pass/fail table covering all pillars was produced in this read-only task. | T16-60/62/70 |

### v9

| IDs | Disposition and current evidence | T16 mapping |
|---|---|---|
| J6 | PARTIAL: baseline exists, but complete entry/guard stage measurements and a proven event-driven wake-up path are incomplete. | T16-64 |
| J11 | OPEN: `SUPERBOT_REQUIRE_GATE_KEY` defaults false and no production unit sets it true. | T16-60 |
| J13 | OPEN: `.github/workflows/guards.yml` still uses mutable action tags and downloads gitleaks without a checksum; `supply-chain.yml` is absent. | T16-70 |
| J14 | OPEN AGAIN: current suite has persistent failures, including stale entry-window and Polymarket execution tests. | T16-00 |
| J15 | PARTIAL: `docs/OPERATOR_CHECKLIST.md` exists, but `scripts/vps_verify.py` and current all-pass VPS output are absent. | T16-70 |
| K1 | PARTIAL: `kalshi_ws.py` supports books/fills/CFB and its tests passed; REST polling still drives entries and the stale-stream guard integration needs the T16 hot-path audit. | T16-12/64 |

### v10

| IDs | Disposition and current evidence | T16 mapping |
|---|---|---|
| P0-3 | PARTIAL: review packet and four-corner tests exist, but current non-author approval is not evidenced in HEAD. | T16-03 |
| A1 | PARTIAL: stage instrumentation exists; regression enforcement and complete paper-preview/cancel percentiles are incomplete. | T16-64 |
| A5 | OPEN: persistent/cached clients exist, but no measured low-rate keepalive/connection-reuse report was found. | T16-64 |
| A6 | PARTIAL: cache-only guard and wake infrastructure exist, but the complete event-driven guard/entry acceptance is not demonstrated. | T16-64 |
| A7 | PARTIAL: import side effects are tested and lazy, but JSON state remains authoritative and the ledger drops on full queue. | T16-61/64 |
| A8 | SUPERSEDED as a standalone task: uvloop is used by tick bus; all further runtime tuning must be justified by T16 latency measurements, not guessed. | T16-64 |
| B1 | OPEN: SQLite WAL is present, but cash/positions/ledger are not transactionally authoritative and the kill -9 acceptance is absent. | T16-61 |
| B2 | OBSOLETE: durable Supabase replication and remote conflict/checksum acceptance are removed by R9. | Local n8n outbox T16-60; backups T16-61/70 |
| B3 | SUPERSEDED as a same-physical-store requirement: both venues have mode-isolated local stores, but v16 requires authority, not one shared file. Poly authority remains open under B1. | T16-61 |
| D1 | PARTIAL: shared palace configuration/bridge landed; one managed mempalace-mcp service and concurrency drill remain unproven. | T16-62 |
| D3, D4 | PARTIAL: calibration/incident drawers exist; risk-decision preload of prior KG/postmortems and a structured drawer for every losing settlement were not found. | T16-62 |
| D5 | OPEN: `scripts/gen_handoff.py` is absent; HANDOFF is not generated from DB/git/palace. | T16-05/62 |
| E1, E2, E5 | PARTIAL: envelopes, claims/leases, and handoff-capable events exist, but the requested docs, pre-commit foreign-claim enforcement, and acked mid-task handoff tooling are incomplete. | T16-63 |
| E3 | OPEN: R3 requires a different reviewer, but merge/deploy does not verify a non-author verdict from MemPalace. | T16-63 |
| F5 | OPEN: the 3822-line monolith remains. | T16-61/64 |
| F6 | OPEN: maker realism is not established; `fair_value.py` hardcodes fill probabilities and v16 forbids invented fill odds. | T16-21/50 |
| G1 | PARTIAL: `scripts/measure_edge.py` and scheduled backtest plumbing exist, but no proven automatic paper-only retraction on a stale/below-breakeven clustered bound was found. | T16-04/16 |

The v10 A2/A3/A4 client work is now visibly landed: separate read/order/bulk
buckets (`polymarket_us_client.py:33-58`), single-attempt settlement result and
background-poller contract (`:185`), cached authenticated/unauthenticated
clients and `reload_credentials` (`:108-143`), and shared `_build_order_params`
(`:349`). C1/C2/C3/C4 are covered by the durable signed outbox, 18 workflows,
signed control lane, and `scripts/e2e_smoke.py`; a real fresh-n8n canary remains
under T16-60 rather than a duplicate old task. F1's suggested SidePrice class is
superseded by the tested own-side price/probability fixes; F2/F3/F4 are covered
by pending-send reconciliation, shared order construction, and the production
fee-term evidence in `docs/POLY_US_MARKETS.md`.

### v11

Every bare ID in this v11 subsection is shorthand for the exact register ID
with the `V11-` prefix. For example, S3 means V11-S3 and D1 means V11-D1.

| IDs | Disposition and current evidence | T16 mapping |
|---|---|---|
| S3 | OPEN: `_enqueue_ledger` still drops ledger records on queue full. | T16-61 |
| S5 | PARTIAL: VPS README refresh landed but stale Supabase, workflow-install, and module claims remain. | T16-05 |
| L1, L4 | PARTIAL: baseline/latency budgets exist, but complete stage enforcement, venue RTT comparison, clock-offset evidence, and 24-hour report are missing. | T16-64/70 |
| L2 | PARTIAL: guard cache-only behavior landed; the ordinary entry path still polls/falls back rather than satisfying the full subsecond design. | T16-64 |
| L3, D3 | OPEN: JSON remains authoritative; DB-vs-JSON-vs-ledger cutover and kill -9 proof are absent. | T16-61 |
| L5 | PARTIAL: n8n/MemPalace are asynchronous, but Poly money records can still be dropped; Supabase async-sync subpart is obsolete. | T16-60/61 |
| D4 | PARTIAL: online backup/restore tests exist, but scheduled offsite retention and a clean-machine restore drill are unverified. | T16-70 |
| N3, N4 | PARTIAL: workflows and agent dispatch JSON exist; a test fixture for every workflow and current live import/escalation proof are not established. | T16-60 |
| M1 | PARTIAL: bridge access and shared path landed; managed single-owner service and concurrent production check remain. | T16-62 |
| M2, M3 | PARTIAL: instructions and many drawers exist, but the required historical finding IDs and per-task closure coverage were not established from HEAD alone. | T16-62 |
| M4 | PARTIAL: calibration/day0 KG writes exist; current latency, sync, forecast, and model-weight facts are incomplete. | T16-62 |
| M5 | OPEN: no scheduled palace export/backup plus restore drill was found. | T16-62/70 |
| A1 | PARTIAL: five canonical identities exist, not the full host/role scheme for data/model/exec/risk/reviewer/ops. | T16-63 |
| A2, A3 | PARTIAL: bus, acks, replay defense, and leases exist; cross-agent file-claim enforcement and measured p95 delivery under 250 ms are not evidenced. | T16-63 |
| A4 | OPEN: non-author review is documented but not enforced by merge/deploy tooling. | T16-63 |
| P1 | PARTIAL: own-side maker-price tests exist; a fresh venue `orders.preview` validation was intentionally not performed in this read-only/no-account task. | T16-01/25 |
| P3 | OPEN: no real-fill adverse-selection/maker-fill study; the current generic fair-value model uses hardcoded probabilities. | T16-21/50 |
| P4 | OPEN: no `scan_truncated_total` or markets-seen coverage metric was found in `poly_us_trader.py`/`trader.py`. | T16-13/60 |
| P5 | PARTIAL: venue reconciliation code/tests exist, but current Polymarket execution setup failures prevent a clean parity claim and no 24-hour diff is present. | T16-41 |
| K1 | PARTIAL: floor fix/tests landed; non-author review remains. | T16-03 |
| K2 | PARTIAL: band evidence is recorded and config consistency tests passed, but automatic stale-evidence event/retraction is unproven. | T16-04/16 |
| K3 | PARTIAL: Kalshi WS and shared latency components exist; polling entry remains. | T16-12/64 |
| O1 | PARTIAL: Termux paths/scheduler are removed from operational scripts/docs, but tracked `ml_python` proot wrapper remains. | T16-05/70 |
| O2 | PARTIAL: several hardened units exist; n8n/MemPalace and the full timer/service set are missing. | T16-70 |
| D1, D2 | OBSOLETE: Supabase transactional outbox, remote conflict policy, and remote lag SLO are removed by R9. Local n8n outbox is not obsolete. | T16-60/61 local only |

V11 S1/S2/S4 are supported by the fresh secret scan/PUBLIC check, successful
clean imports, and import-side-effect tests. N1/N2 are covered by REST upsert,
signed idempotent outbox, and their passing tests. P2 uses the production fee
terms recorded in `docs/POLY_US_MARKETS.md`; P6 has cross-venue collectors;
P7's old bare daily-rollover swallows were not found by the current grep.
K4/K5 and O3 are covered by the corrected band guidance, native retrain timer,
and installed source-corruption gates.

The older named carry items are also explicit: CF-1 maps to P0-3/T16-03;
CF-2 maps to D1/D3/M2 under T16-62; CF-3 is closed by the real v13 audit; CF-4
is closed by tracked planning docs (v12 A2). The v6/v7 A11 streaming deviation
is closed by the current stream clients. The B11 ATR-sigma deviation is closed
by `GATES_SIGMA_FROM_ATR_V2=True` (`config.py:390`) and passing
`tests/test_gates_sigma_v2.py`; this does not substitute for T16-20 model
validation.

### v12

| IDs | Disposition and current evidence | T16 mapping |
|---|---|---|
| A1 | PARTIAL: review packet and tests exist; final non-author review is open. | T16-03 |
| A3 | PARTIAL: README is still stale on Supabase, workflow installation, and modules. | T16-05 |
| A5, X1 | PARTIAL: `scripts/measure_edge.py` and `measure_edge_poly.py` exist, but the requested current clustered result persisted with n/clusters/losses/lo95/breakeven in an `edge_measurements` table was not found. No band was changed by this reconciliation. | T16-04/13/16 |
| D1, D2 | PARTIAL: both feeds exist, but tick-bus production enablement, connectivity/soak, and endpoint regression closure are incomplete. | T16-10 |
| D3 | PARTIAL: `spot_consensus.py` has the age-unit defect described under T13-10 and no complete degraded-consensus production gate proof. | T16-10/25 |
| D4 | PARTIAL: instrumentation exists without complete metrics endpoint, enforced budget, and current venue RTT selection. | T16-64/70 |
| D5 | PARTIAL: Kalshi/Polymarket WS caches exist; complete depth, REST drift reconciliation, and event-driven use are unfinished. | T16-12/64 |
| M1 | PARTIAL: GBM module exists; held-out calibrated comparison and final call-site consolidation remain. | T16-20 |
| M2 | PARTIAL: native ML path/timer exists; model card and held-out promotion win are absent. | T16-23/16 |
| M3 | PARTIAL: cache and Brier outcome ledger exist; full context-hash caching, structured veto model, and production down-weight evidence are incomplete. | T16-24 |
| M4 | PARTIAL: `kl_tools.py` exists; `ensemble.py`, boundary clipping, and demonstrated online KL weighting are absent. | T16-22 |
| M5, M5b | PARTIAL: scanner/matcher code exists, but unsafe equivalence semantics and absent fee/depth/leg study block any arbitrage claim. | T16-30/31/32 |
| M6 | PARTIAL: `decision.py` exists, but stale-input, lower-bound EV, and full production decision-log/replay requirements remain. | T16-25 |
| B1 | PARTIAL: shared engine exists without the complete recorded-tick/book, walk-forward, latency, queue-position, and clustered validation path. | T16-51 |
| B2 | OPEN: `forecast14.py` is synthetic and violates R8; no calibrated per-venue real-log suite exists. | T16-52/53 |
| B3 | PARTIAL: sizing policy exists but does not enforce lower-bound probability, independent venue compounding, fractional venue lots, or forecast reuse. | T16-40/52 |
| C1, C2 | PARTIAL: REST installer and many workflows exist; drift CI, valid nightly forecast, and fresh-VPS import proof are missing. | T16-60/52 |
| C3 | OPEN: no enforced approvals/model-registry workflow with recorded approver and startup approved-config check was found. | T16-16/63 |
| C4 | PARTIAL: n8n-down durable delivery tests passed; reduced-new-risk staleness policy and required production gate key are incomplete. | T16-42/60 |
| L1 | PARTIAL: core local tables exist, but Poly authority is unresolved and several proposed decision/model/approval/pair-map records are absent. | T16-61 |
| L3 | PARTIAL: Poly reconciliation exists; complete Kalshi equivalent and 24-hour empty-diff run are not evidenced. | T16-41 |
| L4 | PARTIAL: online backup tests exist; offsite restore drill is unverified. | T16-70 |
| P1, P2, P3 | PARTIAL: bridge/shared palace and operating instructions exist; managed service, complete required findings, and model/forecast registry are not closed. | T16-62 |
| P4 | PARTIAL: report/dispatch workflows exist, but verified palace-driven daily model/promotion reporting is absent. | T16-60/62 |
| G1 | PARTIAL: canonical service identities exist, not the full specialist role roster. | T16-63 |
| G3 | PARTIAL: leases exist; mandatory money-path signoff is not merge-enforced. | T16-63 |
| G4 | PARTIAL: risk halt/control path exists; measured sub-100 ms cross-agent veto delivery is not proven. | T16-42/63 |
| X2, X3 | PARTIAL: client hardening is substantial, but fresh market-rule preview, full sizing/latency/reconciliation parity, and clean execution tests remain. | T16-01/40/41/64 |
| L2 | OBSOLETE: Supabase outbox replication, remote inbox/conflict policy, and lag acceptance are removed by R9. | Local n8n outbox T16-60; backups T16-61/70 |

V12 A0 is completed by this document and the coverage ledger below. A2 is
satisfied by tracked planning documents; A4's audit artifact exists and its
findings are explicitly carried. G2's bus is implemented and tested.

## Coverage ledger for landed or deliberately superseded older IDs

These IDs are not open carry-forward work. The evidence is the named current
module/test family; where an old design was intentionally narrowed, the T16
replacement is named instead of preserving a duplicate task.

- **v6 landed/superseded:** V1 (data-dir isolation plus release deploy), N1
  (signed durable n8n outbox), M1 (background palace bridge), L2 (Polymarket
  streams), L4 (Poly spot streams), I1 (signed typed control), N3 (webhook
  auth), D3 (online backup/integrity/migrations), Q1/Q2 (test isolation/import
  handling), Q4 (old `local.py` scaffolding superseded by the bridge), and Q5
  (fee probe plus smaller client/inbox fixes). Tests include V1, N1-N4, H6/H7,
  D4, Q/isolation, and fee/stream suites.
- **v7 landed/superseded:** N1-N4; D1 (inline remote writes removed; current
  R9 cleanup is separate); D4; M1-M4; C1-C3; L1-L3; L5; and V1. Current files
  and their same-named test modules are present. D3 is obsolete only for
  Supabase replication, as noted above.
- **v8 landed:** H1-H7 and H9-H15, except the separately listed H8/H16/X1.
  The focused 264-test run included palace, control, replay, release, guard,
  risk, units, and ops coverage. H1's per-identity-key remainder is covered by
  v9 J10, which is landed.
- **v9 landed:** J1, J2, J3, J5, J7, J8, J9, J10, and J12. Current evidence
  includes GTD/pending-send/stream reconciliation, cached guard pause, job
  bridge hardening, durable scheduled alerts, release paths, per-identity HMAC,
  and large-file/log guards.
- **v10 landed/superseded:** P0-1, P0-2, P0-4, P0-5 (residual proot cleanup is
  v11 O1); A2/A3/A4; C1-C4 subject to the fresh-n8n operational check under
  T16-60; D2; E4; F1-F4 with the narrowing described above; and G2-G4. G4 is
  supported by `verify_source`, `verify_compiles`, `verify_undefined`,
  `verify_operators`, and `verify_review_marked`, not by a claim that these
  gates catch every semantic splice.
- **v11 landed/superseded:** S1/S2/S4, N1/N2, P2/P6/P7, K4/K5, and O3, with
  evidence listed immediately after the v11 table. Aliases: C0 is this
  reconciliation; C1 maps to K1; C2 to M1; C3 to M2; C4 is the completed v13
  audit; C5 maps to K2 and v12 A5.
- **v12 landed/superseded:** A0 (this report), A2, A4, and G2. Every other
  non-Supabase ID is explicitly listed as open/partial above.

## v12 acceptance checklist: no unchecked box hidden

| Original unchecked acceptance | Current disposition |
|---|---|
| pytest/source/ASCII gates green | OPEN: 15 persistent test failures. Final report-only ASCII/source-gate results are recorded below. |
| Binance + Coinbase WS, staleness and p99 tick-to-order | PARTIAL: feeds/tick bus exist, production path is not enabled/proven and tick-bus focused test failed; T16-10/64. |
| n8n JSON, idempotent injection, approval flow | PARTIAL: JSON/upsert/outbox landed; approval enforcement and fresh import remain; T16-60/16/63. |
| Local DB only synchronous path, Supabase lag, restore | PARTIAL locally: Poly JSON authority remains and restore drill is unproven; T16-61/70. The Supabase-lag checkbox is OBSOLETE under R9. |
| MemPalace unlocked, single-owner topology, findings filed | PARTIAL: bridge/topology instructions exist, managed-service and complete-filing proof remains; T16-62. |
| Bus plus claim/signoff, no unreviewed money-path merge | PARTIAL: bus/leases exist, non-author signoff is not merge-enforced; T16-63. |
| forecast14 five families, target CI, back-scoring | OPEN: existing artifact is synthetic and not R8-compliant; T16-52/53. |
| Per-venue API balances with source/timestamp in daily report | PARTIAL: day0 rows have source/timestamp, but the forecast bypasses them and no current valid daily fan report exists; T16-40/52/60. |
| Moonshot never loosens risk; changes approved | PARTIAL: no risk change was made here, but staged-ramp config and enforced approval evidence are incomplete; T16-43/16. |

## Supplied v15 attachment comparison

The two distinct supplied v15 variants differ materially in three places: the
newer variant adds R8 real-data-only rules, requires real-fill probabilities,
and says exactly one palace/mempalace-mcp. The root v15 status table contains
later historical claims, while v16 retains R8 and the one-palace rule and adds
R9 no-Supabase. This reconciliation uses current code plus v16 R8/R9, not the
older v15 allowances for synthetic/conservative fill assumptions and not any
v15 DONE claim without fresh evidence.

## Reconciliation result

- v13: **2 DONE, 24 PARTIAL, 3 OPEN** across the 29 defined task IDs. Mixed
  Supabase portions are explicitly obsolete rather than counted as new work.
- The two DONE items are the public-history security evidence (T13-01) and the
  non-empty in-session audit artifact (T13-07). DONE does not erase the
  separately tracked audit follow-ups or public-repo governance choice.
- The three OPEN v13 items are the no-drop Poly ledger (T13-04), a valid
  real-data forecaster (T13-31), and the monolith split (T13-46). The existing
  synthetic forecast file is not credited as partial fulfillment.
- No older Supabase-only replica item remains open. Current R9 removal of stale
  imports/docs/dependencies is a separate cleanup requirement, not a revival
  of remote replication.

## Final verification of this report

- `LC_ALL=C grep -n '[^ -~]' docs/T16_HISTORICAL_RECONCILIATION.md`: no output,
  grep exit 1 (clean; no non-ASCII bytes).
- `python scripts/verify_source.py --all`: `all clean`, exit 0.
- Report table count check: 29 v13 task rows, matching all 29 task IDs defined
  in the supplied v13 register.
- No other repository file was edited by this reconciliation. The report is
  uncommitted by instruction; no git mutation or service action was performed.

