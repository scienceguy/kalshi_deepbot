# T16-62 Evidence Audit -- MemPalace as a Managed Service

Date: 2026-11-22. Auditor: subagent session (read-only audit).
Task: AGENT_TASKSv16.md lines 600-613 (T16-62 MUST).

## Scope and method

Read-only audit of: T16-62 text, config/env usage, mempalace_bridge.py,
agent_identity.py, deploy units, installed systemd units, tests, and the live
host state. No palace was created, moved, or repaired; no source, config, or
test was edited; no git mutation; no orders/live/credential/process changes.
The only write is this file. The audit server restarted mid-session; live-state
facts (running units, lsof, palace mtimes) were re-verified after the restart
and are quoted post-restart. No secrets or balances are printed; one
`systemctl show` output surfaced a webhook secret during evidence gathering and
is redacted here (referenced only as `SUPERBOT_WEBHOOK_SECRET=<redacted>`).

## Verdict per acceptance point

| # | T16-62 requirement | Verdict |
|---|--------------------|---------|
| 1 | EXACTLY ONE palace | FAIL -- two live palaces in concurrent use |
| 2 | One mempalace-mcp under systemd | FAIL -- no such unit exists anywhere |
| 3 | Never create a second palace (code paths) | PARTIAL -- no direct creator in repo, but the bridge can mkdir a new palace dir and fail soft |
| 4 | No direct opens (fix `_mp_resume_hint`, F-12) | PARTIAL -- `_mp_resume_hint` fixed via bridge; 2 direct Logstream opens remain outside the bridge; F-12 itself is T16-61 scope, still open |
| 5 | Health check | PARTIAL -- bridge degraded check exists and is surfaced, but is inert because MEMPALACE_EXPECTED_DIR is set nowhere; no MCP-server health check (no service) |
| 6 | Documented safe restart | PARTIAL -- trader shutdown ordering documented; no mempalace-mcp restart/health drill; MEMPALACE_GUIDE.md is stale for this host |
| 7 | File findings/model cards/backtests/forecasts/incidents/decisions/quirks | PASS (coding-agent palace) -- wings kalshi_deepbot (71 decisions), kalshi_superbot, kalshi_superbot_v15 verified via `mempalace status` |
| 8 | Agents query before tasks / file after (process) | PARTIAL -- mandated in AGENTS.md and evidenced by filed drawers; not technically enforced |
| 9 | Nightly n8n job summarizes the day | PARTIAL -- daily-digest exists in scripts/scheduled_jobs.py (86400 s cadence, event type `daily.summary` at line 161); no evidence it summarizes the palace; scripts/periodic_insights.py exists but has no scheduler entry found |
| 10 | Never store secrets / raw balances beyond need | PASS on spot-check -- bridge stores caller-provided content only; no balance logging in bridge/trader palace paths reviewed |
| 11 | MEMPALACE_GUIDE.md verbs | FAIL -- guide exists (docs/MEMPALACE_GUIDE.md) but is written for the old termux host; and the HANDOFF-confirmed verb `mempalace event list` is invalid on the installed CLI |
| 12 | Accept: fresh session answers "NO-side floor?" from MemPalace with entry IDs | PARTIAL -- answer retrievable; entry IDs only via `mempalace logstream list`, not search output |
| 13 | Accept: fresh session answers "last forecast and its coverage?" with entry IDs | PARTIAL -- same as 12; the palace itself answers "no forecast filed yet" with a measured-verdict drawer |
| 14 | Single palace means one knowledge scope across agents | FAIL -- the two palaces hold disjoint knowledge (see A1) |

## A1. Palace inventory on this host (evidence)

Commands run (read-only):

    find / -maxdepth 5 -name "logstream.sqlite3" -not -path "*/.venv/*"
    ls -la /var/lib/superbot/mempalace ~/.mempalace ~/.mempalace/palace /tmp/opencode/new_palace
    cat /var/lib/superbot/mempalace/replica.json ~/.mempalace/replica.json
    systemctl list-unit-files | grep -i mempalace
    systemctl --user list-unit-files | grep -i -E "mempalace|palace"
    ls /etc/systemd/system/ | grep -i -E "mempalace|palace"
    for u in superbot-dry poly-us-dry poly15m-paper poly5m-collector; do systemctl is-active $u; done
    lsof | grep -E "\.mempalace/logstream\.sqlite3$|superbot/mempalace/logstream\.sqlite3$"

Results:

- `/var/lib/superbot/mempalace/logstream.sqlite3` -- 203 MB, WAL mtimes
  2026-11-22 21:50-21:51 (live). Contains service events, e.g.
  `evt_20261120T125726_a8c989356af5` (status heartbeat,
  from ubuntu:service:kalshi_trader). replica_id rep_71da42b51dd2bb16cb31fe4bec31d30f.
