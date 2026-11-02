# Kalshi SuperBot — AGENTS.md

Single-operator live-trading bot for Kalshi 15-minute binary crypto markets.
Places favourite-maker limit orders (YES/NO at 75–95c), evaluates signals via an
LLM with 7-layer fallback, writes signals/orders/bot-state to Supabase, and
routes settlement P&L through n8n webhooks.

## Repo layout

| File | Purpose |
|------|---------|
| `trader.py` | Main trading loop (entry point). Dry-run by default; `--live` + `KALSHI_CONFIRM_LIVE=true` for real money. |
| `signals.py` | Signal engine: two-sided favourite maker, stability check, confidence scoring, LLM eval integration. |
| `market_finder.py` | Market discovery — parallel series fetch, orderbook lookup, MAIN + HEDGE_NO selection. |
| `kalshi_client.py` | Signed Kalshi v2 REST client (RSA-PSS, `/trade-api/v2` path prefix). |
| `fees.py` | Kalshi taker fee math (configurable `KALSHI_FEE_RATE`, default 0.07). |
| `risk.py` | Cash floor, exposure ceiling, per-trade cap, drawdown halt, consecutive-loss cooldown. |
| `resilience.py` | Cycle backoff + client-rebuild supervisor with rebuild cap. |
| `n8n_client.py` | n8n webhook/API client; `n8n_health_check()` used at startup. |
| `supabase_client.py` | Supabase REST client with circuit breaker + batch bot-state writes. |
| `llm_eval.py` | 7-layer LLM evaluation (OpenRouter/Groq/DeepSeek/OpenAI/Gemini/DSH/Ollama). |
| `latency_arb.py` | Last-minute taker edge strategy (unused in main flow). |
| `backtest.py` / `parallel_backtest.py` | Parameter-sweep backtests (public + authenticated API). |
| `backtest_engine.py` | Shared backtest engine (entry logic, P&L, sweep grid). |
| `data_collector.py` | Historical market snapshot collector for backtest data. |
| `kalshieye_downloader.py` | Kalshi Eye orderbook downloader. |
| `kalshi_price_data_client.py` | Optional kalshi-price-data SDK wrapper. |
| `inject_n8n_workflows.py` | Injects 4 n8n workflows into n8n's SQLite DB directly. |
| `add_n8n_credentials.py` | Inserts encrypted credentials into n8n DB. |
| `tests/` | 69 pytest tests (fees, risk, signals, kalshi_client, market_finder, n8n, supabase, kalshi_price_data, trader). |

## Environment

- `.env` is gitignored (`chmod 600`). Copy from `.env.example` and fill in real credentials.
- Kalshi private key lives at **`~/.secrets/kalshi_private.pem`** (`chmod 600`),
  pointed to by `KALSHI_KEY_FILE`. It must match `KALSHI_KEY_ID` exactly.
  (It used to be `kalshi_private.pem` in the repo root — do not put secrets in the repo.)
- `DRY_RUN=true` by default. `python trader.py --live` requires `KALSHI_CONFIRM_LIVE=true` in `.env`.
- `KALSHI_BASE_URL` must contain `/trade-api/v2` (validated at client construction).
- Other secrets: `~/.secrets/n8n_api_key`, `~/.secrets/supabase_service_key`,
  `~/.secrets/bw/` (durable credential mirror). See the Credentials section below.

## How to run

```bash
cd /data/data/com.termux/files/home/kalshi_superbot

# 1. Copy env + fill credentials
cp .env.example .env
# edit .env with real KALSHI_KEY_ID, KALSHI_KEY_FILE, SUPABASE_*, N8N_API_KEY

# 2. Run tests (should all pass)
python -m pytest tests/ -q

# 3. Dry-run one cycle
python trader.py --once --dry

# 4. Run backtest
python backtest.py --quick --jobs 4

# 5. Live loop (needs KALSHI_CONFIRM_LIVE=true in .env)
python trader.py --live
```

## n8n

