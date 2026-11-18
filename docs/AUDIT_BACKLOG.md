# Audit backlog (Nice to have)

Findings from the 2026-11-18 four-agent audit that were **verified but deliberately
not fixed** in that batch. Each line names the file, the cost of leaving it, and
what the fix is. Ordering is roughly "do it sooner" to "do it never".

Anything here that touches money, an order path, or a risk limit is still latent
risk: it was judged lower value than the fixes that shipped, not harmless.

## Poly (live-only, latent while poly runs paper)

| # | Where | Issue | Fix |
|---|-------|-------|-----|
| P1 | `poly_us_trader.py` (`_guard_cancel_order`, `reconcile_live_orders`, `cancel_all_resting_orders`) | Three places decide what a cancelled order leaves behind. | Fold into one `_resolve_cancel(o, reason)` (~-25 lines). |
| P2 | `polymarket_us_client.py` `close_position`, `get_all_bbos`; `_slippage_cents` | No callers (`close_position` is mocked by name only); one-line wrapper. | Delete. |
| P3 | `poly_us_trader.py` (`_daily_loss_halted` report block vs `_mp_checkpoint_payload`) | The halt dict is built twice. | Keep one. |
| P4 | `poly_us_trader.py` `price_for_maker_entry` docstring | Docstring predates the current semantics (it says "NO sell"; the order is a NO buy). | Fix the text. |
| P5 | `scripts/poly_ledger_audit.py` | Now that settlement writes its JSONL record, the audit can be run as a standing check. | Schedule it (or fold into the daily digest). |

## Infra / packaging

| # | Where | Issue | Fix |
|---|-------|-------|-----|
| I1 | `local.py` (594 lines) | Continuity/"self-model" persona, reached only behind a `try/except` at `trader.py:3253-3260`; nothing of it touches orders, risk or P&L. | Retire the module (and `mempalace_bridge`/`agent_bus` if nothing else uses them). |
| I2 | `pyproject.toml` vs `requirements.txt` | Two manifests disagree (9 entries only in requirements, `websockets` only in pyproject); `pytest` is a declared *runtime* dependency; `jsonschema` is imported by `events.py` behind a guarded import but pinned nowhere; no pins or lockfile. | Make `requirements.txt` authoritative, empty pyproject `dependencies`, move pytest/pyflakes/jsonschema to an optional `test` extra. |
| I3 | `scripts/` — 23 files, 2,537 lines | Tracked, never imported/tested/scheduled; `migrate_secrets.py` would rewrite the secrets layout if run. Still parsed by the three corruption gates every push. | `git rm`, or move under `legacy/` (which `verify_source`/`verify_compiles` skip). |
| I4 | `deploy.py` | Every deploy leaves its `/opt/superbot-releases/<sha>` behind forever. | Keep `current` + previous, rmtree the rest. |
| I5 | `events.validate_event` | Duplicated validation. | One implementation. |
| I6 | `emit.py` telemetry coalescing | Per-process, so it pairs with the (now-fixed) duplicate-outbox problem. | Revisit only if duplicate pages are seen again. |
| I7 | `pyarrow`, `huggingface_hub`, `polars`, `scipy` in requirements | Used only by dead HF tooling / research scripts. | Move to `requirements-research.txt` once I3 lands. |
| I8 | CI test step | ~a dozen tests silently skip (torch absent, non-systemd host, echo/env quirks) and nobody sees the count. | Run the step with `-q -rs` (or print `pytest | tail`) so skips are visible. |
| I9 | `tests/` | No coverage for the backtest CLIs (`backtest.py`, `backtest_real.py`, `parallel_backtest.py`) or the `check_*`/`probe_band` log-dump scripts. | Legacy-ize or delete the log-dump scripts; smoke-test the backtest CLIs if they stay. |

## Kalshi (lower-priority halves of reported findings)

| # | Where | Issue | Fix |
|---|-------|-------|-----|
| K1 | `trader.py` `_pre_trade_check` vs `risk.check` | Which market-daily-loss base wins after the two are unified. | Confirm the unified base against live config. |
| K2 | `signals.py` stability gate | Blocks all trades ~3 min on a fresh start. | By design; document if it ever surprises an operator. |
| K3 | `market_finder.py` `MAX_HEDGE_PER_SERIES=0` | Hedges off by default. | By design. |

## Verified on this pass, not yet fixed

| # | Where | Issue | Fix |
|---|-------|-------|-----|
| V1 | `scripts/deploy.py` `_rollback` | Re-points `current` and restarts, but the *new* release's unit file is already installed, so a rollback where the unit changed leaves the old release running under the new unit. | Save the previous unit text before the copy and restore it on rollback (~5 lines). |

## Verified and correct (no action)

- `scripts/deploy.py` `_rollback`: returns 3, re-points `current`, restarts, and is
  loud when there is no previous release. (Unit-file mismatch above is the one gap.)
- `backtest_engine.py`: entry price is the quoted mid (or ask for `final_taker`),
  markets with no quote for the entry minute are skipped, and markets are sorted
  by `close_time` before the balance walk. Both lookahead findings (rows 19, 20)
  hold.
- `inject_n8n_workflows.py`: workflows upsert by name (get-by-name → update/create),
  then activate; stale `SuperBot *` entries are pruned. Idempotent on re-run.
- `poly_us_trader.py` `_ledger_totals` / `_reconcile_ledger`: the gap invariant
  (`state_realized + pre_epoch == ledger_realized`) is computed and shouted on
  divergence; with settlement now writing a ledger record, the gap closes going
  forward. Historical rows will still show the old gap until the next `--reset`.

## Verified but not re-audited

These areas were named "not read" by the audit agents. A later pass should
sample them rather than assume:

- `poly_us_trader.py` internals: `evaluate_opportunity`, `_slug_static`, the
  calibration/MemPalace publish hooks; `poly_us_stream.py` WS protocol details.
- `backtest_engine.py` / `backtest.py` / `parallel_backtest.py` logic beyond the
  two already-fixed rows (lookahead entry price, time-ordered balance).
- `scripts/deploy.py` rollback path (`_rollback`) under a real failed restart.
- `n8n/workflows/*.json` idempotency — whether a webhook retry double-applies.
- `supabase/` migrations beyond `schema.sql` (only `schema.sql` was read).
