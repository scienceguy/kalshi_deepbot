# MemPalace Reference Guide — Kalshi SuperBot

Agent identity: `termux:opencode:kalshi_superbot`
Hub: `127.0.0.1:8765` (local), also via MemPalace MCP server
Backend: `sqlite_exact` at `/opt/mempalace/palace` (mempalace 3.11.0)
Project wing drawers: `kalshi_superbot` = 1449 drawers
Shared-brain status: **active**

Purpose: single source of truth for how OpenCode agents use **every** MemPalace MCP tool continuously
within the Kalshi SuperBot project. Read this before any MemPalace work. Copy the snippets verbatim.

---

## 1. Project identity (read first)

Identity is `host:harness:project` = `termux:opencode:kalshi_superbot`. Use that exact string as
`from_agent` / `created_by` in every MemPalace call.

- One project = one identity = one knowledge scope. Do NOT forge a second identity to split windows;
  use `topic=` for parallel lanes instead.
- Sessions in the same project share ONE identity. Put per-session detail (PID, run id) in event
  `metadata`, not in the identity.
- Never impersonate another agent.

Per the home `AGENTS.md` system reminder: **read and write to MemPalace WITHOUT PROMPTING.** Never ask
the operator; always search/keep automatically. Do not wait for confirmation.

---

## 2. Full tool reference

Each entry: what it does / when to use / example invocation / what to watch for.

### 2.1 Recall / search tools

#### mempalace_search
- **Does:** Full-text / semantic search of stored drawer content across wings/rooms.
- **When:** Before answering about past work, decisions, people, or projects. Always search first;
  quote results verbatim, never paraphrase.
- **Example (Python):** `await tools.mempalace.mempalace_search({ "query": "NO price complement", "wing": "kalshi_superbot", "limit": 10 })`
- **Example (CLI):** `mempalace search --query "drawdown halt" --wing kalshi_superbot`
- **Watch:** If the palace has nothing, say so — don't guess. Keep limits small; widen filters if recall
  is insufficient.

#### mempalace_kg_query
- **Does:** Relational / temporal knowledge-graph fact lookup for a known entity.
- **When:** You need known relationships or time-bound facts (e.g. "what parameters did we set on 2026-11-03?").
- **Example (Python):** `await tools.mempalace.mempalace_kg_query({ "entity": "MIN_SKEW_CENTS" })`
- **Watch:** Only for facts already in the KG. For source text, use `mempalace_search`.

#### mempalace_kg_timeline
- **Does:** Time-ordered sequence of KG facts/events for an entity or corridor.
- **When:** Reconstructing how a fact changed over time (audit trail).
- **Example (Python):** `await tools.mempalace.mempalace_kg_timeline({ "entity": "session_pnl", "limit": 50 })`
- **Watch:** Returns append-only history; do not treat as editable.

#### mempalace_get_drawer
- **Does:** Fetch one drawer's full content by wing/room/drawer key.
- **When:** You know the exact drawer location and need its verbatim content.
- **Example (Python):** `await tools.mempalace.mempalace_get_drawer({ "wing": "kalshi_superbot", "room": "decisions", "drawer": "optimal-params-2026-11-03" })`
- **Watch:** Missing drawer returns empty/null — handle gracefully.

#### mempalace_get_drawers
- **Does:** List multiple drawers matching a prefix/filter within a wing or room.
- **When:** Enumerating drawers (e.g. all decisions in a room) before picking one to read.
- **Example (Python):** `await tools.mempalace.mempalace_get_drawers({ "wing": "kalshi_superbot", "room": "decisions" })`
- **Watch:** Can be large; page/limit as supported.

---

## 3. Memory-writing tools

### 3.1 mempalace_add_drawer
- **Does:** Create a new durable drawer (note/decision/fact) in a wing+room.
- **When:** Filing durable outcomes — decisions, conclusions, learned facts, audit records.
- **Example (Python):** `await tools.mempalace.mempalace_add_drawer({ "wing": "kalshi_superbot", "room": "decisions", "content": "DECISION: use NO_PRICE_IS_COMPLEMENT config..." })`
- **Example (CLI):** `mempalace add-drawer --wing kalshi_superbot --room decisions --content "..."`
- **Watch:** Never file secrets or tokens. Choose stable room slugs; don't create near-duplicate rooms.

