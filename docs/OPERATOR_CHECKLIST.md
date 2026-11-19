# Operator Verification Checklist — SuperBot Pre-Live

**Purpose**: Complete verification before enabling live trading (`KALSHI_CONFIRM_LIVE=true` / `POLY_CONFIRM_LIVE=true`).

**Run this checklist on the VPS after every deploy or config change.**

---

## 1. Pre-Flight Checks (Environment, Secrets, Configs)

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 1.1 | `cat /opt/superbot-releases/current/.env \| grep -E "^(KALSHI_KEY_ID|KALSHI_KEY_FILE|SUPABASE_URL|N8N_API_KEY)="` | All 4 vars set, non-empty | All present | Fix .env |
| 1.2 | `ls -la /home/ubuntu/.secrets/kalshi_private.pem` | `-rw------- 1 ubuntu ubuntu ...` | 0600 perms | `chmod 600` |
| 1.3 | `ls -la /home/ubuntu/.secrets/n8n_api_key` | `-rw-------` | 0600 perms | `chmod 600` |
| 1.4 | `ls -la /home/ubuntu/.secrets/supabase_service_key` | `-rw-------` | 0600 perms | `chmod 600` |
| 1.5 | `ls -la /home/ubuntu/.secrets/bw/` | Directory with `index.json` + JSON files | Exists, 0700 | Recreate from BW |
| 1.6 | `grep -r "SUPERBOT_WEBHOOK_SECRET" /opt/superbot-releases/current/.env` | Set (or per-identity vars) | Not empty | Set in .env |
| 1.7 | `grep "SUPERBOT_GATE_KEY" /opt/superbot-releases/current/.env` | Set | Not empty | Set in .env |
| 1.8 | `grep "SUPERBOT_REQUIRE_GATE_KEY" /opt/superbot-releases/current/.env` | `true` | Exactly `true` | Set in .env |
| 1.9 | `grep "KALSHI_CONFIRM_LIVE" /opt/superbot-releases/current/.env` | `false` (dry) or `true` (live) | Must be `true` for live | N/A |
| 1.10 | `grep "POLY_CONFIRM_LIVE" /opt/superbot-releases/current/.env` | `false` (dry) or `true` (live) | Must be `true` for live | N/A |

---

## 2. n8n Workflow Health

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 2.1 | `curl -sf http://127.0.0.1:5678/healthz` | `{"status":"ok"}` | HTTP 200, JSON ok | `systemctl restart n8n` |
| 2.2 | `curl -sf -X POST http://127.0.0.1:5678/webhook/superbot-trade -H "Content-Type: application/json" -d '{"action":"health_check"}'` | `{"ok":true}` | HTTP 200 | Check n8n logs |
| 2.3 | `curl -sf -X POST http://127.0.0.1:5678/webhook/superbot-collect -H "Content-Type: application/json" -d '{"action":"health_check"}'` | `{"ok":true}` | HTTP 200 | Check n8n logs |
| 2.4 | `curl -sf -X POST http://127.0.0.1:5678/webhook/superbot-backtest -H "Content-Type: application/json" -d '{"action":"health_check"}'` | `{"ok":true}` | HTTP 200 | Check n8n logs |
| 2.5 | `curl -sf -X POST http://127.0.0.1:5678/webhook/superbot-monitor -H "Content-Type: application/json" -d '{"action":"health_check"}'` | `{"ok":true}` | HTTP 200 | Check n8n logs |

---

## 3. Database Connectivity

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 3.1 | `python3 -c "from supabase_client import validate_at_startup; validate_at_startup(); print('OK')"` | `OK` | No exception | Check creds |
| 3.2 | `sqlite3 /opt/superbot-releases/current/local_data/state.db ".tables"` | Tables listed | No error | Check perms |
| 3.3 | `sqlite3 /opt/superbot-releases/current/local_data/state.db "PRAGMA journal_mode"` | `wal` | WAL mode | N/A |

---

## 4. Credential Sync Verification

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 4.1 | `cd /opt/superbot-releases/current && python3 scripts/n8n_creds_sync.py --check` | `All credentials present` | Exit 0, no missing | Run sync |
| 4.2 | `cd /opt/superbot-releases/current && python3 scripts/n8n_creds_sync.py --list` | Lists Kalshi, Polymarket, Supabase, etc. | All expected creds shown | N/A |

---

## 5. Dry-Run Cycle Execution

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 5.1 | `cd /opt/superbot-releases/current && python3 trader.py --once --dry 2>&1 | tail -30` | Cycle completes, no ERROR | Exit 0, no traceback | Check logs |
| 5.2 | `cd /opt/superbot-releases/current && python3 poly_us_trader.py --once --dry 2>&1 | tail -30` | Cycle completes, no ERROR | Exit 0, no traceback | Check logs |

---

## 6. E2E Smoke Test (7 Pillars)

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 6.1 | `cd /opt/superbot-releases/current && python3 scripts/e2e_smoke.py 2>&1 | tail -20` | `ALL 7 PILLARS PASSED` | Exit 0, all green | Check failures |

---

## 7. Test Suite

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 7.1 | `cd /opt/superbot-releases/current && python3 -m pytest tests/ -q 2>&1 | tail -5` | `1312 passed, 10 skipped` | All pass | Fix failures |

---

## 8. Corruption Gates (6 Gates)

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 8.1 | `cd /opt/superbot-releases/current && python3 scripts/verify_source.py --all` | `all clean` | All clean | Fix files |
| 8.2 | `cd /opt/superbot-releases/current && python3 scripts/verify_compiles.py --all` | `all clean` | All clean | Fix syntax |
| 8.3 | `cd /opt/superbot-releases/current && python3 scripts/verify_undefined.py --all` | `all clean` | All clean | Fix names |
| 8.4 | `cd /opt/superbot-releases/current && python3 scripts/verify_review_marked.py --all` | `all clean` | All clean | Add markers |
| 8.5 | `cd /opt/superbot-releases/current && python3 scripts/verify_operators.py` | `all clean` | All clean | Fix operators |

