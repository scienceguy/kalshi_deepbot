# VPS runbook — Polymarket US paper engine

Canonical runtime. The Termux/Android checkout is development only; never run a
trader loop there (it would share `local_data/` names with the VPS but not the
state).

| Item | Value |
|---|---|
| Host | `<vps-host>` (SSH user `<vps-user>`, key `<ssh-key>`) |
| Code | `/opt/superbot` (git checkout of `origin/main`) |
| Interpreter | `/opt/superbot/.venv/bin/python` |
| Unit | `poly-us-dry.service` (`deploy/poly-us-dry.service`) |
| State | `/opt/superbot/local_data/poly_us_state.json` |
| Ledger | `/opt/superbot/local_data/poly_us_paper_ledger.jsonl` |
| Logs | `/opt/superbot/local_data/poly_us_dry.log` + journald |
| Lock | `/opt/superbot/.lock_poly` (one instance only) |

## Services on this host (V2)

Every process the bot depends on, its user, writable paths and dependencies.
`deploy/` holds the hardened source of truth; `/etc/systemd/system/` holds what
is actually installed — reinstall the unit after editing (see below).

| Unit | Process | User | WorkingDirectory | ReadWritePaths (ProtectSystem=strict) | After=/Wants= | Restart |
|---|---|---|---|---|---|---|
| `poly-us-dry.service` | `poly_us_trader.py --interval 15` | `polybot` | `/opt/superbot-releases/current` (V1 release symlink) | `local_data/`, `-/opt/superbot/.lock_poly`, `-/opt/superbot/.mempalace` | `network-online.target`, `n8n.service` | `on-failure`, burst 5 / 300s |
| `superbot-dry.service` (deploy/ ref) | `trader.py --dry --interval 15` | `ubuntu` | `/opt/superbot-releases/current` | `local_data/`, `/home/ubuntu/.mempalace`, `/home/ubuntu/.config/mempalace`, `-/opt/superbot/.lock_kalshi` | `network-online.target`, `n8n.service` | `on-failure`, burst 5 / 300s |
| `superbot.service` (deploy/ ref) | `trader.py --live --interval 15` | `ubuntu` | `/opt/superbot-releases/current` | same as dry twin | `network-online.target`, `n8n.service` | `on-failure`, burst 5 / 300s |
| `n8n.service` | `n8n start` | `ubuntu` | `/home/ubuntu` | `~/.n8n` (its own DB) | `network.target` | `always` |
| `poly15m-paper.service`, `poly5m-collector.service` | legacy collectors | `ubuntu` | `/opt/superbot` | repo tree | `network-online.target` | `always` |
| `superbot-jobs.timer` → `superbot-jobs.service` | `scripts/scheduled_jobs.py` (digest, watchdogs, sync lag) | `ubuntu` | `/opt/superbot` | `local_data/` | `network-online.target` | `oneshot`, ticks 1/min |
| `superbot-job-bridge.service` | `scripts/job_bridge.py` (loopback only) | `ubuntu` | `/opt/superbot` | `local_data/` | `network-online.target` | `on-failure`, burst 5 / 300s |
| `superbot-creds-guard.timer` → `superbot-creds-guard.service` | `scripts/n8n_creds_guard.sh` | `ubuntu` | `/opt/superbot` | `local_data/` | `network-online.target`, `n8n.service` | `oneshot`, every 15 min |
| `superbot-retrain.timer` → `superbot-retrain.service` | `scripts/retrain_ml.sh` (paper artifact) | `ubuntu` | `/opt/superbot` | `local_data/`, `data/` | `network-online.target` | `oneshot`, every 6 h |
| `superbot-bus-inbox.service` | `python -m agent_bus` (H11 bus inbox receiver) | `ubuntu` (group `superbot`) | `/opt/superbot` | `local_data/` | `network-online.target` | `on-failure`, burst 5 / 300s |

Notes:

- `superbot.service` is the **live** kalshi unit — reference only in `deploy/`,
  not installed by any deploy path. Real money: `DRY_RUN=false` + `--live`,
  and the second key is a **drop-in the operator owns** (H14) — the repo unit
  deliberately does not contain `KALSHI_CONFIRM_LIVE`:
  ```
  sudo install -d -m 755 /etc/systemd/system/superbot.service.d
  printf '[Service]\nEnvironment=KALSHI_CONFIRM_LIVE=true\n' | \
      sudo tee /etc/systemd/system/superbot.service.d/live-confirm.conf
  sudo chmod 644 /etc/systemd/system/superbot.service.d/live-confirm.conf
  sudo systemctl daemon-reload
  ```
  The same pattern applies to `POLY_CONFIRM_LIVE=true` for a Polymarket live
  unit. Without the drop-in the trader logs an ERROR and refuses live orders.
  Never start this unit as part of testing.