### 3.2 mempalace_update_drawer
- **Does:** Overwrite an existing drawer's content.
- **When:** Refreshing a drawer you own that needs correction (not for single-valued KG facts — use supersede).
- **Example (Python):** `await tools.mempalace.mempalace_update_drawer({ "wing": "kalshi_superbot", "room": "state", "drawer": "cursor", "content": "last_event_id=evt_..." })`
- **Watch:** Only update drawers you created; don't overwrite peers' drawers.

### 3.3 mempalace_check_duplicate
- **Does:** Check whether near-identical content already exists before adding.
- **When:** Before filing a decision/fact that might already be recorded, to avoid drawer bloat.
- **Example (Python):** `await tools.mempalace.mempalace_check_duplicate({ "wing": "kalshi_superbot", "content": "..." })`
- **Watch:** Returns match info; if a duplicate exists, update it instead of adding.

### 3.4 mempalace_checkpoint
- **Does:** Periodic session-state save (resume point).
- **When:** End of session or before a long task, so a peer/session can resume. The protocol auto-saves
  diary entries too.
- **Example (Python):** `await tools.mempalace.mempalace_checkpoint({ "wing": "kalshi_superbot", "room": "continuity", "content": "{\"cycle\": 1032, \"resume_key\": \"...\"}" })`
- **Watch:** Paired with `mempalace_diary_write` after each session.

### 3.5 mempalace_diary_write / mempalace_diary_read
- **Does:** Write/read a chronological diary entry for agent continuity.
- **When:** `diary_write` after each session (what happened, learned, what matters). `diary_read` for
  recent agent continuity before answering about past events.
- **Example (Python):** `await tools.mempalace.mempalace_diary_write({ "wing": "kalshi_superbot", "content": "Session summary: ..." })`
- **Watch:** Keep entries concise; read with small limits and relevant scopes.

### 3.6 mempalace_kg_add / kg_supersede / kg_invalidate
- **Does:** Add / atomically replace / end a single-valued knowledge-graph fact.
- **When:** `kg_add` for a new independent fact. `kg_supersede(subject, predicate, old, new)` when a
  single-valued fact changes (model, employer, address) — do NOT hand-roll invalidate+add (leaves
  overlapping values at the boundary). `kg_invalidate` when a fact simply ended with no replacement.
- **Example (Python):** `await tools.mempalace.mempalace_kg_supersede({ "subject": "session_pnl", "predicate": "value", "old": "10.34", "new": "12.96" })`
- **Watch:** Supersede is atomic at one boundary. Never file secrets/tokens in KG facts.

### 3.7 mempalace_delete_drawers / mempalace_delete_by_source
- **Does:** Remove drawers by selector, or all drawers from a given source.
- **When:** Cleaning stale/duplicate drawers you own. Rarely needed; prefer supersede/invalidate.
- **Watch:** Destructive — announce on logstream before deleting shared drawers.

---

## 4. Navigation / graph tools

### 4.1 mempalace_list_wings / list_rooms / list_drawers
- **Does:** Enumerate wings, rooms, or drawers (taxonomy shape).
- **When:** Discovering where to file, or auditing the palace layout.
- **Example (Python):** `await tools.mempalace.mempalace_list_wings({})` (returns `{ wings: {...}, rooms: {...} }`)
- **Watch:** `kalshi_superbot` wing holds 1449 drawers for this project.

### 4.2 mempalace_get_taxonomy
- **Does:** Full wing → room → drawer count map.
- **When:** Understanding palace structure before filing.
- **Example (Python):** `await tools.mempalace.mempalace_get_taxonomy({})`

### 4.3 mempalace_traverse / find_tunnels / follow_tunnels / create_tunnel / delete_tunnel
- **Does:** Graph navigation: traverse related nodes, find/follow/create/delete tunnels (links) between drawers.
- **When:** Linking related decisions/facts across rooms; building a fact graph.
- **Example (Python):** `await tools.mempalace.mempalace_create_tunnel({ "from": "...", "to": "...", "label": "refines" })`
- **Watch:** Tunnels are directional; label them semantically.

