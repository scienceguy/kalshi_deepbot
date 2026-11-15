# OpenCode Tool Awareness Guide — Kalshi SuperBot

**Project:** Kalshi SuperBot (single-operator live-trading bot for Kalshi 15-minute binary crypto markets)
**Workspace:** `/data/data/com.termux/files/home/kalshi_superbot`
**Platform:** Android / Termux (proot)
**Agent identity:** `termux:opencode:kalshi_superbot`

---

## Purpose

A single reference for how OpenCode agents use **every** available tool in this harness, within the Kalshi SuperBot project. Read before touching code. Copy snippets verbatim.

Core rule: **If a skill or tool applies, you MUST use it.** Reach for tools before asking questions, before exploring codebases, before writing code. Never ask the operator what a tool does — look it up here.

---

## Tool categories (navigate to each section)

| Section | Category | Tools covered |
|---------|----------|---------------|
| 2 | File operations (native) | read, write, edit, glob, grep, shell, execute |
| 3 | Web | webfetch, websearch |
| 4 | MCP databases | sqlite, sqlite-book-l2, supabase |
| 5 | MCP coordination | mempalace (full suite) |
| 6 | MCP browser | tbp |
| 7 | MCP device | droidclaw |
| 8 | Skills | all available skills |
| 9 | OpenCode session management | opencode |
| 10 | Common workflows | copy-pasteable patterns |
| 11 | Decision tree | when to use which tool |
| 12 | Android/proot constraints | /tmp, sockets, proot path bindings |
| 13 | Project conventions | AGENTS.md, mempalace identity |

---

## 2. File operations (native OpenCode functions)

### 2.1 read
- **Does:** Reads the full contents of a file (text), directory listing, image, or PDF. Text lines are prefixed with 1-based line numbers.
- **When:** You need the exact contents of a source file, config, test, log, or to see what files exist in a directory. Always read a file before editing it.
- **Example:**
  ```python
  await functions.read({ "path": "/data/data/com.termux/files/home/kalshi_superbot/trader.py" })
  await functions.read({ "path": "/data/data/com.termux/files/home/kalshi_superbot", "limit": 200 })
  ```
- **Gotchas:** Large files are truncated — use `offset`/`limit` to slice. Images/PDFs are rendered directly (not text-extracted).

### 2.2 write
- **Does:** Creates or overwrites a file. Missing parent directories are created automatically.
- **When:** Creating new files (e.g. this guide) or replacing an entire file's contents. NOT for targeted edits — use `edit` for those.
- **Example:**
  ```python
  await functions.write({ "path": "new_file.py", "content": "print('hello')\n" })
  ```
- **Gotchas:** Overwrites silently. Read the file first if you might lose content. Do not write secrets (`.env`, `*.pem`) — pre-commit hook blocks them.

### 2.3 edit
- **Does:** Replaces exact `oldString` with `newString` in a file. By default `oldString` must occur exactly once; set `replaceAll: true` to replace every occurrence.
- **When:** Targeted changes to an existing file (rename a symbol, fix a line, add a block). Always read the file first to get exact text/indentation.
- **Example:**
  ```python
  await functions.edit({
    "path": "config.py",
    "oldString": "FEE_RATE = 0.07",
    "newString": "FEE_RATE = float(os.getenv('KALSHI_FEE_RATE', '0.07'))"
  })
  ```
- **Gotchas:** Whitespace/indentation must match exactly. Multiple matches fail unless `replaceAll` is set. Typographic quote/dash/space variants are auto-tolerated on retry.

### 2.4 glob
- **Does:** Searches file paths using a glob pattern (e.g. `**/*.py`, `tests/*`).
- **When:** Finding files by name/pattern (all tests, all markdown, scripts in a folder).
- **Example:**
  ```python
  await functions.glob({ "pattern": "tests/test_*.py", "path": "/data/data/com.termux/files/home/kalshi_superbot" })
  await functions.glob({ "pattern": "*.md", "hidden": True })
  ```