- The hardened `superbot-dry.service`/`superbot.service` in `deploy/` are
  **not yet installed**; `/etc/systemd/system/superbot-dry.service` is still
  the older unhardened copy. Install with the commands in "Unit / watchdog".
- The MemPalace bridge is **in-process** (M1), not a sidecar: each trader
  owns its bridge thread. H4: every identity must resolve the **same** palace
  directory — set `Environment=MEMPALACE_DIR=/var/lib/superbot/mempalace`
  (and `UMask=0002`) in every trader unit and add that path to each unit's
  `ReadWritePaths`. One-time setup:
  ```
  sudo groupadd -f superbot
  sudo usermod -aG superbot ubuntu
  sudo usermod -aG superbot polybot
  sudo install -d -o root -g superbot -m 2775 /var/lib/superbot/mempalace
  ```
  The setgid bit (2775) plus `UMask=0002` keeps SQLite's `-wal`/`-shm` files
  writable by both identities. Each process logs the resolved directory at
  startup; set `MEMPALACE_EXPECTED_DIR` to the coding agent's palace to have a
  mismatch reported as `degraded` (state report + `degraded_sources: palace`).
- `MEMPALACE_DIR` is also forwarded to the `mempalace` CLI as
  `MEMPALACE_PALACE_PATH` (`local.py::_mp`), so the CLI-based helpers read the
  same palace as the bridge.
- `MemoryMax=1G` on both traders is measured, not guessed: dry kalshi trader
  RSS ≈ 306 MiB, collectors ≈ 36 MiB, n8n ≈ 324 MiB (2026-11-17).
  `LimitNOFILE=65536` after the bridge hit `Errno 24` at the 1024 default.
- `TMPDIR` is service-owned (`/opt/superbot/local_data`); `PrivateTmp=true`
  keeps agent scratch (`/tmp/opencode`) out of the service namespace.
- `superbot-bus-inbox.service` is **not installed** by `scripts/deploy.py`;
  install it by hand (see "Agent bus inbox, retention and pause persistence").

## SSH exposure (H15)

The OCI launcher opens **only TCP/22**, and only from the CIDR passed as
`--ssh-allow-cidr` (or `$OCI_SSH_ALLOW_CIDR`); `--allow-world-ssh` is the
explicit opt-out and prints a warning. Examples:

```
python3 scripts/oci_launch.py --ssh-allow-cidr 198.51.100.7/32   # your address
python3 scripts/oci_launch.py --status                           # inspect only
```

The launcher also replaces an older world-open TCP/22 rule in the security
list it manages. On the host, keep key-only auth (`PasswordAuthentication no`,
`PermitRootLogin no`) and run fail2ban on `sshd`; n8n and the job bridge are
never exposed (bridge is loopback, n8n is firewalled).

## n8n

- Unit: `n8n.service` (User `ubuntu`). Do **not** set `NODES_EXCLUDE='[]'`:
  n8n must not have shell access (H5). Scripts run through
  `superbot-job-bridge.service` (`scripts/job_bridge.py`, loopback only) and
  scheduled jobs through `superbot-jobs.timer`.
- **Bind address: `*:5678` (all interfaces)**, not loopback — `ss -tlnp`
  shows `*:5678`. The REST API and webhooks are reachable from the network
  unless a firewall restricts them; the N1 installer's webhook secret is the
  only auth on the trade lane. Restrict exposure at the firewall if the host
  is not trusted.
- H5 hardening still to apply on the VPS: run `n8n.service` as a dedicated
  unprivileged user that cannot read `/opt/superbot/.env`, the local trading DB
  or `~/.secrets/`, bind it to `127.0.0.1`, and require owner authentication on
  the editor. With the Execute Command node gone, editor access no longer
  implies shell, but the credentials are still readable by the `ubuntu` user.
