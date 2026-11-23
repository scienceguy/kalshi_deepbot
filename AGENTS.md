# Kalshi DeepBot - Agent Runbook

## 1. Current authority and state

- **Active register:** `AGENT_TASKSv17.md` only. v15/v16 are historical.
- **Mode:** dry-run by default. Never enable `--live`, raise size, or relax a
  risk cap without recorded human approval.
- **Strategy gate:** NO-only, hold-to-expiry, taker plus a calibrated-vol gate.
  Maker entry stays disabled until real fills prove a positive lower bound.
  NO-only edge is still UNMEASURED; the T17-08 study is a hard gate.
- **Latest full suite:** 1673 passed, 3 skipped in 226.27s on the e692c94
  worktree (2026-11-22). The current documentation baseline is 23f29a4.
- **Not ready for live:** parity, real-data provenance, NO-only edge, risk
  limits, latency, forecast coverage, and long-running dry evidence are open.

The $3,250,000 figure is a forecast OUTPUT only. Never use it for sizing,
tuning, martingale/doubling, wider bands, or looser limits.

## 2. Non-negotiable rules

1. **Real data only.** No invented prices, probabilities, fills, balances,
   slippage, or interpolated gaps. Missing data means no fill. Remove the
   synthetic 45c fallback before the real-data audit can pass.
2. **Dry/live parity.** One code path and one config. Only the execution
   adapter, key selection, state namespace, and log paths may differ. Remove
   `PAPER_PER_TRADE_PCT` and dry-only sizing. Live must match the dry-validated
   config hash.
3. **NO-only exits.** Every position settles. Never sell or stop out. Only
   unfilled entry orders may be cancelled. Verify `NO_ONLY` tests/default and
   keep `ALLOW_NO_BUYS` off until the NO-side-floor gate is accepted.
4. **Evidence before claims.** Record command, output, exit status, and HEAD in
   the v17 status table. Do not copy prose claims without checking them.
5. **ASCII only.** Run a non-ASCII check after every edit. Existing non-ASCII
   text is not permission to add more.
6. **One logical change per commit.** Run all four source gates and tests,
   review the diff, commit, and push. Do not bundle unrelated files.
7. **Independent review.** A different agent identity must review every
   money-path diff (`gates.py`, `risk.py`, `trader.py`, `poly_us_trader.py`,
   `fees.py`, `signals.py`, and venue clients). Source gates do not catch all
   semantic substitutions or deleted branches.
8. **Delegation.** The coordinating session keeps at least two delegated
   subagents actively completing disjoint tasks at all times until v17 is done
   (maximum five). Replace completed/failed/idle agents immediately and do not
   duplicate completed work.
9. **Secrets and balances.** Read balances from venue APIs only when required.
   Never print or store keys, tokens, raw balances, account IDs, or connection
   strings in logs, docs, MemPalace, n8n, or git.
10. **Cross-venue pairs are basis deviation, not arbitrage.**

Stop and ask the human for a money-path/spec contradiction, discovered secret,
unclear production API status or venue rule, negative NO-only result,
unexplained halt, or any step requiring live orders.

## 3. Repository map

| Path | Role |
|------|------|
| `trader.py` | Kalshi main loop; dry default, exclusive instance lock |
| `poly_us_trader.py` | Polymarket US trader; JSON/DB authority split remains open |
| `signals.py` | Signal evaluation and NO_ONLY gate; maker-disable work remains |
| `gates.py` | ATR, EV, and time-decay gates |
| `risk.py`, `sizing.py` | Risk limits and sizing |
| `fees.py` | Real Kalshi fee math |
| `kalshi_client.py` | Signed Kalshi v2 REST client |
| `polymarket_us_client.py` | Read-only Polymarket US metadata/client helpers |
| `local_db.py` | SQLite storage and online backup |
| `tick_bus.py` | Opt-in event-driven spot snapshot bus |
| `brti_tracker.py` | BRTI-style index tracking |
| `binance_feed.py`, `coinbase_feed.py` | Native public WS/REST market data |
| `cryptofeed_feed.py` | Optional adapter; production smoke currently failed |
| `mempalace_bridge.py` | MemPalace client bridge, off the trading hot path |
| `agent_identity.py` | Canonical agent identities |
| `n8n_client.py`, `emit.py` | Durable n8n event/outbox path |
| `scripts/job_bridge.py` | Allowlisted loopback script bridge for n8n |
| `backtest_engine.py` | Backtest engine; synthetic fallback removal pending |
| `docs/EVAL_PROTOCOL.md` | Evaluation protocol; v17 gate alignment in progress |
| `AGENT_TASKSv17.md` | Living task register and evidence table |
| `tests/` | Pytest suite; current result is in section 1 |

## 4. Setup and safe commands

```bash
cd /opt/superbot
cp .env.example .env       # then fill required credentials locally
.venv/bin/python -m pytest tests/ -q
.venv/bin/python trader.py --once --dry
.venv/bin/python backtest.py --quick --jobs 4
```

- Use `.venv/bin/python`, not a system or Termux interpreter.
- `trader.py --once --dry` takes the same exclusive instance lock as a long
  run. It is not a read-only probe and must not bypass the lock.
- Live loops require both explicit human approval and the existing confirm
  flags. Do not run them as part of agent validation.

## 5. Required verification

After each edit, run the relevant ASCII check and focused tests. Before each
commit run:

```bash
.venv/bin/python scripts/verify_source.py --all
.venv/bin/python scripts/verify_compiles.py --all
.venv/bin/python scripts/verify_undefined.py --all
.venv/bin/python scripts/verify_operators.py
.venv/bin/python -m pytest tests/ -q --tb=short
```