- `/home/ubuntu/.mempalace/logstream.sqlite3` -- 41 MB, live; held open by the
  running dry trader (PID 506931, `trader.py --dry --interval 15`, started
  2026-11-22 15:04:51 UTC). Contains legacy coding-agent checkpoints, e.g.
  `evt_20261114T031838_d937b06b803b` (from termux:opencode:kalshi_deepbot).
  replica_id rep_ed8925a68653aada12eebb1e852159d1 (DIFFERENT from the other
  palace -- two independent palaces, not replicas of one hub).
- `/home/ubuntu/.mempalace/palace/logstream.sqlite3` -- stale (last write
  2026-11-15); stray nested palace dir.
- `/tmp/opencode/new_palace/` -- created 2026-11-22 08:30; contains
  logstream.sqlite3 (18 MB), chroma.sqlite3 (50 MB), mempalace_embedder.json,
  migration markers (`.blob_seq_ids_migrated`, `.collection_type_fixed`).
  No repo code references this path (grep "new_palace" over repo and
  ~/.bash_history: no hits). Origin unexplained -- stray third palace dir.
- `/tmp/opencode/palace_backup_20261122T0812*` -- backups (benign).
- `/tmp/pytest-of-ubuntu/...` -- pytest temp DBs (benign, test-owned).
- `~/.mempalace` also contains `logstream.db` (61 KB, mtime 2026-11-20) -- an
  additional stray DB with a different filename inside the main palace dir.

Tool-confirmed split-brain (verbatim from the CLI):

    $ MEMPALACE_PALACE_PATH=/var/lib/superbot/mempalace mempalace status
      Palace dir at /var/lib/superbot/mempalace exists but has no chroma.sqlite3 yet.
      ...
      Note: The knowledge graph at /home/ubuntu/.mempalace/knowledge_graph.sqlite3
      holds facts this palace does not read; it reads
      /var/lib/superbot/mempalace/knowledge_graph.sqlite3. Graphs are never merged
      automatically; review and copy them in with `mempalace kg import-legacy --from ...`

`mempalace status` (default env) reads `~/.mempalace`: 20162 drawers, wings
kalshi_deepbot (room decisions: 71), kalshi_superbot, kalshi_superbot_v15,
home, backups, collected_data, nested.

## A2. Root cause of the two live palaces

The installed unit is stale versus the repo unit:

    $ grep -c "MEMPALACE_DIR" /etc/systemd/system/superbot-dry.service
    0
    $ grep -c "MEMPALACE_DIR" /etc/systemd/system/superbot.service /etc/systemd/system/poly-us-dry.service
    /etc/systemd/system/superbot.service:1
    /etc/systemd/system/poly-us-dry.service:1
    $ systemctl show superbot-dry -p Environment
    Environment=DRY_RUN=true DRY_LOG_FILE=... TMPDIR=... SUPERBOT_WEBHOOK_SECRET=<redacted>

Repo `deploy/superbot-dry.service` line 35 sets
`Environment=MEMPALACE_DIR=/var/lib/superbot/mempalace` (and the same for
deploy/superbot.service:39 and deploy/poly-us-dry.service:29), so the running
dry trader fell back to the bridge default `~/.mempalace`
(mempalace_bridge.py:52-61) and is writing heartbeats/checkpoints into the
coding-agent palace. The poly services (units poly15m-paper, poly5m-collector,
both active) do pin `/var/lib/superbot/mempalace`.

Additional inert guard: `MEMPALACE_EXPECTED_DIR` is referenced in code
(mempalace_bridge.py:65, degraded_reason 74-87) and docs, but is set nowhere on
this host (grep over /opt/superbot and systemd env: no assignment), so the
palace-mismatch check never fires in production. trader.py:1949-1954 does
surface `degraded_reason()` as a `palace` degraded source -- the wiring is
correct, it just has nothing to compare against.

## A3. Access-path inventory (no direct opens requirement)

Sanctioned owner: `mempalace_bridge.py` -- all writes go through the
`mempalace` library (Logstream/KnowledgeGraph) against PALACE_DIR-derived
paths (`_LOGSTREAM_DB`, `_KG_DB`, lines 62-63); reads via
`MemPalaceBridge.read_events` (lines 258-273), documented "the only sanctioned
read path". Verified: NO raw `sqlite3.connect` anywhere in the repo touches a
palace DB -- the only sqlite3.connect hits are local_db/leases/latency/n8n and
measure scripts (none open logstream.sqlite3 or knowledge_graph.sqlite3).

Deviations from the single-owner rule (same palace dir, but bypass the bridge):

1. `agent_bus.py:130-140` -- `peer_status()` constructs
   `Logstream(db_path=os.path.join(mempalace_bridge.PALACE_DIR, LOGSTREAM_DB_FILENAME))`
   directly for heartbeat presence reads.