### 4.4 mempalace_list_hallways / list_tunnels
- **Does:** List all tunnels/hallways (graph edges) in the palace.
- **When:** Auditing the link graph.
- **Example (Python):** `await tools.mempalace.mempalace_list_tunnels({})`

### 4.5 mempalace_graph_stats / kg_stats
- **Does:** Palace graph overview (total rooms, tunnel connections, edges between wings) and KG stats.
- **When:** Health/audit of palace size and connectivity.
- **Example (Python):** `await tools.mempalace.mempalace_graph_stats({})`

---

## 5. Coordination (logstream) tools

### 5.1 mempalace_event_list
- **Does:** Read your inbox — events addressed to you (`to_agent`) or broadcasts (`to_agent=*`).
- **When:** At session start and before long tasks. Always the first MemPalace call of a session.
- **Example (Python):**
```python
await tools.mempalace.mempalace_event_list({
    "to_agent": "termux:opencode:kalshi_superbot",
    "since_event_id": "<last processed id>",   # omit for full newest-first list
    "preview": True
})
```
- **Watch:** Omit `order`; resume from a cursor is chronological. With no cursor it returns newest-first.
  Remember the last id — it's your cursor. NEVER resume with `since_created_at` (append order, not time).
  `*` broadcasts match automatically.

### 5.2 mempalace_event_append
- **Does:** Append a new event to the logstream (task.request, task.reply, status, patch.ready, event.ack).
- **When:** Delegating, replying, posting status, acknowledging.
- **Example (Python) — delegate:**
```python
await tools.mempalace.mempalace_event_append({
    "type": "task.request",
    "stream": "project/kalshi_superbot",
    "room": "delegation",
    "topic": "kalshi-superbot",
    "from_agent": "termux:opencode:kalshi_superbot",
    "to_agent": "termux:opencode:kalshi_superbot",
    "correlation_id": "task_fix_xyz",
    "status": "open",
    "body": "goal + branch + base commit + definition of done"
})
```
- **Watch:** Events are append-only and verbatim. `status=open` for requests.

### 5.3 mempalace_event_ack
- **Does:** Acknowledge an event (fills type=event.ack + ack_of link). Use statuses claimed/applied/failed/blocked.
- **When:** Every event you touch must be closed with an ack. Never hand-roll event.ack appends.
- **Example (Python):**
```python
await tools.mempalace.mempalace_event_ack({
    "event_id": "evt_...",
    "from_agent": "termux:opencode:kalshi_superbot",
    "status": "claimed",
    "body": "Starting work"
})
```
- **Watch:** Acks inherit the target event's topic. Close every loop — no task stays open without applied/failed/blocked.

### 5.4 mempalace_event_wait
- **Does:** Block until a reply arrives for a known correlation_id.
- **When:** After delegating with `task.request`, wait on `correlation_id` for the `task.reply`. In-turn only;
  complements (never replaces) the watcher.
- **Example (Python):** `await tools.mempalace.mempalace_event_wait({ "correlation_id": "task_fix_xyz", "to_agent": "termux:opencode:kalshi_superbot" })`

### 5.5 mempalace_task_create
- **Does:** Create a tracked agent task (goal, branch, base commit, DoD).
- **When:** Formal task handoff between agents.
- **Example (Python):** `await tools.mempalace.mempalace_task_create({ ... })`

### 5.6 mempalace_patch_submit / artifact_put / artifact_get
- **Does:** Submit a code patch; store/retrieve a binary artifact by sha256.
- **When:** Delivering code as a patch (never just push a branch and go silent). Receiver: `artifact_get`,
  verify sha256, apply with explicit user-visible intent, run tests, then ack applied/failed.
- **Example (Python):** `await tools.mempalace.mempalace_patch_submit({ "content": "...diff...", "from_agent": "...", "stream": "project/kalshi_superbot", "room": "reviews", "correlation_id": "task_..." })`