- n8n runs at `http://127.0.0.1:5678`.
- 4 workflows injected into n8n's SQLite DB: `superbot-backtest`, `superbot-collect`, `superbot-trade`, `superbot-monitor`.
- All 4 return HTTP 200 on health-check webhook. `llm-trade-veto` (external) returns 200 too.
- `n8n_client.py` webhook timeout = `CYCLE_TIMEOUT_SECONDS - 5` (25s) so webhooks never block a cycle.
- n8n REST API returns 401 (upstream bug #31011); webhooks + direct DB injection are the working path.

## Credentials — durable store (READ THIS BEFORE TOUCHING SECRETS)

Agents keep deleting n8n credentials. The secrets are NOT stored in n8n or in
this repo as the source of truth. They live in two places, both outside the repo:

1. **Bitwarden** (ultimate source) — every "secure note" in the vault.
2. **`~/.secrets/bw/`** — durable local mirror, one protected JSON per item
   (`chmod 600`, dir `chmod 700`) plus `index.json`. Outside the repo, so repo
   cleanups and `git` operations cannot delete it.

n8n holds a **copy** of every credential, recreated from the mirror at any time.

| Task | Command |
|------|---------|
| Recreate every credential in n8n (idempotent, safe to re-run) | `python scripts/n8n_creds_sync.py` |
| Verify all are present | `python scripts/n8n_creds_sync.py --check` |
| List mirror items | `python scripts/n8n_creds_sync.py --list` |
| Also delete n8n creds not in the mirror | `python scripts/n8n_creds_sync.py --prune` |
| Refresh mirror from Bitwarden | `BW_PASSWORD=... python scripts/bw_pull_secure_notes.py` |
| Move the Kalshi PEM / n8n key into `~/.secrets/` | already done — see `.env` `*_FILE` vars |

- **If a credential is missing from n8n: run `python scripts/n8n_creds_sync.py`.** That is the whole fix.
- A background guard (`scripts/n8n_creds_guard.sh`, termux-job-scheduler job
  **7001**, every 15 min, persisted) re-runs the sync automatically.
- `add_n8n_credentials.py` is deprecated; use `scripts/n8n_creds_sync.py`.
- **Never hardcode a secret in source.** A pre-commit hook
  (`scripts/install-pre-commit.sh`) blocks `.env`/`*.pem` and scans staged
  additions for key patterns. Re-install it on a fresh clone.
- Secret files: `~/.secrets/kalshi_private.pem`, `~/.secrets/n8n_api_key`,
  `~/.secrets/supabase_service_key`, `~/.secrets/bw/`.

## Supabase database access

Use the `supabase` MCP tools for all database work; never ask me to use the dashboard SQL editor.
- Before changing anything, call `list_tables` (verbose) to see what exists.
- Use `execute_sql` for queries and data changes.
- Use `apply_migration` for schema changes (create/alter/drop), with a snake_case name.
- Tables in scope: public.bot_state, public.signals, public.orders. Never touch other tables.
- After schema changes, run `notify pgrst, 'reload schema';` via `execute_sql`.
- If a call is rejected, report the exact error. If it says read-only, tell me instead of retrying.

## Database changes

Run SQL with `psql "$SUPABASE_DB_URL" -c "..."` or `-f file.sql`. Put schema changes in `supabase/schema.sql` (idempotent), apply with `-v ON_ERROR_STOP=1 -1`, then run `notify pgrst, 'reload schema';`. Never print or store `$SUPABASE_DB_URL`. Ask before any destructive statement.

## Kalshi API (production)

- Base URL: `https://external-api.kalshi.com/trade-api/v2`
- Auth: RSA-PSS over `timestamp_ms + METHOD + /trade-api/v2/path` (path prefix included).
- `/markets` is public (200 without auth). `/portfolio/balance`, `/portfolio/positions`, `/portfolio/orders` require valid key + signature.
- `~/.secrets/kalshi_private.pem` must match `KALSHI_KEY_ID` exactly; mismatched keys return `INCORRECT_API_KEY_SIGNATURE`.
- Balance returned in cents; divide by 100 for dollars.

## MemPalace shared brain

Host:harness:project = `termux:opencode:kalshi_superbot`.

- Read/write to MemPalace WITHOUT PROMPTING. Never ask the operator.
- Before answering about past work/decisions/projects, search the palace (`mempalace_search`, `mempalace_kg_query`). Quote results verbatim.
- File durable outcomes with `mempalace_add_drawer`. New KG facts: `mempalace_kg_add`. Changed single-valued fact: `mempalace_kg_supersede`. Ended fact: `mempalace_kg_invalidate`.
- Do not file secrets or tokens.

Coordination (logstream):
- Always sweep the mempalace inbox at session start, before other work: `mempalace_event_list` to_agent=`termux:opencode:kalshi_superbot`; act on anything addressed to you.
- Arm `mempalace logstream watch --agent termux:opencode:kalshi_superbot --type task.request --type task.reply --type patch.ready --json` when collaborating/claiming/delegating.
- Delegate with `mempalace_event_append` (type=`task.request`, stream=`project/<name>`, room=`delegation`, correlation_id=`task_...`, status=`open`, body = goal + branch + base commit + DoD), then `mempalace_event_wait` on `correlation_id`.
- Ack with `mempalace_event_ack`. Apply patches with `mempalace_artifact_get` + sha256 verify, then ack `applied`/`failed`.
- Close every loop: no task stays open without an applied/failed/blocked ack.
- Wake dormant peers on shared work: when an inbox event arrives for a task another dormant agent is also on, ping it to resume (`mempalace_event_append` to_agent=`<peer>`, same `correlation_id`) and tell the operator to wake it.

## Known issues / audit findings (from ponytail-audit + follow-up)

| # | File:Lines | Issue | Status |
|---|------------|-------|--------|
| 1 | `trader.py:380-390` | Settlement PnL can double-count on replay crash | FIXED — queue entry removed before crediting PnL |
| 2 | `trader.py:465-466` | Position popped before PnL booked | FIXED — PnL booked before position removed |
| 3 | `trader.py:708-726` | Float contracts truncated in API order but `dollar_risk` keeps float → cash over-reservation | FIXED — contracts floored to int before `dollar_risk` |
| 4 | `kalshi_client.py:63` | `_verify_key` set verified before check succeeded → permanent poison on transient failure | FIXED — verified only after success, with `_verifying` re-entrancy guard |
| 5 | `trader.py:667-680` | Orders recorded as filled before fill confirmation | FIXED — only recorded when `fill_count > 0` |
| 6 | `trader.py:445-447, 796-799` | NO order price assumed complemented, unverified | NOT FIXED — verify against Kalshi before live |
| 7 | `supabase_client.py:189-232` | Supabase write failures return `None` silently; no alert on schema/4xx | FIXED — `_supabase_record_failure()` called on 4xx |
| 8 | `data_collector.py:110-116` | Closed markets store negative `minutes_left` | FIXED — clamped to 0 |
| 9 | `trader.py:917-931` | Lock file uses `/proc/{pid}` (unreliable on Android) | NOT FIXED — still uses /proc check |
| 10 | `signals.py:130-138` | Module-level `_bid_hist`, not persisted on shutdown | FIXED — saved to `bid_hist_{dry,live}.json` |
| 11 | `trader.py:423-428` | Transient balance failure skips entire cycle | NOT FIXED |
| 12 | `market_finder.py:189-192` | Only ONE main market returned per cycle | NOT FIXED |
| 13 | `market_finder.py:159-160` | MAIN selected on raw mid, dropped after orderbook fetch | FIXED — bid history and market finder refactored |
| 14 | `risk.py:43-55` | Drawdown halt uses cost-basis, not mark-to-market | NOT FIXED |
| 15 | `trader.py:508-523, 839-845` | `session_pnl` drift between n8n and Supabase | PARTIAL — batched bot-state writes added |
| 16 | `n8n_client.py:289-293` | Health check writes through trade webhook | FIXED — uses `superbot-trade` health-check action (returns 200) |
| 17 | `kalshi_client.py:191-208` | `_last_req` set before 429 retry loop → burst after rate limit | FIXED — gap enforced inside rate-limit block |
| 18 | `config.py:189-199` | `DRY_RUN=false` in `.env` silently enables live | FIXED — `.env.example` defaults `KALSHI_CONFIRM_LIVE=false`; `--live` requires explicit confirm |
| 19 | `backtest_engine.py:64-150` | Lookahead bias: `last_price` as entry price | NOT FIXED — documented warning in docstrings |
| 20 | `backtest_engine.py:170-180` | Balance updated per trade in list order, not time order | NOT FIXED |
| 21 | `backtest.py/parallel_backtest.py` | Default sweep huge (~147k combos), slow on Android | FIXED — `--quick` default, jobs capped at CPU count |
| 22 | `signals.py:202-211` | Stability gate blocks all trades ~3min on fresh start | NOT FIXED (by design) |
| 23 | `fees.py:19` | `FEE_RATE` from env, no range validation | FIXED — `0 <= FEE_RATE <= 1` validated at import |
| 24 | `market_finder.py:196` | `MAX_HEDGE_PER_SERIES=0` disables hedges | NOT FIXED (deliberate — hedges off by default) |
| 25 | `trader.py:402` | Calls private `signals._maybe_save_bid_hist` | FIXED — public `se._maybe_save_bid_hist` called |
| 26 | `supabase_client.py:333-344` | `get_bot_state` returns None on corrupt JSON | FIXED — logs warning on decode failure |
| 27 | `kalshi_client.py:41-49` | `KALSHI_BASE_URL` validation hard-fails on non-standard URLs | FIXED — warns instead of failing |
| 28 | `trader.py:870-872` | `SIGALRM` can interrupt in-flight `requests` call | NOT FIXED — per-call `timeout=` used; session rebuilt on timeout |
| 29 | `add_n8n_credentials.py:117-126` | Module-level side effects mutate n8n DB on import | FIXED — config read moved into `load_encryption_key()` called from `main()` |
| 30 | `inject_n8n_workflows.py` | Direct n8n SQLite writes bypass API | FIXED — idempotent (deletes by webhook path before insert) |
| 31 | `fees.py:19` | Hardcoded 0.07 fee multiplier | FIXED — `KALSHI_FEE_RATE` env var (default 0.07) |
| 32 | `.env` / `kalshi_private.pem` present locally | Secret-scanning guard | FIXED — `.git/hooks/pre-commit` blocks `.env`/`*.pem` commits; `scripts/install-pre-commit.sh` installs it |
| 33 | `trader.py:711` | Startup initialises Supabase client and logs state | FIXED — graceful degradation |
| 34 | `market_finder.py:124-129` | Sequential per-series fetches with retries | FIXED — parallel `ThreadPoolExecutor` over series |
| 35 | `signals.py:130` | Module-level `_bid_hist` global, file persistence | FIXED — scoped to `bid_hist_{dry,live}.json` |
| 36 | `trader.py:543-545` | 4 sequential `set_bot_state` writes per cycle | FIXED — batched via `set_bot_state_batch` |
| 37 | `latency_arb.py:176` | `run(client, markets)` accepts unused `client` | FIXED — `run(markets)` only |
| 38 | `parallel_backtest.py:55` | Public fetch `limit=1000`, no cursor pagination | FIXED — cursor pagination added |
| 39 | `kalshieye_downloader.py:213` | Only `files[0]` downloaded per date | NOT FIXED |
| 40 | `data_collector.py:13-12` | Docstring says dry-run, collects closed historical | FIXED — docstring corrected |
| 41 | `config.py:63` | `_env_bool` silently returns default for unparseable values | NOT FIXED |
| 42 | `tests/` — `test_kalshi_price_data_client.py`, `test_n8n_client.py` shallow | Weak coverage | PARTIAL — added `tests/test_trader.py` (8 tests) |
| 43 | `tests/` — no tests for `market_finder.find_active_markets` / `rank_signals` | NOT FIXED |
| 44 | `trader.py` — no per-market/day loss limit beyond drawdown | NOT FIXED |
| 45 | `backtest_engine.py:258-255` | Sweep tests ignored params (`MIN_SKEW`, etc.) | FIXED — `StratParams` aligns with grid |
| 46 | `kalshi_price_data_client.py:133-142` | `health_check()` may call missing SDK method | NOT FIXED |
| 47 | `add_n8n_credentials.py` / `inject_n8n_workflows.py` | Keys visible in process list | PARTIAL — env var paths for key files |
| 48 | `config.py:233-339` | `validate_*` not auto-called on import | NOT FIXED — called explicitly by `trader.py` |
| 49 | `resilience.py:44` | Backoff starts at `base_backoff` (5s) on first failure | NOT FIXED |
| 50 | `kalshi_client.py:265` | `get_markets` cache keyed by `(series, status, limit)`, no invalidation | NOT FIXED (30s TTL acceptable) |
| 51 | `trader.py:586-593` | `set_bot_state` writes every cycle even when unchanged | FIXED — batched single write |
| 52 | `historical_data/`, `hf_data/` in repo | Repo bloat | NOT FIXED |
| 53 | `bot.log`, `*_result.json` in working tree | Clutter | NOT FIXED |
| 54 | `llm_eval.py:324-332` | System prompt says 2 keys, parser accepts extra | NOT FIXED |
| 55 | `parallel_backtest_hf.py:77-85` | NO branch uses `100 - mid_proxy`, no skew/trend gates | NOT FIXED — HF results not applicable to live |
| 57 | `config.py:143-144` | `CASH_FLOOR_PCT=0.70` + `EXPOSURE_CEILING_PCT=0.30` sum to 1.0 | NOT FIXED (rounding epsilon handled) |
| 58 | `signals.py:46-50` | `is_trade` allows empty `side` | NOT FIXED |
| 60 | `supabase_client.py:72-140` | Credential loading order opaque | NOT FIXED |

## Git workflow

- Commit early and often; each commit message prefixed with `Must fix:`, `Should fix:`, or `Nice-to-have:`.
- `git push origin main` after every meaningful change.
- Pre-commit hook blocks `.env` and `*.pem` from being staged.

## CONCURRENT AGENTS — READ BEFORE ANY GIT COMMAND

**More than one agent edits this checkout at the same time.** There is a single
working tree (`/data/data/com.termux/files/home/kalshi_superbot`) shared by every
agent, and all of them push to the same `main`. Treat the tree as shared state.

Rules:

- **Pull before you push.** Always `git fetch && git pull --rebase origin main`
  right before pushing, so you graft onto whatever a peer just pushed.
- **Never run destructive git commands unannounced:** `git reset --hard`,
  `git checkout -- <path>`, `git clean`, `git stash`, `git rebase` on published
  history, or any `git push --force`. Announce on the logstream first (see below)
  and re-read the tree. An uncommitted file you did not write is a peer's work.
- **Check `git status` before editing a file** you did not open yourself; if it
  has unexpected changes, stop and look — do not overwrite.
- **Small commits, small pushes.** Long-lived uncommitted work in a shared tree
  gets lost.
- If you rewrite published history (only for a secret leak or similar), you MUST
  post a `status` broadcast with the new HEAD and the reset instructions.

### Coordination channel (MemPalace logstream)

Before a risky or cross-cutting change, announce and listen:

```bash
mempalace logstream watch --agent termux:opencode:kalshi_superbot \
  --type task.request --type task.reply --type status --json
```

- Post status: `mempalace_event_append` (type=`status`, stream=`project/kalshi_superbot`,
  room=`status`, to_agent=`*`).
- Delegation: `type=task.request`, room=`delegation`, correlation_id=`task_...`.
- Use `topic=kalshi-superbot` to tag a lane and avoid stepping on unrelated work.

### Current history note

`main` was history-rewritten on 2026-11-01 to purge a leaked key (see the
Credentials section). HEAD moved to `ac0b91b`; **all SHAs before that are
invalid**. If your local history predates the rewrite: `git fetch` then
`git reset --hard origin/main` (after saving any local work as a patch).

## Verification checklist before claiming done

- [x] `python -m pytest tests/ -q` → all pass (118 passed)
- [x] All 4 n8n webhooks return HTTP 200 (superbot-backtest/collect/trade/monitor)
- [x] `kalshi_client.get_balance()` returns a dollar amount (not cents) — `cents / 100.0`
- [x] `git status` clean
- [x] `git push origin main` completed
