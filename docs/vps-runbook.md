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

## Redeploy

Real host/user/key values are kept in private notes, never in this repository.

```bash
ssh -i <ssh-key> <vps-user>@<vps-host>
cd /opt/superbot
git fetch origin main && git pull --ff-only origin main
.venv/bin/python -m pip install -r requirements.txt      # only if deps changed
.venv/bin/python -m pytest tests/test_poly_us_trader.py tests/test_polymarket_us_client.py -q
sudo systemctl restart poly-us-dry.service
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

```bash
cd /opt/superbot
git log --oneline -5                      # pick the previous good SHA
git checkout <sha>
sudo systemctl restart poly-us-dry.service
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
```

Reinstall after editing:

```bash
sudo cp /opt/superbot/deploy/poly-us-dry.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart poly-us-dry.service
```

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