- The n8n REST API returns 401 (upstream bug #31011); webhooks + direct DB
  injection are the working paths.

## Redeploy

Real host/user/key values are kept in private notes, never in this repository.

Deploys go through the release mechanism (V1) — the service runs from
`/opt/superbot-releases/<sha>` via the `current` symlink, never from the
shared dev tree:

```bash
ssh -i <ssh-key> <vps-user>@<vps-host>
cd /opt/superbot
git fetch origin main && git pull --ff-only origin main
scripts/deploy.sh <sha>          # lease + worktree + verify + smoke + swap +
                                 # unit install + restart + heartbeat watch +
                                 # automatic rollback on heartbeat miss
```

`deploy.sh` stages the unit itself (copies `deploy/<unit>` into
`/etc/systemd/system/`, daemon-reloads, restarts, waits for the
`reported_state:poly_us` heartbeat and rolls back on failure). The first
hardened deploy is also what installs the unit update from this change.

Ordering matters for the Kalshi units: `/opt/superbot-releases/current` must
exist before the first `superbot.service` / `superbot-dry.service` start. The
symlink is created by `scripts/deploy.py`, which also symlinks `.env` and
`local_data` into each release; without it, systemd fails with a chdir error
because the working directory — and therefore the relative `trader.py` in
`ExecStart=... trader.py` — does not resolve.

Manual dev-loop checks still apply:

```bash
.venv/bin/python -m pytest tests/ -q                  # full suite
.venv/bin/python -m pytest tests/test_poly_us_trader.py -q 2>&1 | tail -1
```

## Verify (all four must hold)

```bash
systemctl is-active poly-us-dry.service      # active
systemctl is-enabled poly-us-dry.service     # enabled
systemctl show -p MainPID --value poly-us-dry.service
ls -l /opt/superbot/.lock_poly               # exists, held
journalctl -u poly-us-dry.service -n 30 --no-pager   # a cycle, no Traceback
```

A `Type=notify` unit is only `active` after the engine sends `READY=1`; if it
sits in `activating`, init failed — read journald.

If the worktree has local edits, do **not** `git checkout -- .`
(see AGENTS.md). Back them up first:
`git diff > ~/backups/uncommitted-$(date +%s).patch && git stash push -m "<why>"`.

## Rollback

For the release-deployed poly unit, rollback is automatic on heartbeat miss;
manual rollback is:

```bash
ln -sfn /opt/superbot-releases/<previous-good-sha> /opt/superbot-releases/current.tmp
mv -T /opt/superbot-releases/current.tmp /opt/superbot-releases/current   # atomic
sudo systemctl restart poly-us-dry.service
```

For the dev tree (anything not release-deployed):

```bash
cd /opt/superbot
git log --oneline -5                      # pick the previous good SHA
git checkout <sha>
sudo systemctl restart <unit>
```

## Unit / watchdog

The unit runs `Type=notify` with `WatchdogSec=120`. The engine pings
`WATCHDOG=1` after every cycle (~15–20 s) **and** from its sleep helper at least
every 30 s while backing off after failures (A10), so a long venue-outage
backoff no longer gets killed and restarted mid-sleep; a genuinely wedged cycle
still trips the watchdog.

The service runs as the dedicated unprivileged user `polybot` with
`NoNewPrivileges=true`, `ProtectSystem=strict`, writes confined to
`local_data/` and `.lock_poly`, `PrivateTmp=true` and `MemoryMax=1G`. Create the
account and restricted `.env` once:

```bash
sudo useradd --system --home /opt/superbot --shell /usr/sbin/nologin polybot
sudo chown -R polybot:polybot /opt/superbot/local_data
sudo install -m 600 -o polybot -g polybot /opt/superbot/.env /opt/superbot/.env
sudo install -d -o polybot -g polybot /opt/superbot/.mempalace   # V2: bridge palace
```

Reinstall after editing:

```bash
sudo cp /opt/superbot/deploy/poly-us-dry.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart poly-us-dry.service
```

The kalshi hardened units in `deploy/` (`superbot-dry.service`, and the
real-money `superbot.service` reference) follow the same hardening and are
installed the same way when the operator chooses; nothing in this repo
installs or restarts them automatically.

## Graceful shutdown contract (V2)

`TimeoutStopSec=90` on both trader units covers, in order:

1. in-flight cycle finishes (SIGTERM sets a stop flag; the loop exits after it);
2. **live mode only**: every venue resting order is cancelled
   (`cancel_all_resting_orders` poly / the live-gated block in `trader.py`) —
   an unmanned maker order is naked exposure;
3. deferred ledger/DB writes flushed (`flush_ledger`, ≤5 s budget);
4. MemPalace session end + bridge stop (≤5 s each), DB close, log-listener stop.

A shutdown test (`tests/test_v2_units.py`) drives the poly path with faked
venue calls: resting orders are cancelled and the ledger outbox is flushed,
all inside the budget. systemd SIGKILLs only if the process ignores SIGTERM
past `TimeoutStopSec`.

If SSH exposure is a concern, restrict it to key-only auth, a firewall
allowlist, and rotate the key.

## Log rotation

```bash
sudo cp /opt/superbot/deploy/superbot.logrotate /etc/logrotate.d/superbot
sudo logrotate -d /etc/logrotate.d/superbot     # dry run
```

`copytruncate` is deliberate: the engine keeps its log/ledger files open.

## State backup

```bash
chmod +x /opt/superbot/deploy/state-backup.sh
crontab -e   # add: 15 3 * * * /opt/superbot/deploy/state-backup.sh
```

Snapshots land in `~/backups/state/` (7 kept). The script also takes
WAL-safe SQLite online backups of every `local_data/superbot_local*.db`
into `~/backups/state/sqlite/` (7 kept) — `cp` of a WAL-mode DB's main
file is not a consistent snapshot, so don't.

In addition, each trader runs ~nightly from its cycle loop
(`maybe_nightly_maintenance()` in `local_db.py`): `PRAGMA integrity_check`,
an online backup into `local_data/backups/sqlite/` (7 kept), and pruning.
A corrupt DB is paged as a `db.integrity_failed` critical event and is
restored from the newest integrity-verified backup at the next startup; if
no good backup exists the DB is renamed `.corrupt.<ts>` and recreated empty
(and the operator gets paged either way).

Off-box copy of the backups is the operator's choice (e.g. `rclone copy`
to OCI Object Storage with a scoped credential on the same cron, or the
bucket lifecycle rule for retention). It is deliberately not wired by the
bot — no cloud credentials in the app's path.