2. `bus_startup.py:36-37, 89` -- imports `_LOGSTREAM_DB` from the bridge and
   opens `Logstream(db_path=_LOGSTREAM_DB)` directly in
   `sweep_logstream_inbox()`.

CLI subprocess paths (through the mempalace CLI, not raw SQLite -- acceptable
as clients, but env handling varies):

- `local.py:20-79` -- approved CLI wrapper; explicitly forwards
  `MEMPALACE_DIR` to `MEMPALACE_PALACE_PATH` (lines 75-79). Correct.
- `n8n_coordination.py:45-46, 231, 255` -- `python3 -m mempalace logstream
  append`, `cwd=MEMPALACE_HOME` (default `~/mempalace`); relies on inherited
  os.environ for palace selection -- if the n8n-side process lacks
  MEMPALACE_DIR, the CLI defaults to `~/.mempalace`, silently writing to the
  other palace.
- `scripts/deploy.py:76-93` -- same CLI append; no env pinning at all.

Fixed item verified: `poly_us_trader.py:138-179` `_mp_resume_hint()` reads via
`mempalace_bridge.get_default_bridge(...).read_events(...)` (T13-42); docstring
states "never a direct Logstream open". tests/test_m2_poly_mempalace.py:92,111,218
patch it. F-12 (JSON state vs DB cash authority) is explicitly T16-61 scope
(AGENT_TASKSv16.md:285, 589) and remains open -- recorded, not audited here.

Second-palace creation hazards (code that can materialize a new palace):

- `mempalace_bridge.py:295` -- `os.makedirs(PALACE_DIR, exist_ok=True)` in the
  spill path: a wrong/renamed MEMPALACE_DIR fails soft and silently creates a
  fresh palace directory instead of erroring.
- No `mempalace init` / palace-creation call exists in repo code (grep for
  "mempalace init", "create_palace", "init_palace": no hits).

## A4. Health check and safe restart

Present:

- Bridge circuit breaker + metrics (mempalace_bridge.py:108-148, 328-352) and
  `degraded_reason()` (74-87); surfaced as a degraded source in
  trader.py:1949-1954 ("reviewed: palace mismatch must surface as degraded").
- Runbook: docs/vps-runbook.md -- H4 recipe (lines 56-73, incl.
  `sudo install -d -o root -g superbot -m 2775 /var/lib/superbot/mempalace`),
  shutdown ordering with "MemPalace session end + bridge stop (<=5 s)" as step
  4 (lines 225-234), restart verification ("watch one full cycle", line ~295).