- **Gotchas:** Default limit 100 matches. Use `path` to scope; otherwise searches CWD.

### 2.5 grep
- **Does:** Searches file contents using ripgrep regex (or literal text with `literal: true`). Returns matching file paths, line numbers, and line previews.
- **When:** Finding where a function/symbol is defined or used before editing. Always grep before read to locate code.
- **Example:**
  ```python
  await functions.grep({ "pattern": "def set_bot_state", "path": "/data/data/com.termux/files/home/kalshi_superbot", "include": "*.py" })
  await functions.grep({ "pattern": "KALSHI_FEE_RATE", "literal": True })
  ```
- **Gotchas:** Default limit 100 lines. Case-sensitive by default. Use `include` to narrow to file types.

### 2.6 shell
- **Does:** Executes a shell command on Android/Termux. Returns stdout; large output saved to a file with a truncated preview.
- **When:** Only when no dedicated tool exists (git operations, running pytest, installing packages, moving files). Prefer native tools for file read/edit/search.
- **Example:**
  ```python
  await functions.shell({ "command": "python -m pytest tests/ -q", "workdir": "/data/data/com.termux/files/home/kalshi_superbot" })
  await functions.shell({ "command": "git status --short", "workdir": "/data/data/com.termux/files/home/kalshi_superbot" })
  ```
- **Gotchas:** `/tmp` is NOT writable — use `/data/data/com.termux/files/usr/tmp/opencode` for temp files. Long-running commands: use `background: true` and do not poll. Quote paths with spaces.

### 2.7 execute
- **Does:** Runs JavaScript in a confined Code Mode runtime. The only place MCP tools (sqlite, mempalace, supabase, tbp, droidclaw, opencode) are callable from within scripted logic.
- **When:** Composing multiple MCP/tool calls in one scripted batch (e.g. list tables then query each; fetch several MCP resources concurrently).
- **Example:**
  ```python
  await functions.execute({ "code": "const tables = await tools.sqlite['list-tables'](); return tables;" })
  ```
- **Gotchas:** No filesystem access, no imports, no timers (`setTimeout` unavailable). Only the listed MCP tools are callable inside `execute`. `fetch` is available for HTTP. All external access goes through `tools`.

---

## 3. Web

### 3.1 webfetch
- **Does:** Read-only fetch of an HTTP/HTTPS URL, returned as text/markdown/HTML.
- **When:** Grabbing public docs, API references, or research material you can't get from local tools. Read-only only.
- **Example:**
  ```python
  await functions.webfetch({ "url": "https://docs.kalshi.com", "format": "markdown", "timeout": 60 })
  ```
- **Gotchas:** Read-only (no POST). Max timeout 120s. Large results may be preview-truncated.

### 3.2 websearch
- **Does:** Web search using the user's selected search integration, for current information beyond knowledge cutoff (year 2026).
- **When:** Needing recent facts not in local docs (e.g. Kalshi API changes, n8n version notes).
- **Example:**
  ```python
  await functions.websearch({ "query": "Kalshi trade-api v2 RSA-PSS signature" })
  ```
- **Gotchas:** Depends on the configured search integration; results may vary.

---

## 4. MCP databases

### 4.1 sqlite / sqlite-book-l2
Both expose the same read + write surface; `sqlite-book-l2` is the book-library variant (second MCP server). Add as second server in opencode.json if needed.

- **`sqlite.list-tables` / `sqlite-book-l2.list-tables`** — List all tables in the DB.
  - When: Discovering schema before querying.
  - Example: `await tools.sqlite['list-tables']()`
- **`sqlite.describe-table` / `sqlite-book-l2.describe-table`** — Detailed table structure (columns, types).
  - When: Inspecting a table before `SELECT`.
  - Example: `await tools.sqlite['describe-table']({ "tableName": "bot_state" })`
