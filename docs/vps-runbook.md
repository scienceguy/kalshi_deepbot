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
| `superbot-dry.service` (deploy/ ref) | `trader.py --dry --interval 15` | `ubuntu` | `/opt/superbot` | `local_data/`, `/home/ubuntu/.mempalace`, `/home/ubuntu/.config/mempalace`, `-/opt/superbot/.lock_kalshi` | `network-online.target`, `n8n.service` | `on-failure`, burst 5 / 300s |
| `superbot.service` (deploy/ ref) | `trader.py --live --interval 15` | `ubuntu` | `/opt/superbot` | same as dry twin | `network-online.target`, `n8n.service` | `on-failure`, burst 5 / 300s |
| `n8n.service` | `n8n start` | `ubuntu` | `/home/ubuntu` | `~/.n8n` (its own DB) | `network.target` | `always` |
| `poly15m-paper.service`, `poly5m-collector.service` | legacy collectors | `ubuntu` | `/opt/superbot` | repo tree | `network-online.target` | `always` |

Notes:

- `superbot.service` is the **live** kalshi unit — reference only in `deploy/`,
  not installed by any deploy path. Real money: `DRY_RUN=false` +
  `KALSHI_CONFIRM_LIVE=true` + `--live`. Never start it as part of testing.
- The hardened `superbot-dry.service`/`superbot.service` in `deploy/` are
  **not yet installed**; `/etc/systemd/system/superbot-dry.service` is still
  the older unhardened copy. Install with the commands in "Unit / watchdog".
- The MemPalace bridge is **in-process** (M1), not a sidecar: each trader
  owns its bridge thread and writes its user's palace dir (listed above).
  The two palace DBs on this host are expected to differ (CLI
  `~/.config/mempalace/palace` vs bridge `~/.mempalace`) — see the P8 report.
- `polybot`'s palace `/opt/superbot/.mempalace` is dash-prefixed: the unit
  starts without it, but create it once for heartbeat parity —
  `sudo install -d -o polybot -g polybot /opt/superbot/.mempalace`.
- `MemoryMax=1G` on both traders is measured, not guessed: dry kalshi trader
  RSS ≈ 306 MiB, collectors ≈ 36 MiB, n8n ≈ 324 MiB (2026-11-17).
  `LimitNOFILE=65536` after the bridge hit `Errno 24` at the 1024 default.
- `TMPDIR` is service-owned (`/opt/superbot/local_data`); `PrivateTmp=true`
  keeps agent scratch (`/tmp/opencode`) out of the service namespace.

## n8n

- Unit: `n8n.service` (User `ubuntu`, `NODES_EXCLUDE=[]` is required —
  n8n 2.x disables `executeCommand` by default and the backtest/collect
  webhooks fail without it; start via `sh scripts/start_n8n.sh`).
- **Bind address: `*:5678` (all interfaces)**, not loopback — `ss -tlnp`
  shows `*:5678`. The REST API and webhooks are reachable from the network
  unless a firewall restricts them; the N1 installer's webhook secret is the
  only auth on the trade lane. Restrict exposure at the firewall if the host
  is not trusted.
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

## Modes

Paper by default. Live requires **both** `POLY_CONFIRM_LIVE=true` in `.env` and
`--live` on the command line; without the env var the engine exits 2.
