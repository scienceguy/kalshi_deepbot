# AGENT_TASKS v8 reconciliation — H3–H16 + review fixes

Status: **complete**. Every patch has a production change and a test.
Verified 2026-11-19 on the VPS (`pytest tests/ -q` → all pass; the three
source-corruption gates report `all clean`).

| # | Patch | Where | Test | Notes |
|---|-------|-------|------|-------|
| H3 | Optional `mempalace` | `mempalace_bridge.py` (guarded import, no-op fallback, `degraded_reason()`), `requirements.txt`, `pyproject.toml` | `tests/test_h3_h4_palace.py::test_import_trader_without_mempalace` | `import trader` works with the package blocked; bridge no-ops and reports `mempalace library not installed`. |
| H4 | One shared palace | `mempalace_bridge.py` (`MEMPALACE_DIR` → `MEMPALACE_PALACE_PATH` → `~/.mempalace`, `MEMPALACE_EXPECTED_DIR` mismatch), `local.py::_mp` forwards the CLI env, `trader.py::_report_cycle_state` adds `palace` to `degraded_sources`, runbook + AGENTS.md recipe | `tests/test_h3_h4_palace.py` (4 tests) | Two identities on one temp dir see each other's event; a mismatch is `degraded`; setgid/umask recipe documented. |
| H1 | Control lane | `control.py` (persist pause in local DB, `pause_entries` never expires, `resume_entries` clears), `n8n_webhook_auth.py` (`check_freshness=False` at drain), per-identity keys (J10) | `tests/test_h1_pause_persistence.py`, `tests/test_h1_poly_startup.py` | Restart keeps a pause; Poly startup waits for an explicit resume. |
| H2 | Gate key ≠ HMAC key | `n8n_webhook_auth.py` (signature checked before `check_and_mark`), `n8n_client.py` (gate key), loopback/HTTPS rule | `tests/test_h2_gate_key.py` | Bad signature consumes no nonce. |
| H5 | No Execute Command | `scripts/start_n8n.sh` (default exclusion kept), `n8n_startup_check()` | `tests/test_h5_no_execute_command.py` | No workflow JSON contains `n8n-nodes-base.executeCommand`. |
| H6 | Spot streams in the Poly process | `poly_us_trader.py` (`start_ws_streams`), `binance_feed.spot_with_age` | `tests/test_h6_h7_spot_bbo.py` | Guard reads stream spot with a max age; stale feed = `feed_stale`. |
| H7 | No network under the guard lock | `poly_us_trader.py` (snapshot → fetch → apply; cache-only guard tick; `guard_tick_ms` warning) | `tests/test_h6_h7_spot_bbo.py` | A blocking REST call cannot freeze a tick. |
| H8 | Event-driven decisions | `docs/LATENCY_BASELINE.md` (measured p50/p95/p99 per stage from `latency_measurements`) | `tests/test_h8_h9_trader_wiring.py` | Table checked in. Guard wake-ups deliberately **not** added: the Polymarket process does not yet record `guard_tick_ms`, so the "after measuring" condition is unmet and the 1 s timer stays. |
| H9 | Write-ahead order intent | `poly_us_trader.py` (`pending_send` intent recorded before the REST call, confirmed by venue order id, reconciled by `(ticker, side)` at startup) | `tests/test_h9_order_intent.py` (`tests/test_h8_h9_trader_wiring.py`) | Crash after send leaves a resolvable intent, not an untracked live order. The SDK has no client-order-id field, so the intent id is local and reconciliation matches on ticker+side. |
| H10 | Risk functions with callers | `trader.py` / `poly_us_trader.py` wire `kelly_size` (also MAKER fee path), exposure and liquidity checks | `tests/test_h10_risk_wiring.py` | The wiring test fails for any public risk method without a non-test caller. |
| H11 | Durable inbox | `bus_startup.py` (durable `bus_inbox` table + logstream sweep), `scripts/bus_inbox.py` (`list` / `list --table` / `ack`), `agent_bus.py::BusReceiver` persists before acking | `tests/test_h11_bus_inbox.py`, `tests/test_h11_bus_wiring.py`, `tests/test_h11_bus_e2e.py` | `scripts/bus_inbox.py list` shows open items; `ack <id>` removes them. |
| H12 | Bus replay protection | `agent_bus.py` (shared `NonceStore`, `message_id` required) | `tests/test_h12_bus_replay.py` | Replays and envelopes without `message_id` are rejected. |
| H13 | Kalshi units on `current` | `deploy/superbot*.service`, `scripts/deploy.py` per-unit restart/rollback | `tests/test_h13_release_path.py`, `tests/test_v2_units.py` | Every unit's `WorkingDirectory` is under the releases root. |
| H14 | Operator-owned live confirm | `deploy/superbot.service` has no `CONFIRM_LIVE`; runbook documents the drop-in | `tests/test_live_gate.py`, `tests/test_v2_units.py` | `--live` without the drop-in refuses to place orders. |
| H15 | OCI SSH + Termux leftovers | `scripts/oci_launch.py` (`--ssh-allow-cidr`, `--allow-world-ssh`), `scripts/n8n_creds_guard.sh` (VPS paths) | `tests/test_h15_ops.py` | Ingress is TCP/22 + the requested CIDR only; no `com.termux` paths remain. |
| H16 | Environment-dependent tests | `test_v2_units.py` rewrites `ExecStart` to an existing binary; Supabase warning made lazy; subprocess env pinned | `tests/test_v2_units.py`, `tests/test_b16_import_side_effects.py` | Suite passes with no `/opt/superbot` and no Supabase variables. |

## Review follow-ups (second pass)

| Item | Change |
|------|--------|
| Durable bus inbox wiring | `bus_startup.py` drains the durable inbox at startup and sweeps the logstream; watcher armed per event. |
| Poly persisted-pause reload | `poly_us_trader.py` reloads `control_state` at startup and runs no cycles until a signed `resume_entries`. |
| Local DB retention | `local_db.py` prunes control tables (7–30 days), nightly maintenance job. |
| Isolated e2e smoke | `scripts/e2e_smoke.py` force-sets `SUPERBOT_DATA_DIR` / `SUPERBOT_LOCAL_DB_PATH` and refuses to write outside its temp root. |
| Loud pause-persist failures | `control._persist_pause` returns False on any write error, logs at ERROR ("memory and DB now disagree"), and the command result records `persisted: false` — a restart never silently un-pauses. |
| Dead-code cleanup | Unused locals removed (`cancelled_any` ×2, `now`), symmetry comments added. |

## Inferred claims that turned out wrong / unverified

- H9 assumed the venue accepts a client order id: the Polymarket US SDK does,
  and the intent id is used to reconcile `pending_send` records.
- H3's suggested pin `mempalace>=0.1.0` matches the installed 0.11 line on the
  VPS; the installed CLI reads `MEMPALACE_PALACE_PATH`, not `MEMPALACE_DIR`, so
  both are accepted and the CLI is passed the mapped value.
- Not measured: latency figures in `docs/LATENCY_BASELINE.md` are from paper
  trading, not a live venue.