- **`sqlite.query` / `sqlite-book-l2.query`** — Read-only SELECT queries.
  - When: Reading data without mutating.
  - Example: `await tools.sqlite.query({ "sql": "SELECT * FROM bot_state LIMIT 5;" })`
- **`sqlite.execute` / `sqlite-book-l2.execute`** — Data-modifying SQL (INSERT/UPDATE/DELETE/CREATE/DROP).
  - When: Writing data or schema changes.
  - Example: `await tools.sqlite.execute({ "sql": "INSERT INTO bot_state (...) VALUES (...);" })`
- **Gotchas:** `query` is SELECT-only (read-only). For Supabase schema changes use `apply_migration` + `notify pgrst, 'reload schema'`. Never print/store `$SUPABASE_DB_URL`. Ask before destructive statements.

### 4.2 supabase
- **`supabase.list_projects`** — List all Supabase projects.
- **`supabase.get_project`** — Get project details.
- **`supabase.list_organizations`** — List organizations.
- **`supabase.get_user` / `supabase.delete_user`** — User management.
- **When:** Supabase admin/project-level work. Day-to-day bot-state/orders/signals writes go through `supabase_client.py` (REST client), not these MCP tools directly — but these are available for project inspection.
- **Gotchas:** Per AGENTS.md, use `execute_sql`/`apply_migration` for DB changes via MCP; if a call is read-only, report that instead of retrying.

---

## 5. MCP coordination (MemPalace)

Identity: `termux:opencode:kalshi_superbot`. Use as `from_agent`/`created_by` in every call. One project = one identity = one knowledge scope.

### 5.1 Recall / search
- **`mempalace_search`** — Full-text/semantic search of drawer content across wings/rooms. Before answering about past work; quote verbatim. `await tools.mempalace.mempalace_search({ "query": "...", "wing": "kalshi_superbot", "limit": 10 })`
- **`mempalace_kg_query`** — Relational/temporal KG fact lookup for a known entity. `await tools.mempalace.mempalace_kg_query({ "entity": "MIN_SKEW_CENTS" })`
- **`mempalace_kg_timeline`** — Time-ordered KG facts/events for an entity. `await tools.mempalace.mempalace_kg_timeline({ "entity": "session_pnl", "limit": 50 })`

### 5.2 Get drawers
- **`mempalace_get_drawer`** — One drawer's full content by wing/room/drawer key. `await tools.mempalace.mempalace_get_drawer({ "wing": "kalshi_superbot", "room": "decisions", "drawer": "optimal-params-2026-11-03" })`
- **`mempalace_get_drawers`** — List multiple drawers matching a prefix/filter. `await tools.mempalace.mempalace_get_drawers({ "wing": "kalshi_superbot", "room": "decisions" })`

### 5.3 Memory writing
- **`mempalace_add_drawer`** — New durable drawer (decision/fact). Never file secrets. `await tools.mempalace.mempalace_add_drawer({ "wing": "kalshi_superbot", "room": "decisions", "content": "DECISION: ..." })`
- **`mempalace_update_drawer`** — Overwrite an existing drawer you own.
- **`mempalace_check_duplicate`** — Check near-identical content exists before adding.
- **`mempalace_checkpoint`** — Periodic session-state save / resume point.
- **`mempalace_diary_write` / `mempalace_diary_read`** — Write/read chronological diary entry. Write after each session.
- **`mempalace_kg_add`** — New independent relational/temporal KG fact.
- **`mempalace_kg_supersede`** — Atomically replace a single-valued fact (old→new at one boundary).
- **`mempalace_kg_invalidate`** — End a fact with no replacement.
- **`mempalace_delete_drawers` / `mempalace_delete_by_source`** — Destructive removal; announce on logstream first.