## Restore drill (SQLite DBs)

Exercise this quarterly and after any corruption page. Everything below is
safe on paper/dry-run instances; on a live instance do it with the trader
stopped.

1. Stop the trader. Snapshot the current state for post-mortem:
   `cp -a local_data /tmp/pre-restore-local_data`.
2. List backups: `ls -t local_data/backups/sqlite/ superbot_local*.db.*.bak`.
3. Verify a candidate:
   `.venv/bin/python -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" local_data/backups/sqlite/superbot_local_live.db.<stamp>.bak`
   — must print `ok`.
4. Restore: stop the trader, then start it again. The startup integrity
   gate checks the DB and swaps in the newest verified backup automatically
   (you will see `restored ... from backup ...` in the log and get a
   `db.integrity_failed` critical page). Manual equivalent:
   `cp local_data/backups/sqlite/<file>.bak local_data/superbot_local_live.db`
   plus `rm -f local_data/superbot_local_live.db-wal local_data/superbot_local_live.db-shm`.
5. Verify row counts look sane (e.g. `orders` not empty if you traded
   recently) and `PRAGMA integrity_check` prints `ok`.
6. Restart the trader and watch one full cycle in the log.

The automated path above is exercised in `tests/test_d4_backup_restore.py`
against temp dirs.

## Agent bus inbox, retention and pause persistence (H11 / H1)

### superbot-bus-inbox.service (H11)

New unit `deploy/superbot-bus-inbox.service`; **not installed by
`scripts/deploy.py`**. It runs `/opt/superbot/.venv/bin/python -m agent_bus` as
`ubuntu` (group `superbot`), `ProtectSystem=strict` with only
`/opt/superbot/local_data` writable, `TMPDIR=/opt/superbot/local_data`,
`Restart=on-failure` (burst 5 / 300 s). Every verified bus envelope is
persisted to the `bus_inbox` table in the local DB, so messages survive the
coding agent being down:

```bash
sudo install -m 644 deploy/superbot-bus-inbox.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now superbot-bus-inbox.service
```

### scripts/bus_inbox.py (H11)

CLI over the `bus_inbox` table (from the repo root):

```bash
.venv/bin/python scripts/bus_inbox.py list           # pending messages as JSON
.venv/bin/python scripts/bus_inbox.py list --table   # tabular
.venv/bin/python scripts/bus_inbox.py ack <id>       # mark one delivered
.venv/bin/python scripts/bus_inbox.py drain          # print all pending, then mark delivered
```

`ack` prints `{"ok": ..., "id": ...}` and exits 1 when the id does not exist;
acking an already-delivered row still exits 0.

### Retention: prune_control_tables (H8)

`LocalDB.prune_control_tables()` (defaults `latency_keep_days=7`,
`pending_keep_days=7`) deletes `latency_measurements` rows older than 7 days
and terminal `pending_send` rows (`confirmed` / `cancelled` / `failed`) older
than 7 days. `status='pending'` rows are live intents and are never deleted.

It runs at most once an hour per process (`_PRUNE_EVERY_S = 3600`) and is
triggered from `record_latency` — a completely idle trader that records no
latency measurements never prunes. Only `trader.py` calls `record_latency`;
`poly_us_trader.py` does not, so the Poly path never triggers the prune.

### Pause persistence (H1)

A signed `pause_entries` command is persisted to the local DB
(`control_state`) and reloaded at startup by both traders — `poly_us_trader.py`
via `_load_persisted_pause()`, `trader.py` via an inline equivalent — so a
restart no longer un-pauses the bot.

If the persist write fails, `control.py` logs at ERROR and the command result
carries `persisted: false`. An operator seeing that must fix the DB and
re-issue the pause: the pause is then only in memory and a restart resumes
trading. Read-only check of what survived a restart:

```bash
.venv/bin/python -c "from local_db import get_local_db; print(get_local_db().get_control_state('pause_entries'))"
```

## Modes

Paper by default. Live requires **both** `POLY_CONFIRM_LIVE=true` in `.env` and
`--live` on the command line; without the env var the engine exits 2.