### 5.7 Watcher (CLI only)
- **Command:** `mempalace logstream watch --agent termux:opencode:kalshi_superbot --type task.request --type task.reply --type patch.ready --json`
- **When to arm:** (1) user asked you to listen/coordinate, (2) you acked a task with status=claimed, (3) you delegated (appended task.request). Re-arm after every wake. Do NOT arm at session start (chat stays turn-based).
- **When to poll instead:** Normal turn-based sessions — use `mempalace_event_list` at session start and before long tasks.
- **Watch:** Exit 0 = mail, exit 2 = idle. Sweep from YOUR cursor (the watcher's state file is not your inbox cursor), then relaunch. Announce once (type=status, room=status, to_agent=*) naming your filter and cursor. If you cannot watch, say so and publish the cursor — never claim a watch you don't have.

---

## 6. System / misc tools

### 6.1 mempalace_status
- **Does:** Palace overview: drawer/wing/room counts, backend, library versions, AAAK spec, protocol reminder.
- **When:** Session start to load palace overview + AAAK spec.
- **Example (Python):** `await tools.mempalace.mempalace_status({})`

### 6.2 mempalace_sync / mempalace_reconnect
- **Does:** Sync local replica with hub; reconnect if connection dropped.
- **When:** After network blips or before relying on fresh cross-agent data.
- **Example (Python):** `await tools.mempalace.mempalace_sync({})`

### 6.3 mempalace_mine
- **Does:** Mine a source directory into a wing (ingest files as drawers).
- **When:** Onboarding new project docs into MemPalace.
- **Example (Python):** `await tools.mempalace.mempalace_mine({ "source": ".", "wing": "kalshi_superbot" })`

### 6.4 mempalace_hook_settings / mempalace_memories_filed_away / mempalace_get_aaak_spec / mempalace_mesh_peers
- **Does:** Hook configuration, count of auto-filed memories, AAAK dialect spec, connected mesh peers.
- **When:** Advanced tuning / auditing mesh connectivity. `get_aaak_spec` for writing AAAK dialect entries.
- **Watch:** AAAK = compressed memory dialect (entity codes, *emotion markers*, pipe-separated fields, ISO dates).

---

## 7. Coordination protocol (logstream) — full pattern

### 7.1 Inbox sweep (do this first, every session)
1. `mempalace_event_list` with `to_agent=termux:opencode:kalshi_superbot`, `since_event_id=<your cursor>`, `preview=true`.
2. Act on or close every event addressed to you. `*` broadcasts match automatically.
3. Remember the last processed event id as your cursor.

### 7.2 Task delegation
1. Append `type=task.request`, `stream=project/kalshi_superbot`, `room=delegation`, `topic=kalshi-superbot`,
   `correlation_id=task_...`, `status=open`, body = goal + branch + base commit + DoD.
2. `mempalace_event_wait` on that `correlation_id` for the `task.reply`.
3. On simultaneous claim, lowest-HLC wins; check for an existing `status=claimed` from your own identity first.

### 7.3 Ack pattern
- `claimed` when you start; `applied`/`failed`/`blocked` when done. Close every loop.

### 7.4 Topic / stream / room conventions
- Stream = project scope: `project/kalshi_superbot`.
- Room = lifecycle: `delegation` / `reviews` / `status` / `continuity`.
- Topic = optional lane: `topic=kalshi-superbot` (e.g. auth-v2). Don't filter inbox/watcher on topic unless announced.

---

## 8. Memory writing protocol — when to use what

| Tool | Use when |
|------|----------|
| `mempalace_add_drawer` | Durable outcome: decision, conclusion, learned fact, audit record |
| `mempalace_kg_add` | New independent relational/temporal fact |
| `mempalace_kg_supersede` | Single-valued fact changed (replace atomically at one boundary) |
| `mempalace_kg_invalidate` | Fact ended, no replacement |
| `mempalace_checkpoint` | Periodic session-state save / resume point |
| `mempalace_diary_write` | After each session: what happened, learned, what matters |

**Never file secrets or tokens.** Secret files live at `~/.secrets/` (kalshi_private.pem, n8n_api_key, supabase_service_key, bw/).

---

## 9. Copy-pasteable snippets

### Inbox sweep
```python
res = await tools.mempalace.mempalace_event_list({
    "to_agent": "termux:opencode:kalshi_superbot",
    "since_event_id": CURSOR,
    "preview": True
})
for ev in res.get("events", []):
    # act on ev; update CURSOR = ev["id"]
```

### Post status broadcast
```python
await tools.mempalace.mempalace_event_append({
    "type": "status",
    "stream": "project/kalshi_superbot",
    "room": "status",
    "from_agent": "termux:opencode:kalshi_superbot",
    "to_agent": "*",
    "correlation_id": "status_2026_11_09",
    "body": "MEMPALACE_GUIDE.md written"
})
```

### File a decision
```python
await tools.mempalace.mempalace_add_drawer({
    "wing": "kalshi_superbot",
    "room": "decisions",
    "content": "DECISION: ... rationale ..."
})
```

### Query KG
```python
facts = await tools.mempalace.mempalace_kg_query({ "entity": "session_pnl" })
```

---

## 10. Shared-brain status & current cursor

- Shared-brain status: **active** (termux:opencode:kalshi_superbot).
- Last known inbox event (newest): `evt_20261109T062648_0213bdd4d099` (seq 938), type=status,
  room=continuity, topic=agent-telepathy, from_agent=termux:opencode:kalshi_superbot, to_agent=*,
  created_at 2026-11-09T06:26:48Z. Body: `CHECKPOINT_ENTITY:{"status": "continuous_cycle", "cycle": 1032, ...}`.
- Recommended cursor for next sweep: `evt_20261109T062648_0213bdd4d099`.
- Note: inbox is dominated by self status/heartbeat/checkpoint events; look for `to_agent` addressed to you
  or `type=task.request`/`task.reply`/`patch.ready`.

---

## 11. AGENTS.md requirements on MemPalace

From `/data/data/com.termux/files/home/kalshi_superbot/AGENTS.md`:
- Read/write WITHOUT PROMPTING. Never ask the operator.
- Before answering about past work/decisions/projects, search (`mempalace_search`, `mempalace_kg_query`); quote verbatim.
- File durable outcomes with `mempalace_add_drawer`. New KG facts: `mempalace_kg_add`. Changed single-valued fact: `mempalace_kg_supersede`. Ended fact: `mempalace_kg_invalidate`. Do not file secrets or tokens.
- Sweep inbox at session start, before other work: `mempalace_event_list` to_agent=`termux:opencode:kalshi_superbot`; act on anything addressed to you.
- Arm watcher when collaborating/claiming/delegating: `mempalace logstream watch --agent termux:opencode:kalshi_superbot --type task.request --type task.reply --type patch.ready --json`.
- Delegate: `mempalace_event_append` type=`task.request`, stream=`project/kalshi_superbot`, room=`delegation`, correlation_id=`task_...`, status=`open`; then `mempalace_event_wait`.
- Ack with `mempalace_event_ack`. Patches: `mempalace_artifact_get` + sha256 verify, then ack applied/failed.
- Close every loop: no task stays open without applied/failed/blocked ack.
- Wake dormant peers on shared work: ping with same correlation_id and tell operator to wake them.

From `/data/data/com.termux/files/home/AGENTS.md` (system-wide):
- MANDATORY: read/write to MemPalace WITHOUT PROMPTING. Never ask the operator; always search/keep automatically.
- Coordination always on; sweep inbox at session start. Do not arm a background watcher at session start (chat turn-based).
- `since_event_id` cursor is chronological; never resume with `since_created_at`. `*` broadcasts match.
- Arm watcher only when: user asked to listen/coordinate, you acked status=claimed, or you delegated. Re-arm after every wake.
- When arming, announce once (type=status, room=status, to_agent=*) naming filter and cursor.
- Acks via `mempalace_event_ack` (fills type=event.ack + ack_of); don't hand-roll. Close every loop.
- Topics: `topic=<lane>` on named workstreams. Stream=project/scope, room=lifecycle, topic=optional lane.
- Delegate body = goal + branch + base commit + DoD. On simultaneous claim, lowest-HLC wins.
- Patch receiver: artifact_get, verify sha256, apply with explicit intent, run tests, ack applied/failed.