- Unit hardening: MEMPALACE_DIR + ReadWritePaths include the palace,
  UMask=0002, superbot group (deploy/*.service).

Missing:

- No mempalace-mcp service exists, hence no health check or documented safe
  restart for it. T16-70 lists `mempalace-mcp` as a planned unit
  (AGENT_TASKSv16.md:648) but nothing is installed
  (`systemctl list-unit-files | grep -i mempalace` -> empty; user units empty;
  /etc/systemd/system has no mempalace/palace unit).
- docs/MEMPALACE_GUIDE.md is stale for this host: identity
  `termux:opencode:kalshi_superbot`, hub 127.0.0.1:8765, backend
  `/opt/mempalace/palace`, termux filesystem paths -- all contradict
  agent_identity.py (`ubuntu:opencode:kalshi_deepbot`) and this host.
- HANDOFF.md contains no mempalace section (grep: 0 hits). The two verbs it
  once confirmed are partially wrong on the installed CLI:
  `mempalace status` works; `mempalace event list` is INVALID --
  `mempalace: error: argument command: invalid choice: 'event'` (valid
  subcommands include `logstream`, `task`, `artifact`, `palace`); the working
  verb is `mempalace logstream list --to-agent <id> --limit N` (verified).

## A5. Acceptance-point runtime test (read-only)

Commands run:

    mempalace search "NO-side floor" --results 3
    mempalace search "forecast coverage" --results 3
    mempalace logstream list --to-agent ubuntu:opencode:kalshi_deepbot --limit 2
    MEMPALACE_PALACE_PATH=/var/lib/superbot/mempalace mempalace logstream list --to-agent ubuntu:service:poly_us_trader --limit 2

Results:

- "NO-side floor": answerable -- verbatim drawer hit in
  `kalshi_superbot_v15/general` (source AGENT_TASKSv15.md) describing the
  side-blind q-floor bug ("a NO LONGSHOT at 3c ... would get q floored to
  0.9928 (wrong direction ...)") and the fix path.
- "forecast coverage": answerable -- verbatim drawer hit in
  `kalshi_deepbot/decisions` that is a prior T15-62 acceptance answer stating
  NO daily forecast series has been filed yet and quoting the measured
  95-99c-band verdict (+4.5..+6.0c/trade honest edge; maker variant dead).
- Entry IDs: search output shows wing/room/source + match scores but NOT entry
  IDs; IDs are obtainable from `mempalace logstream list` (evt_... verified on
  both palaces). So the acceptance letter ("with entry IDs") is only met via
  the logstream verb, not search.
- Coverage gap: both searches ran against `~/.mempalace` only. The services'
  palace has no chroma backend, so service-filed knowledge (heartbeats,
  checkpoints, service drawers) is invisible to a fresh coding-agent session
  and vice versa -- the split palace breaks the one-knowledge-scope intent.

## A6. Tests

tests/test_h3_h4_palace.py (5 tests): no-op bridge without the mempalace
package; `_palace_dir_from_env` precedence; expected-dir mismatch -> degraded;
missing library -> degraded; two identities sharing one tmp_path palace see
each other's events. All unit-level on temp dirs; nothing asserts the HOST has
exactly one palace or that MEMPALACE_EXPECTED_DIR is set in units -- the exact
gap that bit production (A2).

## Key gaps (ranked) and smallest next fixes

1. Two live palaces (FAIL, highest impact). Smallest fix: sync the installed
   `/etc/systemd/system/superbot-dry.service` from `deploy/superbot-dry.service`
   (one line: `Environment=MEMPALACE_DIR=/var/lib/superbot/mempalace`), then
   `sudo systemctl daemon-reload && sudo systemctl restart superbot-dry.service`.
   After that, all service writers pin one palace; the coding agent/MCP still
   reads `~/.mempalace` (see 2).
2. Coding-agent side still points at `~/.mempalace` (opencode MCP entry
   `/home/ubuntu/.local/bin/mempalace-mcp` in `~/.opencode/opencode.json` has
   no palace env). Smallest fix: decide the ONE canonical dir
   (`/var/lib/superbot/mempalace` per the H4 recipe), then add
   `Environment=MEMPALACE_DIR=/var/lib/superbot/mempalace` (or
   MEMPALACE_PALACE_PATH) to the opencode MCP invocation, and set
   `MEMPALACE_EXPECTED_DIR` to the same value in every service unit so
   degraded_reason() finally has a comparator. Consolidation of existing
   content (kg import-legacy / mine) is a separate operator-approved step --
   deliberately NOT done in this read-only audit.
3. No mempalace-mcp systemd unit (FAIL vs T16-62 letter). Smallest fix: add a
   minimal oneshot/simple unit (or document the deliberate decision that the
   MCP server is launched per-session by opencode and must never run as a
   second long-lived server), plus a health check line
   (`mempalace status`) in the runbook.
4. Two direct Logstream opens outside the bridge (agent_bus.py:130-140,
   bus_startup.py:36-37,89). Smallest fix: route both through
   `mempalace_bridge.read_events(...)` (it already accepts
   topic/from_agent/to_agent/order/limit) or add a bridge `latest_heartbeat()`
   helper; delete the duplicate `Logstream` construction.
5. Silent second-palace creation (mempalace_bridge.py:295) and soft-fail env
   mistakes. Smallest fix: on startup, if PALACE_DIR does not exist, log a
   WARNING naming the dir and skip makedirs in the spill path (spill to TMPDIR
   instead), so a bad MEMPALACE_DIR cannot conjure a new palace.
6. Stale docs: docs/MEMPALACE_GUIDE.md (termux-era), invalid `event list` verb,
   stale header comment in deploy/superbot-dry.service ("M1 bridge palace" at
   ~/.mempalace). Smallest fix: one pass updating identity, palace path, and
   the confirmed verbs (`mempalace status`, `mempalace logstream list`).
7. Stray palace dirs (`~/.mempalace/palace`, `/tmp/opencode/new_palace`,
   `~/.mempalace/logstream.db`, legacy `termux:` events). Smallest fix:
   operator-confirmed archive/quarantine; do nothing unilaterally.

## What remains open

- Palace consolidation and backfill of the two KGs (operator decision;
  `mempalace kg import-legacy` suggested by the tool itself).
- mempalace-mcp unit + health check + safe-restart drill (T16-70 overlap).
- F-12 two-sources-of-truth resolution (T16-61 scope; referenced, not audited).
- Whether the daily digest should summarize palace content (T16-62 "Nightly
  n8n job summarizes the day"): daily-digest exists; palace-summary behavior
  unverified; scripts/periodic_insights.py has no scheduler entry found.
- Origin of `/tmp/opencode/new_palace` (created 2026-11-22 08:30; no repo or
  history reference found).
- Acceptance points 12/13 with literal entry IDs from search output (search
  shows sources, not IDs; logstream list provides IDs).