### 5.4 Navigation / graph
- **`mempalace_list_wings` / `list_rooms` / `list_drawers`** — Enumerate taxonomy shape.
- **`mempalace_get_taxonomy`** — Full wing→room→drawer count map.
- **`mempalace_traverse` / `find_tunnels` / `follow_tunnels` / `create_tunnel` / `delete_tunnel`** — Graph navigation and links between drawers.
- **`mempalace_list_hallways` / `list_tunnels`** — List graph edges.
- **`mempalace_graph_stats` / `kg_stats`** — Palace/KG health overview.

### 5.5 Coordination (logstream)
- **`mempalace_event_list`** — Read your inbox (to_agent or `*` broadcasts). FIRST call every session. Use `since_event_id=<cursor>`, `preview=true`, omit `order`. Never resume with `since_created_at`.
- **`mempalace_event_append`** — Append event (task.request, task.reply, status, patch.ready, event.ack).
- **`mempalace_event_ack`** — Acknowledge (fills type=event.ack + ack_of). Statuses: claimed/applied/failed/blocked. Close every loop.
- **`mempalace_event_wait`** — Block until a reply arrives for a known correlation_id (in-turn only; complements watcher).
- **`mempalace_task_create`** — Create a tracked agent task (goal, branch, base commit, DoD).
- **`mempalace_patch_submit`** — Submit a code patch (never just push a branch and go silent).
- **`mempalace_artifact_put` / `mempalace_artifact_get`** — Store/retrieve binary artifact by sha256.
- **Watcher (CLI only):** `mempalace logstream watch --agent termux:opencode:kalshi_superbot --type task.request --type task.reply --type patch.ready --json`. Arm when: user asked to listen/coordinate, you acked status=claimed, or you delegated. Re-arm after every wake. Do NOT arm at session start (chat turn-based). Exit 0 = mail, exit 2 = idle.

### 5.6 System / misc
- **`mempalace_status`** — Palace overview (drawer/wing/room counts, backend, versions, AAAK spec). Session start.
- **`mempalace_sync` / `mempalace_reconnect`** — Sync replica with hub; reconnect after drop.
- **`mempalace_mine`** — Mine a source dir into a wing (ingest files as drawers).
- **`mempalace_hook_settings` / `mempalace_memories_filed_away` / `mempalace_get_aaak_spec` / `mempalace_mesh_peers`** — Advanced tuning/audit. AAAK = compressed memory dialect.

**Never file secrets or tokens.** Secret files live at `~/.secrets/` (kalshi_private.pem, n8n_api_key, supabase_service_key, bw/).

---

## 6. MCP browser (tbp)

Firefox-based browser automation at http://127.0.0.1:5678 (n8n) / general web. Passes Cloudflare natively.

- **`tbp.browser_goto`** — Navigate to a URL. `await tools.tbp.browser_goto({ "url": "https://..." })`
- **`tbp.browser_text`** — Read visible text content of the current page. `await tools.tbp.browser_text({})`
- **`tbp.browser_title`** — Get current page title. `await tools.tbp.browser_title({})`
- **`tbp.browser_url`** — Get current page URL. `await tools.tbp.browser_url({})`
- **`tbp.browser_click`** — Click an element (selector). `await tools.tbp.browser_click({ "selector": "button.submit" })`
- **`tbp.browser_type`** — Type text into an input. `await tools.tbp.browser_type({ "selector": "input#q", "text": "kalshi api" })`
- **`tbp.browser_press`** — Press a key (Enter, Escape, etc.). `await tools.tbp.browser_press({ "key": "Enter" })`
- **`tbp.browser_screenshot`** — Capture a screenshot of the current page. `await tools.tbp.browser_screenshot({})`
- **`tbp.browser_a11y`** — Get the accessibility tree of the current page. `await tools.tbp.browser_a11y({})`
- **`tbp.browser_audit`** — One-command page health report. `await tools.tbp.browser_audit({})`
- **`tbp.browser_perf`** — Page performance metrics. `await tools.tbp.browser_perf({})`
- **`tbp.browser_auth_list`** — List all saved auth sessions. `await tools.tbp.browser_auth_list({})`