Use a focused suite while iterating, but do not claim full readiness without a
fresh full-suite result. Read money-path diffs even when every gate is green.

## 6. Data plane and latency

- Target event-driven entry from Binance/Coinbase WS and a BRTI-style index.
- Spot older than 1.0 second means no trade. Stale or one-sided venue books
  also mean no trade.
- Targets: p95 tick-to-submit under 500 ms, p99 under 1 second.
- No blocking I/O, LLM, REST (including KL ladder fetches), DB, n8n, or
  MemPalace calls on the hot path.
- Persist ticks on a throttled writer path, not in the decision path.
- `tick_bus` remains opt-in. Cryptofeed 2.5.0 imports and its focused tests
  pass, but the real adapter smoke failed with Coinbase 401 and Binance 451;
  this is not production evidence. Native public-feed paths remain the current
  route.

## 7. SQLite and backups

Local SQLite is the only database. Do not use Supabase tools, dashboards, or
new Supabase credentials. Legacy Supabase client/config/workflow references
remain only until T17-61 removes them.

The T16/T17 authority audit is not green: Polymarket JSON remains an authority
for cash/positions/PnL, migration can duplicate settlement keys, and some
startup/write paths fail open. Do not declare local-first complete until these
are fixed and tested. Keep the n8n outbox; it is separate from Supabase.

Use SQLite-safe online backup or litestream. Offsite restore and the 30-minute
network-down drill still need evidence.

## 8. MemPalace

- Shared palace: `/var/lib/superbot/mempalace`.
- Service units should set both `MEMPALACE_DIR` and
  `MEMPALACE_EXPECTED_DIR` to that path. Do not create or open another palace.
- Identity: `ubuntu:opencode:kalshi_deepbot`; service identities come from
  `agent_identity.py`.
- Query before a task and file evidence afterward. Never file secrets or raw
  balances.
- Current host fix: the dry-trader unit was patched and restarted onto the
  shared palace, and the bridge no longer creates a missing spill directory.
  A managed `mempalace-mcp` unit, direct-open removal, and final retirement of
  other palace directories remain open.
- Useful CLI verbs: `mempalace status`, `mempalace search '<query>'`,
  `mempalace logstream list --to-agent <id>`, `mempalace artifact put`.
  Older `mempalace event list` examples are stale.

## 9. Inter-agent and shared-tree protocol

1. Sweep the logstream inbox, read this file and v17, and query MemPalace.
2. Check `git status` before editing. Another agent's uncommitted file is not
   yours to overwrite or include in a commit.
3. Use disjoint task scopes and concrete acceptance evidence. Task leases and
   enforced reviewer identity are still being completed under T17-63.
4. Never run destructive git commands on the shared tree: no `reset --hard`,
   `checkout -- <path>`, `clean`, `stash`, published-history rebase, or force
   push. Probe experiments in a throwaway clone.
5. Before pushing: `git fetch origin main`; integrate remote changes safely,
   then push. Never claim a commit/push without command output and exit status.

## 10. n8n

- n8n is loopback-only at `127.0.0.1:5678` and never on the tick-to-order path.
- Keep the Execute Command node disabled. Scripts run through
  `scripts/job_bridge.py` on `127.0.0.1:18711` with allowlisted argv.
- Core webhook workflows: backtest, collect, trade, monitor, coordinate,
  control. Backtest/collect/trade/monitor returned HTTP 200 on 2026-11-22;
  coordinate/control health evidence is still pending.
- Digest and watchdogs run on `superbot-jobs.timer`, not as hot-path n8n calls.
- Restore credentials with `python scripts/n8n_creds_sync.py`; do not hardcode
  credentials or recreate them manually.

## 11. Credentials and venue APIs

- Kalshi private key: `~/.secrets/kalshi_private.pem`, matched to
  `KALSHI_KEY_ID`. Base URL is
  `https://external-api.kalshi.com/trade-api/v2`.
- Kalshi authentication is RSA-PSS over timestamp + method + path. `/markets`
  is public; portfolio endpoints require valid authentication.
- The live Kalshi pair is the Bitwarden "Kalshi GPT" key/RSA items. The plain
  "Kalshi API Key"/"Kalshi RSA Key" items are stale. Restore those two mirror
  files after any Bitwarden pull.
- Polymarket US uses Key ID + Ed25519 secret from the protected mirror. Keep
  `polymarket_us_client.py` read-only during verification.
- T17-01 has historical production market evidence, but current market/rules
  revalidation and the account-applicable fee-tier proof are still open. A 401
  or 429 is not evidence that a market does not exist.

## 12. Current gate status

| Task | Current state |
|------|---------------|
| T17-00 | PARTIAL: v17 pointer and full suite are green; findings re-verification continues |
| T17-01 | PARTIAL: production catalog evidence exists; current rules/fee-tier proof open |
| T17-02 | PARTIAL: prior hygiene report exists; full-history/default-value gaps open |
| T17-03 | PARTIAL: F-04/F-05 fixed and reviewed; current HEAD verification continues |
| T17-04 | PARTIAL: real band study exists; single accepted config definition open |
| T17-50 | PARTIAL: protocol exists but v17 NO-only/risk/stop-rule alignment is incomplete |
| T17-06 | OPEN: dry/live parity and config-hash audit |
| T17-07 | OPEN: synthetic-data removal and data provenance |
| T17-08 | OPEN: pre-registered NO-only edge study |

Do not begin after-gate strategy work until this gate passes. If T17-08 finds
no NO region with a market-clustered net-of-fee lower bound above zero, stop,
write the conclusion, and report to the human.