---

## 9. Control Lane Test

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 9.1 | `cd /opt/superbot-releases/current && python3 scripts/send_command.py --command pause_entries --by operator --reason "test" 2>&1` | `accepted` | Exit 0 | Check keys |
| 9.2 | Check local DB: `sqlite3 /opt/superbot-releases/current/local_data/state.db "SELECT * FROM control_inbox ORDER BY id DESC LIMIT 1"` | Row with pause_entries | Row exists | N/A |
| 9.3 | `cd /opt/superbot-releases/current && python3 scripts/send_command.py --command resume_entries --by operator --reason "test" 2>&1` | `accepted` (requires second identity) | Exit 0 | Check keys |

---

## 10. Job Bridge Health

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 10.1 | `curl -sf http://127.0.0.1:18711/healthz` | `{"ok":true,"service":"job_bridge"}` | HTTP 200 | Check service |
| 10.2 | `curl -sf -X POST http://127.0.0.1:18711/backtest -H "X-Superbot-Gate-Key: $SUPERBOT_GATE_KEY" -H "Content-Type: application/json" -d '{"action":"health_check"}'` | `{"ok":true}` | HTTP 200 | Check gate key |
| 10.3 | `curl -sf -X POST http://127.0.0.1:18711/collect -H "X-Superbot-Gate-Key: $SUPERBOT_GATE_KEY" -H "Content-Type: application/json" -d '{"action":"health_check"}'` | `{"ok":true}` | HTTP 200 | Check gate key |

---

## 11. Settlement Reconciliation Dry-Run

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 11.1 | `cd /opt/superbot-releases/current && python3 -c "from pnl_reconcile import maybe_reconcile_session_pnl; from trader import get_state; from kalshi_client import KalshiClient; c = KalshiClient(); s = get_state(); maybe_reconcile_session_pnl(c, s); print('OK')"` | `OK` | No exception | Check logs |

---

## 12. Risk Limits Validation

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 12.1 | `grep "CASH_FLOOR_PCT\|EXPOSURE_CEILING_PCT" /opt/superbot-releases/current/.env` | Sum ≤ 1.0 | Sum ≤ 1.0 | Adjust .env |
| 12.2 | `grep "DAILY_LOSS_LIMIT_PCT" /opt/superbot-releases/current/.env` | Set, 0 < x ≤ 1 | Valid range | Adjust .env |
| 12.3 | `grep "MAX_CONTRACTS_CAP" /opt/superbot-releases/current/.env` | Set, positive | Valid | Adjust .env |

---

## 13. Log File Permissions and Rotation

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 13.1 | `ls -la /opt/superbot-releases/current/local_data/*.log 2>/dev/null \| head -5` | Files exist, 0644 | Rotated, perms OK | Fix perms |
| 13.2 | `grep "LOG_MAX_BYTES\|LOG_BACKUP_COUNT" /opt/superbot-releases/current/.env` | Set (e.g. 10MB, 10) | Rotation configured | Set in .env |

---

## 14. Systemd Service Status

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 14.1 | `systemctl is-enabled superbot-bus-inbox.service` | `enabled` | Enabled | `systemctl enable` |
| 14.2 | `systemctl is-active superbot-bus-inbox.service` | `active` | Running | `systemctl start` |
| 14.3 | `systemctl is-enabled superbot-job-bridge.service` | `enabled` | Enabled | `systemctl enable` |
| 14.4 | `systemctl is-active superbot-job-bridge.service` | `active` | Running | `systemctl start` |
| 14.5 | `systemctl is-enabled superbot-jobs.timer` | `enabled` | Enabled | `systemctl enable` |
| 14.6 | `systemctl is-active superbot-jobs.timer` | `active` | Running | `systemctl start` |
| 14.7 | `systemctl is-enabled superbot-creds-guard.timer` | `enabled` | Enabled | `systemctl enable` |
| 14.8 | `systemctl is-active superbot-creds-guard.timer` | `active` | Running | `systemctl start` |
| 14.9 | `systemctl is-enabled superbot-retrain.timer` | `enabled` | Enabled | `systemctl enable` |
| 14.10 | `systemctl is-active superbot-retrain.timer` | `active` | Running | `systemctl start` |
| 14.11 | If dry: `systemctl is-active superbot-dry.service` | `active` | Running | `systemctl start` |
| 14.12 | If live: `systemctl is-active superbot.service` | `active` | Running | `systemctl start` |
| 14.13 | If poly dry: `systemctl is-active poly-us-dry.service` | `active` | Running | `systemctl start` |

---

## 15. Gateway Key Rotation

| Step | Command | Expected Output | Pass Criteria | Rollback |
|------|---------|-----------------|---------------|----------|
| 15.1 | Verify Kalshi API key matches `~/.secrets/kalshi_private.pem` | `KALSHI_KEY_ID` matches cert | Match | Regenerate |
| 15.2 | Verify n8n webhook secret rotated in last 90 days | Check BW timestamp | < 90 days old | Rotate in BW |
| 15.3 | Verify job bridge gate key rotated in last 90 days | Check BW timestamp | < 90 days old | Rotate in BW |

---

## Sign-Off

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Operator | | | |
| Reviewer | | | |

---

**Note**: This checklist must be completed in full before setting `KALSHI_CONFIRM_LIVE=true` or `POLY_CONFIRM_LIVE=true`. Any FAIL requires remediation and re-verification of the failed step.