**When:** Web research that needs interaction (login, clicking, forms), Cloudflare-protected sites, checking n8n webhook health (http://127.0.0.1:5678).
**Gotchas:** Browser state persists across calls within a session. Screenshots/a11y may be large. n8n REST API returns 401 (upstream bug #31011) — use webhooks or direct DB injection instead.

---

## 7. MCP device (droidclaw)

Android/Termux device control via ADB + Kira daemon.

- **`droidclaw.adb_connect`** — Connect wireless ADB: `adb connect IP:PORT`. `await tools.droidclaw.adb_connect({ "ip_port": "192.168.1.50:5555" })`
- **`droidclaw.kira_start`** — Start Kira interactive agent or daemon. `await tools.droidclaw.kira_start({})`
- **`droidclaw.kira_stop`** — Stop background daemon. `await tools.droidclaw.kira_stop({})`
- **`droidclaw.kira_status`** — Show daemon status. `await tools.droidclaw.kira_status({})`
- **`droidclaw.kira_logs`** — View daemon log (optional n lines). `await tools.droidclaw.kira_logs({ "n": 50 })`

**When:** Device-level debugging, ADB wireless connection, Kira daemon management on Android.
**Gotchas:** Android/proot restrictions on sockets and netlink may limit ADB. Kira logs can be verbose — limit `n`.

---

## 8. Skills (via `skill` tool)

Load specialized instructions before doing work that matches. Available:

**Planning/writing:** `writing-plans`, `writing-skills`, `brainstorming`, `executing-plans`, `subagent-driven-development`, `using-git-worktrees`, `using-superpowers`
**Quality/review:** `ponytail`, `ponytail-audit`, `ponytail-debt`, `ponytail-gain`, `ponytail-help`, `ponytail-review`, `ponytail-help`, `receiving-code-review`, `requesting-code-review`, `verification-before-completion`
**Debugging:** `systematic-debugging`, `diagnosing-superpowers`
**Dev workflow:** `test-driven-development`, `finishing-a-development-branch`, `dispatching-parallel-agents`, `shared_brain`
**Integrations:** `hf-cli`, `ifixai`, `mempalace`, `mempalace-recall`, `mempalace-task`, `opencode`, `report`

- **`skill`** — Load a skill's instructions into the conversation. `await functions.skill({ "id": "ponytail" })` or `await functions.skill({ "id": "systematic-debugging" })`
- **When:** Before creative work (brainstorming), before implementing (test-driven-development, executing-plans), before reviewing (ponytail-review, receiving-code-review), before debugging (systematic-debugging), before HF work (hf-cli), before MemPalace ops (mempalace, mempalace-recall, mempalace-task).
- **Gotchas:** A skill must be invoked before the work it governs; it doesn't run the work for you. Some skills (mempalace, opencode) are referenced by the system prompt and may already be active.

---

## 9. OpenCode session management

- **`opencode.list_mcp_resources`** — List documents/records exposed by MCP servers (optionally filter by server). `await tools.opencode.list_mcp_resources({ "server": "sqlite" })`
- **`opencode.read_mcp_resource`** — Read one MCP resource by server + URI. `await tools.opencode.read_mcp_resource({ "server": "n8n", "uri": "n8n://workflows" })`
- **`opencode.session_move`** — Move a session to another directory (or current session). `await tools.opencode.session_move({ "directory": "/path/to/worktree" })`
- **`opencode.session_rename`** — Rename a session. `await tools.opencode.session_rename({ "title": "kalshi-trader-debug" })`
- **`opencode.worktree_create`** — Create a named git worktree. `await tools.opencode.worktree_create({ "name": "fix/auth-bug" })`
- **`opencode.worktree_list`** — List repository worktrees. `await tools.opencode.worktree_list({})`
- **`opencode.worktree_remove`** — Remove a worktree (force optional). `await tools.opencode.worktree_remove({ "directory": "...", "force": true })`

**When:** Managing sessions/worktrees, reading MCP resources not directly callable, isolating feature work via worktrees.
**Gotchas:** Worktrees outside CWD should be made the session's working directory via `session_move`. Never run destructive git commands unannounced in the shared tree (pull before push; no reset/clean/stash on published history).

---

## 10. Common workflows — copy-pasteable patterns

### 10.1 Reading + editing a file safely
```python
# 1. Locate code
res = await functions.grep({ "pattern": "def set_bot_state", "include": "*.py" })
# 2. Read exact text (preserve indentation)
content = await functions.read({ "path": res[0]["file"], "limit": 60 })
# 3. Targeted edit
await functions.edit({
    "path": res[0]["file"],
    "oldString": "        FEE_RATE = 0.07",
    "newString": "        FEE_RATE = float(os.getenv('KALSHI_FEE_RATE', '0.07'))"
})
# 4. Verify
await functions.read({ "path": res[0]["file"], "offset": res[0]["line"] - 3, "limit": 6 })
```

### 10.2 Running tests and checking output
```python
await functions.shell({
    "command": "python -m pytest tests/ -q",
    "workdir": "/data/data/com.termux/files/home/kalshi_superbot"
})
# Then inspect failures, fix, re-run the specific failing file:
await functions.shell({ "command": "python -m pytest tests/test_trader.py -q", "workdir": "..." })
```
Per AGENTS.md verification: all tests must pass (152 passed baseline), `git status` clean, then `git push origin main`.

### 10.3 Querying SQLite / Supabase
```python
# SQLite (local)
tables = await tools.sqlite['list-tables']()
schema = await tools.sqlite['describe-table']({ "tableName": "bot_state" })
rows = await tools.sqlite.query({ "sql": "SELECT * FROM bot_state ORDER BY updated_at DESC LIMIT 10;" })

# Supabase project inspection
projects = await supabase.list_projects()
orgs = await supabase.list_organizations()
```
Per AGENTS.md: use `execute_sql`/`apply_migration` for schema changes; `notify pgrst, 'reload schema';` after; ask before destructive statements; never print `$SUPABASE_DB_URL`.

### 10.4 Web research
```python
# Public docs / read-only
await functions.webfetch({ "url": "https://docs.kalshi.com", "format": "markdown" })
# Current facts beyond cutoff
await functions.websearch({ "query": "Kalshi trade-api v2 RSA-PSS signature path prefix" })
# Interactive / Cloudflare-protected
await tools.tbp.browser_goto({ "url": "http://127.0.0.1:5678" })
await tools.tbp.browser_text({})
```

### 10.5 Delegating to subagents (MemPalace task.request)
```python
await tools.mempalace.mempalace_event_append({
    "type": "task.request",
    "stream": "project/kalshi_superbot",
    "room": "delegation",
    "topic": "kalshi-superbot",
    "from_agent": "termux:opencode:kalshi_superbot",
    "to_agent": "termux:opencode:kalshi_superbot",
    "correlation_id": "task_fix_abc123",
    "status": "open",
    "body": "Goal: ... Branch: ... Base commit: ... DoD: tests pass"
})
reply = await tools.mempalace.mempalace_event_wait({
    "correlation_id": "task_fix_abc123",
    "to_agent": "termux:opencode:kalshi_superbot"
})
```

### 10.6 Invoking a skill
```python
await functions.skill({ "id": "systematic-debugging" })   # before debugging
await functions.skill({ "id": "ponytail-review" })         # before reviewing a change
await functions.skill({ "id": "test-driven-development" }) # before implementing a feature
```

### 10.7 Browser automation (tbp)
```python
await tools.tbp.browser_goto({ "url": "http://127.0.0.1:5678" })
await tools.tbp.browser_text({})          # read page
await tools.tbp.browser_audit({})         # page health
await tools.tbp.browser_screenshot({})    # capture
```

### 10.8 MemPalace coordination — full loop
```python
# 1. Inbox sweep (first call of session)
inbox = await tools.mempalace.mempalace_event_list({
    "to_agent": "termux:opencode:kalshi_superbot",
    "since_event_id": CURSOR,
    "preview": True
})
# 2. File a durable decision
await tools.mempalace.mempalace_add_drawer({
    "wing": "kalshi_superbot",
    "room": "decisions",
    "content": "DECISION: ... rationale ..."
})
# 3. Supersede a single-valued fact
await tools.mempalace.mempalace_kg_supersede({
    "subject": "session_pnl", "predicate": "value", "old": "10.34", "new": "12.96"
})
# 4. Ack an event
await tools.mempalace.mempalace_event_ack({
    "event_id": "evt_...",
    "from_agent": "termux:opencode:kalshi_superbot",
    "status": "claimed",
    "body": "Starting work"
})
# 5. Submit a patch
await tools.mempalace.mempalace_patch_submit({
    "content": "--- a/trader.py\n+++ b/trader.py\n...",
    "from_agent": "termux:opencode:kalshi_superbot",
    "stream": "project/kalshi_superbot",
    "room": "reviews",
    "correlation_id": "task_fix_abc123"
})
```

---

## 11. Decision tree — when to use which tool

| Goal | First tool to reach for | Why |
|------|------------------------|-----|
| Find where a symbol is defined/used | `grep` | Precise, fast, returns line numbers; then `read` that file |
| See file contents | `read` | Exact text with line numbers; slice with offset/limit |
| Find files by name | `glob` | Pattern match across the tree |
| Edit one file | `edit` | Targeted replacement; preserves surrounding code |
| Create/replace a whole file | `write` | New files or full replacement |
| Run a command (git, pytest, pip) | `shell` | Only when no dedicated tool exists |
| Compose multiple MCP calls | `execute` | Scripted JS batch; only MCP tools callable inside |
| Read public URL (read-only) | `webfetch` | text/markdown/html; no POST |
| Current facts beyond knowledge cutoff | `websearch` | 2026-current info |
| Interactive web / Cloudflare / n8n health | `tbp.*` | Firefox automation; click/type/press/screenshot |
| Query local SQLite | `sqlite.*` | list-tables → describe-table → query (SELECT) / execute (write) |
| Supabase project/org/user | `supabase.*` | Admin-level project inspection |
| Remember / recall past work | `mempalace_search` / `kg_query` | Before answering about past decisions; quote verbatim |
| File a durable decision/fact | `mempalace_add_drawer` / `kg_add` / `kg_supersede` | Never file secrets |
| Coordinate / delegate / ack | `mempalace_event_*` | Inbox sweep first; close every loop with ack |
| Browser-based device (ADB/Kira) | `droidclaw.*` | Android device control |
| Load specialized instructions | `skill` | Before the work it governs |
| Session/worktree/MCP-resource mgmt | `opencode.*` | Move/rename sessions, worktrees, read MCP resources |

**Key ordering rules:**
1. `grep` before `read` to locate code; never `read` a whole tree to find one symbol.
2. `execute` for MCP calls inside scripted logic; native tools (read/write/edit/grep/glob/shell/webfetch/websearch/skill/subagent/experimental_image_generate) are direct functions, not inside `execute`.
3. `shell` only when no dedicated tool exists (git, pytest, package installs).
4. MemPalace first thing every session: inbox sweep before any other work.

---

## 12. Android/proot constraints

- **`/tmp` is NOT writable** (restricted mount / Android). Never write temp files to `/tmp`. Use `/data/data/com.termux/files/usr/tmp/opencode` (pre-created, approved) for any temporary writes.
- Prefer `/data/data/com.termux/files/home/tmp/opencode` over generic `/tmp`.
- **Socket restrictions:** Android/proot limits sockets and netlink. ADB (droidclaw) wireless connect may be limited; n8n at `http://127.0.0.1:5678` is local HTTP and works. MemPalace hub at `127.0.0.1:8765` is local.
- **Proot path bindings:** Work within `/data/data/com.termux/files/home/kalshi_superbot`. Absolute paths must match the proot layout; don't assume Linux `/home` or `/opt` paths unless verified.
- **File permissions:** `.env` is `chmod 600`; secret PEM at `~/.secrets/kalshi_private.pem` `chmod 600`; `~/.secrets/bw/` dir `chmod 700`. Pre-commit hook blocks `.env`/`*.pem` from being staged.
- **Large output:** `shell` and MCP results may be preview-truncated; save to a file in the approved temp dir if you need full output.

---

## 13. Project conventions (AGENTS.md + mempalace)

**Agent identity:** `termux:opencode:kalshi_superbot`. Use as `from_agent`/`created_by` in every MemPalace call. One project = one identity; use `topic=` for parallel lanes, not forged identities.

**Read/write MemPalace WITHOUT PROMPTING.** Never ask the operator; always search/keep automatically.

**Session start:** Sweep inbox (`mempalace_event_list`, `to_agent=termux:opencode:kalshi_superbot`, `since_event_id=<cursor>`, `preview=true`) before other work. Act on everything addressed to you; `*` broadcasts match automatically. Remember the last id as your cursor — never resume with `since_created_at`.

**Watcher:** Arm `mempalace logstream watch --agent termux:opencode:kalshi_superbot --type task.request --type task.reply --type patch.ready --json` only when (1) user asked to listen/coordinate, (2) you acked status=claimed, or (3) you delegated. Re-arm after every wake. Do NOT arm at session start. Announce once (type=status, room=status, to_agent=*) naming filter and cursor.

**Delegate:** `mempalace_event_append` type=task.request, stream=project/kalshi_superbot, room=delegation, topic=kalshi-superbot, correlation_id=task_..., status=open, body = goal + branch + base commit + DoD. Then `mempalace_event_wait` on correlation_id.

**Ack:** `mempalace_event_ack` (fills type=event.ack + ack_of). Statuses: claimed/applied/failed/blocked. Close every loop — no task stays open without applied/failed/blocked.

**Patches:** Receiver does `mempalace_artifact_get`, verifies sha256, applies with explicit intent, runs tests, then ack applied/failed. Never just push a branch and go silent.

**Memory writing:** `add_drawer` for durable outcomes; `kg_add` for new KG facts; `kg_supersede` for changed single-valued facts; `kg_invalidate` for ended facts. `checkpoint` for session-state save; `diary_write` after each session. Never file secrets/tokens.

**Git:** Pull before push (`git fetch && git pull --rebase origin main`). Small commits, small pushes. No destructive git commands unannounced. Commit messages prefixed with `Must fix:`/`Should fix:`/`Nice-to-have:`.

**Verification before done:** `python -m pytest tests/ -q` all pass; all 4 n8n webhooks return HTTP 200; `kalshi_client.get_balance()` returns dollars (cents/100); `git status` clean; `git push origin main`.

**Optimal parameters (backtest 2026-11-03):** MIN_SKEW_CENTS=3, MIN_TREND_CENTS=8, ENTRY_WINDOW 6-11 min, ENTRY_BAND 15-55, MIN_NO_ENTRY_CENTS=15, MAX_NO_ENTRY_CENTS=55, PER_TRADE_PCT=0.08, AGGRESSIVE_FILL=False, PAPER_PER_TRADE_PCT=0.95 (dry-run only).

---

**Rule:** If a skill or tool applies, you MUST use it. Reach for tools before asking questions, before exploring codebases, before writing code.
